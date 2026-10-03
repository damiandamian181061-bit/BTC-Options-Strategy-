"""One causal strategy/risk service for paper, testnet, and live adapters."""

import asyncio
from contextlib import suppress
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from . import demo
from .domain import MarketSnapshot, ModelResult, SpreadCandidate, utc_now
from .execution import PaperAdapter, SpreadExecutor
from .forecasts import downside_semivariance, forecast, realized_variance
from .jev import Jev, api_key
from .live import LiveAdapter
from .market import (
    HISTORY_FILE,
    IMPLIED_FILE,
    PublicDeribit,
    collect,
    refresh_history,
    refresh_implied,
)
from .operations import read_controls, take_commands
from .risk import assess
from .storage import Ledger, MarketStore
from .strategy import candidates, event_context, surface_for

STALE_FEED_SECONDS = 60
ENTRY_ERRORS = (ValueError, KeyError, ArithmeticError, OSError, TypeError)


def parse_prices(source):
    """Validated timestamp,price history from a path or text buffer."""
    frame = pd.read_csv(source)
    if not {"timestamp", "price"}.issubset(frame.columns):
        raise ValueError("underlying CSV requires timestamp,price")
    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    values = pd.to_numeric(frame["price"], errors="raise").to_numpy(float)
    if (
        frame.empty
        or timestamps.isna().any()
        or timestamps.duplicated().any()
        or not np.all(np.isfinite(values))
        or np.any(values <= 0)
    ):
        raise ValueError("empty history, duplicate timestamps or invalid underlying prices")
    return pd.Series(values, index=pd.DatetimeIndex(timestamps)).sort_index()


def forecast_inputs(prices, cutoff):
    available = prices[prices.index <= cutoff]
    if available.empty or (pd.Timestamp(cutoff) - available.index[-1]).total_seconds() > 900:
        raise ValueError(
            "underlying history is stale; latest observation must be within 15 minutes"
        )
    rv = realized_variance(available, cutoff)
    downside = downside_semivariance(available, cutoff)
    daily = available.resample("1D").last()
    # Daily close is available only after that UTC day has completed.
    daily = daily[daily.index + pd.Timedelta(days=1) <= cutoff]
    returns = np.log1p(daily.pct_change(fill_method=None).dropna())
    returns.index += pd.Timedelta(days=1)
    return rv, returns, downside


def implied_history(runtime):
    """Daily DVOL as variance per day, indexed by the UTC day it measures."""
    path = Path(runtime) / IMPLIED_FILE
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True))
    return pd.Series((frame["dvol"].to_numpy(float) / 100) ** 2 / 365, index=index)


def forecast_from_prices(prices, cutoff, minimum=60, implied=None):
    rv, returns, downside = forecast_inputs(prices, cutoff)
    if implied is not None:
        implied = implied[implied.index + pd.Timedelta(days=1) <= cutoff]
    return forecast(rv, returns, cutoff, minimum=minimum, downside=downside, implied=implied)


def history_progress(prices, cutoff, minimum=60):
    result = {"completed_days": 0, "required_days": minimum, "ready": False}
    if prices is None:
        return {**result, "reason": "Import intraday history or allow the recorder to build it."}
    try:
        rv, returns, _ = forecast_inputs(prices, cutoff)
        result.update(
            completed_days=min(len(rv), len(returns)),
            variance_days=len(rv),
            return_days=len(returns),
        )
    except (ValueError, KeyError, TypeError) as exc:
        result["reason"] = str(exc)
    return result


def recorded_prices(runtime):
    store = MarketStore(runtime / "market")
    data = store.query("""SELECT time_bucket(INTERVAL '5 minutes', CAST(received_at AS TIMESTAMPTZ)) + INTERVAL '5 minutes' AS timestamp,
       arg_max(TRY_CAST(json_extract(payload,'$.price') AS DOUBLE),received_at) AS price
       FROM market WHERE channel='deribit_price_index.btc_usdc' GROUP BY 1 ORDER BY 1""")
    if isinstance(data, list) or data.empty:
        return None
    # DuckDB returns TIMESTAMPTZ in the machine's zone; histories are joined in UTC.
    index = pd.DatetimeIndex(data["timestamp"])
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return pd.Series(data["price"].to_numpy(float), index=index)


def available_prices(runtime):
    """Deribit backfill, then any imported history, then our own recorded index (wins ties)."""
    histories = []
    for name in (HISTORY_FILE, "underlying.csv"):
        if (Path(runtime) / name).exists():
            histories.append(parse_prices(Path(runtime) / name))
    recorded = recorded_prices(Path(runtime))
    if recorded is not None:
        histories.append(recorded)
    if not histories:
        return None
    prices = pd.concat(histories).sort_index(kind="stable")
    return prices[~prices.index.duplicated(keep="last")]


