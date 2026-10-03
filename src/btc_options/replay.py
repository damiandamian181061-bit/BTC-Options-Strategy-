"""Chronological executable-quote replay with three independently accounted portfolios."""

import json

import numpy as np

from .domain import MarketSnapshot, DecisionRecord
from .execution import PaperAdapter
from .jev import Jev
from .service import TradingService, forecast_from_prices, parse_prices
from .storage import Ledger


def read_snapshots(path):
    snapshots = [
        MarketSnapshot.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not snapshots:
        raise ValueError("empty replay history")
    if any(b.asof <= a.asof for a, b in zip(snapshots, snapshots[1:], strict=False)):
        raise ValueError("replay snapshots must be strictly chronological")
    if any(s.source not in ("historical", "production") for s in snapshots):
        raise ValueError("performance replay excludes synthetic/testnet data")
    return snapshots


def session_report(ledger, sid):
    session = ledger.session(sid)
    rows = list(
        ledger.db.execute(
            "SELECT timestamp,payload FROM events WHERE session=? AND kind='equity' ORDER BY seq",
            (sid,),
        )
    )
    equity = np.array([json.loads(r["payload"])["equity"] for r in rows])
    sources = [json.loads(r["payload"])["source"] for r in rows]
    spans = 0
    if len(rows) > 1:
        from datetime import datetime

        spans = (
            datetime.fromisoformat(rows[-1]["timestamp"])
            - datetime.fromisoformat(rows[0]["timestamp"])
        ).total_seconds() / 86400
    completed = ledger.db.execute(
        "SELECT id FROM spreads WHERE session=? AND status='CLOSED'", (sid,)
    ).fetchall()
    profits = []
    for spread in completed:
        pnl = 0
        for order in ledger.orders(sid):
            if order.candidate_id == spread[0]:
                pnl += (
                    1 if order.side == "sell" else -1
                ) * order.filled * order.average - order.fees
        c = json.loads(
            ledger.db.execute("SELECT candidate FROM spreads WHERE id=?", (spread[0],)).fetchone()[
                0
            ]
        )
        for name in (c["long"], c["short"]):
            settled = ledger.db.execute(
                "SELECT cashflow,fee FROM settlements WHERE session=? AND instrument=? AND candidate_id=?",
                (sid, name, spread[0]),
            ).fetchone()
            if settled:
                pnl += settled[0] - settled[1]
        profits.append(pnl)
    gains = sum(max(p, 0) for p in profits)
    losses = sum(max(-p, 0) for p in profits)
    drawdown = (
        float(np.max(1 - equity / np.maximum.accumulate(np.r_[session["initial"], equity])[1:]))
        if len(equity)
        else 0
    )
    failures = ledger.db.execute(
        "SELECT COUNT(*) FROM events WHERE session=? AND kind='management_error'", (sid,)
    ).fetchone()[0]
    reconciles = ledger.db.execute(
        "SELECT COUNT(*) FROM events WHERE session=? AND kind='reconciliation'", (sid,)
    ).fetchone()[0]
    return dict(
        schema_version=1,
        session=sid,
        mode=session["mode"],
        initial=session["initial"],
        final_equity=float(equity[-1]) if len(equity) else session["cash"],
        cash=session["cash"],
        closed_spreads=len(completed),
        net_realized=sum(profits),
        profit_factor=gains / losses if losses else None,
        win_rate=sum(p > 0 for p in profits) / len(profits) if profits else None,
        max_drawdown=drawdown,
        days=spans,
        production_only=bool(sources) and all(s == "production" for s in sources),
        reconciliations=reconciles,
        management_failures=failures,
        halt=session["halt"],
        open_positions=ledger.positions(sid),
        limitations=[
            "Executable-depth IOC assumptions require forward fill validation.",
            "Profit factor is undefined without realized losses.",
            "Unclosed positions are marked at executable bid/ask, not counted as realized wins.",
        ],
    )


class RecordedJev(Jev):
    def __init__(self, path=None):
        super().__init__(key="")
        self.records = (
            []
            if path is None
            else [
                DecisionRecord.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )

    async def evaluate(self, candidate, context, now=None, **kwargs):
        packet = await super().evaluate(candidate, context, now=now, **kwargs)
        matching = [
            r
            for r in self.records
            if r.request_hash == packet.request_hash
            and r.model == self.model
            and r.timestamp <= now
            and 0 <= (now - r.timestamp).total_seconds() <= kwargs.get("max_age", 15)
            and r.status == "valid"
        ]
        if matching:
            result = matching[-1].model_copy(update={"candidate_id": candidate.id})
            return result
        return packet


async def backtest(settings, snapshots_path, prices_path, output, decisions_path=None):
    snapshots = read_snapshots(snapshots_path)
    prices = parse_prices(prices_path)
    output.mkdir(parents=True, exist_ok=True)
    reports = {}
    for name in ("quantitative", "deterministic_context", "jev_filter"):
        cfg = settings.model_copy(deep=True)
        cfg.mode = "paper"
        cfg.jev_mode = "filter" if name == "jev_filter" else "off"
        if name == "quantitative":
            cfg.event_calendar = ""
        ledger = Ledger(output / f"{name}.sqlite")
        sid = ledger.start(cfg, now=snapshots[0].asof)
        jev = RecordedJev(decisions_path)
        service = TradingService(cfg, ledger, sid, PaperAdapter(ledger, sid, cfg), jev)
        try:
            for snapshot in snapshots:
                try:
                    physical = forecast_from_prices(prices, snapshot.asof, cfg.min_forecast_days)
                except ValueError:
                    physical = None
                await service.step(snapshot, physical)
            reports[name] = session_report(ledger, sid)
        finally:
            await jev.close()
            ledger.close()
    result = dict(
        kind="backtest",
        portfolios=reports,
        matched_risk_budgets=True,
        historical_executable_quotes=True,
        regimes=[],
        jev_evaluable=decisions_path is not None,
        limitations=[
            "No temporal model promotion from a single snapshot fit.",
            "Regime coverage must be annotated and validated separately.",
            "Missing recorded Jev judgments abstain; retrospective model calls are excluded.",
        ],
    )
    (output / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
