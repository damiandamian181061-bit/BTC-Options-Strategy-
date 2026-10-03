from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from typer.testing import CliRunner

from btc_options.cli import app
from btc_options.domain import MarketSnapshot, utc_now
from btc_options.market import StreamState
from btc_options.replay import read_snapshots
from btc_options.service import available_prices, fresh_recorded_snapshot
from btc_options.storage import MarketStore


def feed(state, name, quote, now, change_id=10):
    timestamp = int(now.timestamp() * 1000)
    state.apply(
        f"book.{name}.100ms",
        {
            "instrument_name": name,
            "timestamp": timestamp,
            "type": "snapshot",
            "change_id": change_id,
            "bids": [["new", quote.bid, quote.bid_size]],
            "asks": [["new", quote.ask, quote.ask_size]],
        },
        now,
    )
    state.apply(
        f"ticker.{name}.100ms",
        {
            "instrument_name": name,
            "timestamp": timestamp,
            "index_price": quote.index,
            "underlying_price": quote.forward,
            "mark_price": quote.mark,
            "mark_iv": quote.iv * 100,
            "greeks": {"delta": quote.delta, "vega": quote.vega},
        },
        now,
    )


def test_stream_snapshot_gap_and_recovery(market, now):
    name = next(iter(market.quotes))
    quote = market.quotes[name]
    state = StreamState(market.instruments)
    feed(state, name, quote, now)
    snapshot = state.snapshot(now)
    assert snapshot.transport == "websocket" and snapshot.source == "production"
    assert snapshot.quotes[name].bid == quote.bid
    assert snapshot.quotes[name].sequence == 10
    with pytest.raises(ValueError, match="sequence gap"):
        state.apply(
            f"book.{name}.100ms",
            {
                "instrument_name": name,
                "timestamp": int(now.timestamp() * 1000),
                "type": "change",
                "change_id": 12,
                "prev_change_id": 11,
                "bids": [],
                "asks": [],
            },
            now,
        )
    assert not state.snapshot(now).quotes
    feed(state, name, quote, now, change_id=13)
    assert state.snapshot(now).quotes[name].sequence == 13


def test_quiet_book_stays_current_only_while_stream_is_alive(market, now):
    name, other = list(market.quotes)[:2]
    state = StreamState(market.instruments)
    feed(state, name, market.quotes[name], now)
    feed(state, other, market.quotes[other], now)
    later = now + timedelta(seconds=20)
    # Only the other instrument's ticker moves; this book and ticker are unchanged but live.
    data = state.tickers[other][0] | {"timestamp": int(later.timestamp() * 1000)}
    state.apply(f"ticker.{other}.100ms", data, later)
    quotes = state.snapshot(later).quotes
    assert other in quotes and name not in quotes  # Its own ticker is still 20s old.
    data = state.tickers[name][0] | {"timestamp": int(later.timestamp() * 1000)}
    state.apply(f"ticker.{name}.100ms", data, later)
    assert state.snapshot(later).quotes[name].exchange_time == later
    # A silent connection keeps nothing fresh.
    assert not state.snapshot(later + timedelta(seconds=16)).quotes


def test_quiet_book_with_sequence_gap_is_never_current(market, now):
    name = next(iter(market.quotes))
    state = StreamState(market.instruments)
    feed(state, name, market.quotes[name], now)
    with pytest.raises(ValueError, match="sequence gap"):
        state.apply(
            f"book.{name}.100ms",
            {
                "instrument_name": name,
                "timestamp": int(now.timestamp() * 1000),
                "type": "change",
                "change_id": 12,
                "prev_change_id": 11,
                "bids": [],
                "asks": [],
            },
            now,
        )
    later = now + timedelta(seconds=5)
    data = state.tickers[name][0] | {"timestamp": int(later.timestamp() * 1000)}
    state.apply(f"ticker.{name}.100ms", data, later)
    assert not state.snapshot(later).quotes


def test_recorded_snapshots_isolate_modes_and_invalidate(settings, market):
    store = MarketStore(settings.runtime_dir / "market")
    now = utc_now()
    snapshot = market.model_copy(update={"source": "production", "asof": now})
    store.snapshot(snapshot)
    assert fresh_recorded_snapshot(settings.runtime_dir, settings).id == snapshot.id
    testnet = settings.model_copy(update={"mode": "testnet"})
    assert fresh_recorded_snapshot(settings.runtime_dir, testnet) is None
    store.invalidate_latest()
    invalid = MarketSnapshot.model_validate_json((settings.runtime_dir / "latest.json").read_text())
    assert all(not q.valid for q in invalid.quotes.values())
    stale = snapshot.model_copy(update={"asof": now - timedelta(seconds=30)})
    store.snapshot(stale)
    assert fresh_recorded_snapshot(settings.runtime_dir, settings) is None