def fresh_recorded_snapshot(runtime, settings):
    path = Path(runtime) / "latest.json"
    if not path.exists():
        return None
    try:
        snapshot = MarketSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        expected = "testnet" if settings.mode == "testnet" else "production"
        age = (utc_now() - snapshot.asof).total_seconds()
        if snapshot.source == expected and 0 <= age <= settings.max_quote_age_seconds:
            return snapshot
    except (ValueError, OSError):
        pass
    return None


class TradingService:
    """Management first, then optional entries. Jev may veto entries, never authorize risk."""

    def __init__(self, settings, ledger, session, adapter, jev=None):
        self.settings, self.ledger, self.session = settings, ledger, session
        self.executor = SpreadExecutor(adapter, ledger, session)
        self.jev = jev
        self.last_evaluation = None
        self.auto_trade = True
        self.jev_gate = settings.jev_mode == "filter"

    def log(self, kind, payload, timestamp=None):
        self.ledger.event(self.session, kind, payload, timestamp)

    @property
    def jev_enabled(self):
        return self.jev is not None and self.settings.jev_mode != "off"

    async def manage(self, snapshot):
        """Mandatory management never waits for forecasts or Jev. False means halted."""
        try:
            equity = self.ledger.equity(self.session, snapshot)
            if self.ledger.observe_equity(self.session, equity, snapshot.asof, self.settings.risk):
                await self.executor.reduce()
            await self.executor.manage(snapshot, self.settings)
            return True
        except (ValueError, KeyError, httpx.HTTPError) as exc:
            self.ledger.halt(self.session, "management/reconciliation failure")
            self.log("management_error", {"reason": str(exc)}, snapshot.asof)
            try:
                await self.executor.reduce()
            except (httpx.HTTPError, ValueError, KeyError):
                self.log(
                    "cancellation_error",
                    {"reason": "retry cancellation/reconciliation through recovery"},
                    snapshot.asof,
                )
            return False

    async def step(self, snapshot, physical=None):
        if not await self.manage(snapshot):
            return
        equity = self.ledger.equity(self.session, snapshot)
        self.log(
            "equity",
            {"equity": equity, "source": snapshot.source, "snapshot": snapshot.id},
            snapshot.asof,
        )
        if (
            self.last_evaluation
            and (snapshot.asof - self.last_evaluation).total_seconds()
            < self.settings.evaluation_seconds
        ):
            return
        self.last_evaluation = snapshot.asof
        context = self.context(snapshot)
        if context is None:
            return
        await self.advise(snapshot, context)
        if self.ledger.spreads(self.session) or self.ledger.session(self.session)["halt"]:
            return
        try:
            ranked = self.scan(snapshot, physical, equity)
            if ranked:
                # Jev's view of the best idea is recorded even when auto-trading is off.
                decision = await self.judge(ranked[0], snapshot, context)
                if self.auto_trade:
                    await self.enter(ranked[0], snapshot, context, decision)
        except ENTRY_ERRORS as exc:
            self.log("entry_blocked", {"reason": str(exc)}, snapshot.asof)

    def context(self, snapshot):
        try:
            return event_context(self.settings, snapshot.asof)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.log("entry_blocked", {"reason": f"invalid event context: {exc}"}, snapshot.asof)
            return None

    async def advise(self, snapshot, context):
        """Jev CLOSE advice on open spreads; it only acts when the Jev gate is on."""
        if not self.jev_enabled:
            return
        for row in self.ledger.spreads(self.session):
            if row["status"] != "OPEN":
                continue
            position = SpreadCandidate.model_validate_json(row["candidate"]).model_copy(
                update={"created_at": snapshot.asof}
            )
            decision = await self.jev.evaluate(
                position,
                context,
                now=snapshot.asof,
                advisory=True,
                max_age=self.settings.max_quote_age_seconds,
            )
            self.log("jev_advisory", decision, snapshot.asof)
            if self.jev_gate and decision.action == "CLOSE":
                self.ledger.spread_status(position.id, "CLOSING")

    async def judge(self, candidate, snapshot, context):
        if not self.jev_enabled:
            return None
        decision = await self.jev.evaluate(
            candidate, context, now=snapshot.asof, max_age=self.settings.max_quote_age_seconds
        )
        self.log("jev", decision, snapshot.asof)
        return decision

    def scan(self, snapshot, physical, equity):
        """Fit the surface, rank spreads and record the top ideas with their Greeks."""
        surface = surface_for(snapshot, self.settings)
        self.log(
            "model",
            ModelResult(
                model="SSVI",
                version="essvi-hm-1",
                cutoff=snapshot.asof,
                values={
                    "rho_front": float(surface.rho[0]),
                    "rho_back": float(surface.rho[-1]),
                    "iv_rmse": surface.fit_rmse,
                },
                healthy=surface.health(),
                measure="risk_neutral",
            ),
            snapshot.asof,
        )
        self.log("surface", surface.as_dict(), snapshot.asof)
        if physical is None:
            self.log(
                "entry_blocked",
                {"reason": "physical forecast unavailable; record or import intraday history"},
                snapshot.asof,
            )
            return []
        for model in physical.models:
            self.log("model", model, snapshot.asof)
        ranked = candidates(snapshot, physical, equity, self.settings, surface)
        self.log(
            "candidate_scan",
            {"count": len(ranked), "candidates": [c.model_dump(mode="json") for c in ranked[:5]]},
            snapshot.asof,
        )
        return ranked

    async def enter(self, candidate, snapshot, context, decision=None):
        """Jev gate (if on), then the independent risk check, then protected execution."""
        if decision is None:
            decision = await self.judge(candidate, snapshot, context)
        if self.jev_gate and (decision is None or decision.action != "TRADE"):
            return "Jev did not approve this trade"
        risk = assess(candidate, snapshot, self.ledger, self.session, self.settings)
        self.log("risk", risk, snapshot.asof)
        if not risk.approved:
            return "risk check failed: " + ", ".join(risk.reasons)
        await self.executor.enter(candidate, snapshot)
        return None

    async def command(self, command, snapshot, physical):
        """Dashboard request. Entries are re-priced and re-checked on the current snapshot."""
        action = command.get("action")
        reason = None
        try:
            if action == "halt":
                self.ledger.halt(self.session, "operator kill switch")
                await self.executor.reduce()
            elif action == "close":
                rows = {r["id"]: r for r in self.ledger.spreads(self.session)}
                if command.get("spread") not in rows:
                    reason = "no such open spread"
                else:
                    self.ledger.spread_status(command["spread"], "CLOSING")
            elif action == "enter":
                reason = await self.requested_entry(command, snapshot, physical)
            else:
                reason = "unknown command"
        except ENTRY_ERRORS as exc:
            reason = str(exc)
        self.log(
            "command",
            {
                "id": command.get("id"),
                "action": action,
                "status": "rejected" if reason else "accepted",
                "reason": reason or "",
            },
            snapshot.asof,
        )

    async def requested_entry(self, command, snapshot, physical):
        if self.ledger.session(self.session)["halt"]:
            return "entries are halted for this session"
        if len(self.ledger.spreads(self.session)) >= self.settings.risk.max_positions:
            return "position limit reached"
        context = self.context(snapshot)
        if context is None:
            return "invalid event context"
        if physical is None:
            return "forecast history is still warming up; entries are blocked"
        ranked = self.scan(snapshot, physical, self.ledger.equity(self.session, snapshot))
        legs = (command.get("long"), command.get("short"))
        match = next((c for c in ranked if (c.long, c.short) == legs), None)
        if match is None:
            return "this spread no longer passes the strategy checks at current quotes"
        return await self.enter(match, snapshot, context)


