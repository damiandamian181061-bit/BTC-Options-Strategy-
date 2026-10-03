"""Credential-free Deribit data access, sequence checks, and durable collection."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pandas as pd
import websockets

from .domain import Instrument, MarketSnapshot, Quote, utc_now
from .operations import atomic_text
from .storage import MarketStore


def from_ms(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def exchange_stamp(value_ms, received):
    """Exchange clocks can run ahead of ours; nothing is stamped after we received it."""
    return min(from_ms(value_ms), received)


class PublicDeribit:
    def __init__(self, testnet=False, transport=None):
        host = "test.deribit.com" if testnet else "www.deribit.com"
        self.base = f"https://{host}/api/v2/"
        self.ws = f"wss://{host}/ws/api/v2"
        self.testnet = testnet
        self.client = httpx.AsyncClient(timeout=15, transport=transport)

    async def close(self):
        await self.client.aclose()

    async def call(self, method, **params):
        if not method.startswith("public/"):
            raise ValueError("public data client forbids authenticated methods")
        for attempt in range(6):
            response = await self.client.get(self.base + method, params=params)
            if response.status_code != 429:
                break
            # Public requests share a credit pool; back off instead of dropping data.
            await asyncio.sleep(0.2 * 2**attempt)
        response.raise_for_status()
        result = response.json()
        if "error" in result:
            raise ValueError(f"Deribit public API error: {result['error'].get('code')}")
        return result["result"]

    async def instruments(self):
        data = await self.call(
            "public/get_instruments", currency="USDC", kind="option", expired=False
        )
        result = {}
        for item in data:
            if item.get("base_currency") != "BTC" or item.get("settlement_currency") != "USDC":
                continue
            if not item.get("is_active", False):
                continue
            instrument = Instrument(
                name=item["instrument_name"],
                expiry=from_ms(item["expiration_timestamp"]),
                strike=item["strike"],
                option_type=item["option_type"],
                contract_size=item["contract_size"],
                min_amount=item["min_trade_amount"],
                amount_step=item["min_trade_amount"],
                tick_size=item["tick_size"],
                tick_steps=item.get("tick_size_steps", []),
            )
            result[instrument.name] = instrument
        return result

    async def snapshot(self, min_days=0, max_days=60):
        metadata = await self.instruments()
        now = utc_now()
        metadata = {
            n: i
            for n, i in metadata.items()
            if min_days <= (i.expiry - now).total_seconds() / 86400 <= max_days
        }
        semaphore = asyncio.Semaphore(5)

        async def fetch(name):
            async with semaphore:
                book = await self.call("public/get_order_book", instrument_name=name, depth=20)
            received = utc_now()
            try:
                return Quote(
                    instrument=name,
                    exchange_time=exchange_stamp(book["timestamp"], received),
                    receive_time=received,
                    bid=book["best_bid_price"],
                    ask=book["best_ask_price"],
                    bid_size=book["best_bid_amount"],
                    ask_size=book["best_ask_amount"],
                    index=book["index_price"],
                    forward=book["underlying_price"],
                    mark=book["mark_price"],
                    iv=book["mark_iv"] / 100,
                    delta=book["greeks"]["delta"],
                    vega=book["greeks"].get("vega", 0),
                    bids=book["bids"],
                    asks=book["asks"],
                    sequence=book["change_id"],
                )
            except (KeyError, TypeError, ValueError):
                return None  # Empty, crossed, or incomplete books never become candidates.

        results = await asyncio.gather(*(fetch(n) for n in metadata), return_exceptions=True)
        quotes = {q.instrument: q for q in results if isinstance(q, Quote)}
        return MarketSnapshot(
            asof=utc_now(),
            instruments=metadata,
            quotes=quotes,
            source="testnet" if self.testnet else "production",
        )


class BookSequence:
    """Ungrouped book diffs require a snapshot and an unbroken change-id chain."""

    def __init__(self):
        self.sequence = None
        self.bids = {}
        self.asks = {}

    def apply(self, packet):
        if packet["type"] == "snapshot":
            self.bids, self.asks = {}, {}
        elif self.sequence is None or packet.get("prev_change_id") != self.sequence:
            self.sequence = None
            self.bids, self.asks = {}, {}
            raise ValueError("book sequence gap: resubscribe for snapshot")
        for side in ("bids", "asks"):
            levels = getattr(self, side)
            for action, price, size in packet[side]:
                if action == "delete":
                    levels.pop(price, None)
                elif action in ("new", "change") and size >= 0:
                    levels[price] = size
                else:
                    raise ValueError("invalid book update")
        self.sequence = packet["change_id"]


class StreamState:
    """Rebuild executable snapshots from validated books and synchronized tickers."""

    def __init__(self, metadata):
        self.metadata = metadata
        self.books = {}
        self.book_times = {}
        self.tickers = {}
        self.clock = None  # Latest (exchange time, receive time) seen on this connection.

    def apply(self, channel, data, received):
        if isinstance(data, dict) and isinstance(data.get("timestamp"), (int, float)):
            stamp = (exchange_stamp(data["timestamp"], received), received)
            self.clock = stamp if self.clock is None else max(self.clock, stamp)
        if channel.startswith("book."):
            name = data["instrument_name"]
            self.books.setdefault(name, BookSequence()).apply(data)
            self.book_times[name] = (exchange_stamp(data["timestamp"], received), received)
        elif channel.startswith("ticker.") and data.get("instrument_name") in self.metadata:
            self.tickers[data["instrument_name"]] = (data, received)

    def snapshot(self, now, max_age=15):
        quotes = {}
        for name, book in self.books.items():
            if book.sequence is None or name not in self.tickers or name not in self.metadata:
                continue
            data, received = self.tickers[name]
            # Deribit pushes a book only when it changes. With an unbroken change-id chain on
            # a live connection, an unchanged book is current as of the latest packet.
            book_time, book_received = max(self.book_times[name], self.clock)
            exchange_time = min(exchange_stamp(data["timestamp"], received), book_time)
            receive_time = min(received, book_received)
            if (
                not 0 <= (now - receive_time).total_seconds() <= max_age
                or not 0 <= (now - exchange_time).total_seconds() <= max_age
            ):
                continue
            bids = sorted(((p, s) for p, s in book.bids.items() if s > 0), reverse=True)[:20]
            asks = sorted((p, s) for p, s in book.asks.items() if s > 0)[:20]
            if not bids or not asks:
                continue
            try:
                quotes[name] = Quote(
                    instrument=name,
                    exchange_time=exchange_time,
                    receive_time=receive_time,
                    bid=bids[0][0],
                    bid_size=bids[0][1],
                    ask=asks[0][0],
                    ask_size=asks[0][1],
                    index=data["index_price"],
                    forward=data["underlying_price"],
                    mark=data["mark_price"],
                    iv=data["mark_iv"] / 100,
                    delta=data["greeks"]["delta"],
                    vega=data["greeks"].get("vega", 0),
                    bids=bids,
                    asks=asks,
                    sequence=book.sequence,
                )
            except (ValueError, KeyError, TypeError):
                continue
        return MarketSnapshot(
            asof=now,
            instruments=self.metadata,
            quotes=quotes,
            source="production",
            transport="websocket",
        )


async def collect(settings, seconds=300, streaming=True):
    client = PublicDeribit()
    store = MarketStore(settings.runtime_dir / "market", batch_size=4096)
    deadline = asyncio.get_running_loop().time() + seconds
    stop = asyncio.Event()
    last_stream_snapshot = 0

    async def snapshots():
        while not stop.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                snapshot = await client.snapshot(0, settings.max_expiry_days + 1)
                if asyncio.get_running_loop().time() - last_stream_snapshot > 15:
                    store.snapshot(snapshot)
                else:
                    store.append("rest_snapshot", snapshot.model_dump(mode="json"), snapshot.asof)
            except (httpx.HTTPError, ValueError) as exc:
                store.append("collector_error", {"type": type(exc).__name__})
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=min(60, max(0.01, deadline - asyncio.get_running_loop().time())),
                )
            except TimeoutError:
                pass

    async def stream():
        nonlocal last_stream_snapshot
        backoff = 1
        while not stop.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                metadata = await client.instruments()
                channels = [
                    "deribit_price_index.btc_usdc",
                    "deribit_volatility_index.btc_usd",
                    "trades.option.USDC.100ms",
                    "trades.future.BTC.100ms",
                    "ticker.BTC-PERPETUAL.100ms",
                    "instrument.state.option.USDC",
                    "instrument.creation.option.USDC",
                ]
                channels += [f"ticker.{n}.100ms" for n in metadata]
                channels += [
                    f"book.{n}.100ms"
                    for n, i in metadata.items()
                    if 0
                    < (i.expiry - utc_now()).total_seconds() / 86400
                    <= settings.max_expiry_days
                ]
                async with websockets.connect(
                    client.ws, ping_interval=20, max_size=8_000_000
                ) as socket:
                    for start in range(0, len(channels), 100):
                        await socket.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": start + 1,
                                    "method": "public/subscribe",
                                    "params": {"channels": channels[start : start + 100]},
                                }
                            )
                        )
                    state = StreamState(metadata)
                    backoff = 1
                    while not stop.is_set() and asyncio.get_running_loop().time() < deadline:
                        try:
                            packet = json.loads(await asyncio.wait_for(socket.recv(), timeout=1))
                        except TimeoutError:
                            continue
                        if "error" in packet:
                            store.append("subscription_error", packet["error"])
                            raise ValueError("subscription refused")
                        if packet.get("method") != "subscription":
                            continue
                        channel, data = packet["params"]["channel"], packet["params"]["data"]
                        received = utc_now()
                        store.append(channel, data, received)
                        state.apply(channel, data, received)
                        if channel.startswith("instrument.creation.") or channel.startswith(
                            "instrument.state."
                        ):
                            raise ValueError(
                                "instrument lifecycle change: refresh metadata and subscriptions"
                            )
                        if asyncio.get_running_loop().time() - last_stream_snapshot >= 3:
                            snapshot = state.snapshot(utc_now(), settings.max_quote_age_seconds)
                            if snapshot.quotes:
                                store.snapshot(snapshot)
                                last_stream_snapshot = asyncio.get_running_loop().time()
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                websockets.ConnectionClosed,
                httpx.HTTPError,
            ) as exc:
                store.append("stream_gap", {"type": type(exc).__name__, "retry_seconds": backoff})
                store.invalidate_latest()
                await asyncio.sleep(
                    min(backoff, max(0, deadline - asyncio.get_running_loop().time()))
                )
                backoff = min(backoff * 2, 30)

    try:
        await asyncio.gather(snapshots(), stream() if streaming else asyncio.sleep(0))
    finally:
        stop.set()
        store.flush()
        await client.close()


HISTORY_FILE = "deribit-history.csv"
CANDLE_MS = 5 * 60 * 1000


async def price_candles(client, start, end):
    """5-minute BTC-PERPETUAL closes from Deribit's public chart API, stamped at candle end.

    The perpetual tracks the BTC index within basis points, so its realized variance is a
    faithful physical-history proxy until the recorder has collected index data itself.
    """
    rows = {}
    cursor = int(start.timestamp() * 1000)
    stop = int(end.timestamp() * 1000)
    while cursor < stop:
        upper = min(stop, cursor + 4900 * CANDLE_MS)
        data = await client.call(
            "public/get_tradingview_chart_data",
            instrument_name="BTC-PERPETUAL",
            resolution="5",
            start_timestamp=cursor,
            end_timestamp=upper,
        )
        if data.get("status") == "ok":
            for tick, close in zip(data["ticks"], data["close"], strict=True):
                # Only completed candles: a candle closes at tick + 5 minutes.
                if close and close > 0 and tick + CANDLE_MS <= stop:
                    rows[from_ms(tick + CANDLE_MS)] = float(close)
        cursor = upper
    return rows


async def refresh_history(runtime, client, days=365):
    """Backfill/extend real Deribit history in runtime/deribit-history.csv; returns rows added.

    A year supports direct multi-horizon HAR regressions and a stable daily GJR-GARCH fit."""
    path = runtime / HISTORY_FILE
    now = utc_now()
    earliest = now - pd.Timedelta(days=days)
    existing = None
    if path.exists():
        frame = pd.read_csv(path)
        existing = pd.Series(
            frame["price"].to_numpy(float),
            index=pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True)),
        )
        existing = existing[existing.index >= now - pd.Timedelta(days=days + 5)]
    ranges = []
    if existing is None or not len(existing):
        ranges.append((earliest, now))
    else:
        first, last = existing.index[0].to_pydatetime(), existing.index[-1].to_pydatetime()
        # Older installs kept a shorter window; fill the missing early range once.
        if first - earliest > pd.Timedelta(days=1):
            ranges.append((earliest, first))
        if (now - last).total_seconds() >= 300:
            ranges.append((last, now))
    if not ranges:
        return 0
    rows = {}
    for start, end in ranges:
        rows.update(await price_candles(client, start, end))
    fresh = pd.Series(rows, dtype=float)
    if fresh.empty:
        return 0
    fresh.index = pd.DatetimeIndex(fresh.index)
    merged = fresh if existing is None else pd.concat([existing, fresh])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    atomic_text(path, merged.rename("price").rename_axis("timestamp").to_csv())
    return len(fresh)


IMPLIED_FILE = "deribit-dvol.csv"


async def refresh_implied(runtime, client, days=400):
    """Daily BTC DVOL closes (annualized %) stamped at the day measured; refreshed hourly."""
    path = runtime / IMPLIED_FILE
    now = utc_now()
    if path.exists() and (now.timestamp() - path.stat().st_mtime) < 3600:
        return 0
    end = int(now.timestamp() * 1000)
    data = await client.call(
        "public/get_volatility_index_data",
        currency="BTC",
        resolution="1D",
        start_timestamp=end - days * 86400000,
        end_timestamp=end,
    )
    # A daily candle is complete only after its UTC day ends.
    rows = [
        (from_ms(tick), float(close))
        for tick, _, _, _, close in data["data"]
        if close and close > 0 and tick + 86400000 <= end
    ]
    if not rows:
        return 0
    frame = pd.DataFrame(rows, columns=["timestamp", "dvol"])
    atomic_text(path, frame.to_csv(index=False))
    return len(rows)