def test_imported_history_joins_ongoing_recording(settings, now):
    settings.runtime_dir.mkdir(exist_ok=True)
    (settings.runtime_dir / "underlying.csv").write_text(
        f"timestamp,price\n{(now - timedelta(days=80)).isoformat()},100000\n{now.isoformat()},101000\n"
    )
    store = MarketStore(settings.runtime_dir / "market")
    store.append("deribit_price_index.btc_usdc", {"price": 102000}, now + timedelta(seconds=1))
    prices = available_prices(settings.runtime_dir)
    assert len(prices) == 3 and prices.iloc[0] == 100000 and prices.iloc[-1] == 102000
    assert prices.index[-1].to_pydatetime() == now + timedelta(minutes=5)
    assert str(prices.index.tz) == "UTC"


def test_history_export_excludes_demo_and_testnet(settings, market, tmp_path):
    config = tmp_path / "paper.toml"
    config.write_text(f'runtime_dir = "{settings.runtime_dir.as_posix()}"\n')
    store = MarketStore(settings.runtime_dir / "market")
    store.snapshot(market)
    store.snapshot(
        market.model_copy(update={"source": "testnet", "asof": market.asof + timedelta(seconds=1)})
    )
    store.snapshot(
        market.model_copy(
            update={"source": "production", "asof": market.asof + timedelta(seconds=2)}
        )
    )
    output = tmp_path / "export"
    result = CliRunner().invoke(app, ["export-history", str(output), "--config", str(config)])
    assert result.exit_code == 0, result.output
    snapshots = read_snapshots(output / "snapshots.jsonl")
    assert len(snapshots) == 1 and snapshots[0].source == "production"


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_history_import_rejects_invalid_prices(tmp_path, now, value):
    from btc_options.service import parse_prices

    path = tmp_path / "bad.csv"
    path.write_text(f"timestamp,price\n{now.isoformat()},{value}\n")
    with pytest.raises(ValueError, match="invalid underlying prices"):
        parse_prices(path)


def test_exchange_clock_ahead_is_clamped_to_receipt(now):
    from btc_options.market import exchange_stamp

    ahead = int((now + timedelta(milliseconds=120)).timestamp() * 1000)
    assert exchange_stamp(ahead, now) == now
    behind = int((now - timedelta(seconds=2)).timestamp() * 1000)
    assert exchange_stamp(behind, now) == now - timedelta(seconds=2)


async def test_deribit_history_backfill_is_incremental_and_complete_only(settings, monkeypatch):
    import httpx

    from btc_options.market import HISTORY_FILE, PublicDeribit, refresh_history
    from btc_options.service import available_prices

    clock = {"now": datetime(2026, 10, 2, 12, 2, tzinfo=timezone.utc)}
    monkeypatch.setattr("btc_options.market.utc_now", lambda: clock["now"])
    calls = []

    def handle(request):
        params = request.url.params
        start, end = int(params["start_timestamp"]), int(params["end_timestamp"])
        calls.append((start, end))
        assert request.url.path.endswith("public/get_tradingview_chart_data")
        ticks = list(range(start - start % 300000, end, 300000))
        return httpx.Response(
            200,
            json={"result": {"status": "ok", "ticks": ticks, "close": [90000.0] * len(ticks)}},
        )

    client = PublicDeribit(transport=httpx.MockTransport(handle))
    try:
        added = await refresh_history(settings.runtime_dir, client, days=2)
        assert added and len(calls) == 1
        prices = available_prices(settings.runtime_dir)
        # The candle covering 12:00-12:05 is still open at 12:02 and is excluded.
        assert prices.index[-1] == datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        assert await refresh_history(settings.runtime_dir, client, days=2) == 0
        clock["now"] += timedelta(minutes=20)
        assert await refresh_history(settings.runtime_dir, client, days=2) == 4
        assert len(calls) == 2
    finally:
        await client.close()
    assert (settings.runtime_dir / HISTORY_FILE).exists()


async def test_history_backfills_a_window_shorter_than_requested(settings, monkeypatch):
    import httpx

    from btc_options.market import HISTORY_FILE, PublicDeribit, refresh_history

    now = datetime(2026, 10, 2, 12, 2, tzinfo=timezone.utc)
    monkeypatch.setattr("btc_options.market.utc_now", lambda: now)
    index = pd.date_range(now - timedelta(days=1), now - timedelta(minutes=2), freq="5min")
    pd.Series(90000.0, index=index).rename("price").rename_axis("timestamp").to_csv(
        settings.runtime_dir / HISTORY_FILE
    )
    calls = []

    def handle(request):
        start, end = (int(request.url.params[k]) for k in ("start_timestamp", "end_timestamp"))
        calls.append((start, end))
        ticks = list(range(start - start % 300000, end, 300000))
        return httpx.Response(
            200, json={"result": {"status": "ok", "ticks": ticks, "close": [1.0] * len(ticks)}}
        )

    client = PublicDeribit(transport=httpx.MockTransport(handle))
    try:
        assert await refresh_history(settings.runtime_dir, client, days=3) > 0
    finally:
        await client.close()
    assert min(s for s, _ in calls) <= int((now - timedelta(days=3)).timestamp() * 1000)
    frame = pd.read_csv(settings.runtime_dir / HISTORY_FILE, parse_dates=["timestamp"])
    assert frame["timestamp"].min() <= pd.Timestamp(now - timedelta(days=2, hours=23))