async def run(
    settings,
    seconds=300,
    prices_path=None,
    synthetic=False,
    resume=None,
    armed=False,
    release=None,
    recorded_only=False,
    record=True,
):
    if synthetic and settings.mode != "paper":
        raise ValueError("synthetic market is restricted to paper mode")
    prices = parse_prices(prices_path) if prices_path else None
    ledger = Ledger(settings.runtime_dir / "ledger.sqlite")
    session = ledger.start(settings, resume)
    jev = Jev(settings.jev_model)
    client = PublicDeribit(testnet=settings.mode == "testnet") if not synthetic else None
    history_client = PublicDeribit() if not synthetic else None
    # Local paper runs its own WebSocket recorder so quotes match the managed stack;
    # REST snapshots are only a fallback until the stream produces one.
    recorder = (
        asyncio.create_task(collect(settings, seconds))
        if record and settings.mode == "paper" and not synthetic and not recorded_only
        else None
    )
    adapter = (
        PaperAdapter(ledger, session, settings)
        if settings.mode == "paper"
        else LiveAdapter(ledger, session, settings, armed=armed, release=release)
    )
    service = TradingService(settings, ledger, session, adapter, jev)
    jev_status = {"mode": settings.jev_mode, "model": settings.jev_model}
    ledger.event(session, "jev_status", {**jev_status, "available": bool(jev.key)})
    ledger.event(session, "worker_status", {"state": "starting"})
    store = MarketStore(settings.runtime_dir / "market")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds

    def remaining(limit):
        return min(limit, max(0, deadline - loop.time()))

    initialized = False
    stale_since = None
    wait_for_recorder = recorded_only or recorder is not None
    refreshed_at = None
    last_snapshot_id = None
    history_stamp = None
    print(
        f"Session {session} | {settings.mode.upper()} | "
        f"{'SYNTHETIC DEMONSTRATION' if synthetic else 'exchange quotes'}"
    )
    try:
        while loop.time() < deadline:
            if (key := api_key()) != jev.key:
                await jev.close()
                service.jev = jev = Jev(settings.jev_model, key=key)
                ledger.event(session, "jev_status", {**jev_status, "available": bool(jev.key)})
            controls = read_controls(settings.runtime_dir, settings.jev_mode)
            service.auto_trade, service.jev_gate = controls["auto_trade"], controls["jev_gate"]
            try:
                recorded = (
                    None if synthetic else fresh_recorded_snapshot(settings.runtime_dir, settings)
                )
                if wait_for_recorder and recorded is None:
                    stale_since = stale_since or loop.time()
                    # Reconnects (e.g. Deribit listing new strikes) take seconds; only a
                    # sustained outage halts entries and starts reduction.
                    if initialized and loop.time() - stale_since > STALE_FEED_SECONDS:
                        ledger.halt(
                            session, "recorded market feed stale; reconcile before new entries"
                        )
                        await service.executor.reduce()
                    await asyncio.sleep(1)
                    continue
                stale_since = None
                snapshot = (
                    demo.snapshot()
                    if synthetic
                    else (recorded or await client.snapshot(0, settings.max_expiry_days + 1))
                )
            except (httpx.HTTPError, OSError, ValueError) as exc:
                ledger.halt(session, "market disconnect or invalid snapshot")
                ledger.event(session, "data_gap", {"type": type(exc).__name__})
                await asyncio.sleep(remaining(5))
                continue
            if snapshot.id == last_snapshot_id:
                await asyncio.sleep(remaining(1))
                continue
            last_snapshot_id = snapshot.id
            if not recorded:
                store.snapshot(snapshot)
            if not initialized and isinstance(adapter, LiveAdapter):
                await adapter.initialize(snapshot)
            initialized = True
            physical = demo.forecast(snapshot.asof) if synthetic else None
            history_file = prices_path or settings.runtime_dir / "underlying.csv"
            stamp = history_file.stat().st_mtime_ns if history_file.exists() else 0
            history_changed = stamp != history_stamp
            if not synthetic and (
                refreshed_at is None
                or history_changed
                or (snapshot.asof - refreshed_at).total_seconds() >= settings.evaluation_seconds
            ):
                if not prices_path:
                    try:
                        # Physical history is production BTC even when trading on testnet.
                        await refresh_history(settings.runtime_dir, history_client)
                        await refresh_implied(settings.runtime_dir, history_client)
                    except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
                        ledger.event(
                            session,
                            "forecast_unavailable",
                            {"reason": f"Deribit history refresh failed: {exc}"},
                            snapshot.asof,
                        )
                try:
                    prices = (
                        parse_prices(prices_path)
                        if prices_path
                        else available_prices(settings.runtime_dir)
                    )
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    prices = None
                    ledger.event(
                        session,
                        "forecast_unavailable",
                        {"reason": f"invalid underlying history: {exc}"},
                        snapshot.asof,
                    )
                refreshed_at = snapshot.asof
                history_stamp = stamp
            forecast_error = None
            if prices is not None:
                try:
                    physical = forecast_from_prices(
                        prices,
                        snapshot.asof,
                        settings.min_forecast_days,
                        implied_history(settings.runtime_dir),
                    )
                except ValueError as exc:
                    forecast_error = str(exc)
                    ledger.event(
                        session, "forecast_unavailable", {"reason": str(exc)}, snapshot.asof
                    )
            if not synthetic and (
                service.last_evaluation is None
                or (snapshot.asof - service.last_evaluation).total_seconds()
                >= settings.evaluation_seconds
                or history_changed
            ):
                progress = history_progress(prices, snapshot.asof, settings.min_forecast_days)
                progress.update(
                    ready=physical is not None, reason=forecast_error or progress.get("reason", "")
                )
                ledger.event(session, "history_progress", progress, snapshot.asof)
            await service.step(snapshot, physical)
            for command in take_commands(settings.runtime_dir):
                if settings.mode == "paper":
                    await service.command(command, snapshot, physical)
                else:
                    ledger.event(
                        session,
                        "command",
                        {**command, "status": "rejected", "reason": "dashboard is paper-only"},
                        snapshot.asof,
                    )
            ledger.event(session, "worker_status", {"state": "running", **controls})
            await asyncio.sleep(remaining(1))
    finally:
        if recorder:
            recorder.cancel()
            with suppress(asyncio.CancelledError):
                await recorder
        ledger.event(session, "worker_status", {"state": "stopped"})
        await jev.close()
        for public in (client, history_client):
            if public:
                await public.close()
        if isinstance(adapter, LiveAdapter):
            await adapter.close()
        ledger.close()
    return session
