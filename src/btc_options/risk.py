"""Independent final authority. AI and strategy code cannot alter these checks."""

from . import greeks
from .domain import Greeks, RiskAssessment
from .strategy import event_context, fee


def assess(candidate, snapshot, ledger, session, settings):
    reasons = []
    try:
        equity = ledger.equity(session, snapshot)
    except ValueError:
        return RiskAssessment(
            approved=False, reasons=["missing portfolio valuation"], reserved_loss=0
        )
    halted = ledger.observe_equity(session, equity, snapshot.asof, settings.risk)
    if halted:
        reasons.append(halted)
    if settings.mode == "live" and snapshot.source != "production":
        reasons.append("live entries require production quotes")
    if event_context(settings, snapshot.asof)["blocked"]:
        reasons.append("scheduled event window")
    if candidate.snapshot_id != snapshot.id:
        reasons.append("candidate snapshot mismatch")
    if (snapshot.asof - candidate.created_at).total_seconds() > settings.max_quote_age_seconds:
        reasons.append("obsolete candidate")
    if candidate.created_at > snapshot.asof:
        reasons.append("future candidate")
    for name in (candidate.long, candidate.short):
        q, instrument = snapshot.quotes.get(name), snapshot.instruments.get(name)
        if q is None or instrument is None or not q.valid:
            reasons.append("missing/invalid leg")
            continue
        if instrument.settlement != "USDC" or instrument.contract_size != 1:
            reasons.append("unsupported contract units")
        if (snapshot.asof - q.exchange_time).total_seconds() > settings.max_quote_age_seconds:
            reasons.append("stale quote")
        if min(q.bid_size, q.ask_size) < candidate.amount:
            reasons.append("insufficient executable size")
    li, si = snapshot.instruments.get(candidate.long), snapshot.instruments.get(candidate.short)
    if li and si:
        correct = li.strike < si.strike if si.option_type == "put" else li.strike > si.strike
        if li.expiry != si.expiry or li.option_type != si.option_type or not correct:
            reasons.append("spread does not provide matching protection")
        if abs(candidate.width - abs(li.strike - si.strike)) > 1e-6:
            reasons.append("invalid payoff width")
    lq, sq = snapshot.quotes.get(candidate.long), snapshot.quotes.get(candidate.short)
    if lq and sq:
        executable_credit = (sq.bid - lq.ask) * candidate.amount
        required_costs = sum(
            fee(p, sq.index, candidate.amount, settings) for p in (sq.bid, sq.ask, lq.bid, lq.ask)
        )
        if candidate.credit > executable_credit + 1e-8 or executable_credit <= 0:
            reasons.append("credit exceeds executable quote")
        if candidate.costs + 1e-8 < required_costs:
            reasons.append("understated execution costs")
        if candidate.partial_loss + 1e-8 < lq.ask * candidate.amount + required_costs:
            reasons.append("understated protection risk")
    reserved = max(candidate.worst_loss, candidate.partial_loss)
    if reserved > equity * settings.risk.per_trade + 1e-8:
        reasons.append("per trade risk exceeded")
    if (
        candidate.worst_loss + 1e-8
        < candidate.width * candidate.amount - candidate.credit + candidate.costs
    ):
        reasons.append("understated maximum loss")
    rows = ledger.spreads(session)
    if len(rows) >= settings.risk.max_positions:
        reasons.append("position limit")
    if sum(r["reserved"] for r in rows) + reserved > equity * settings.risk.aggregate + 1e-8:
        reasons.append("aggregate risk exceeded")
    # Fully funded maximum liability is reserved; no naked-margin benefit is assumed.
    if candidate.width * candidate.amount > equity * settings.risk.margin_utilization:
        reasons.append("margin budget")
    _, held, missing = greeks.portfolio(ledger.positions(session), snapshot)
    if missing:
        reasons.append("missing quotes for held legs")
    # Budgets cover both the completed spread and the temporary protection-only state.
    wing = greeks.leg(li, lq, snapshot.asof, candidate.amount) if li and lq else Greeks()
    index = sq.index if sq else 0
    spread_delta = candidate.delta_notional / index if index else 0
    if (
        max(abs(held.delta + spread_delta), abs(held.delta + wing.delta)) * index
        > equity * settings.risk.delta_notional
    ):
        reasons.append("delta budget")
    if (
        max(abs(held.vega + candidate.vega), abs(held.vega + wing.vega))
        > equity * settings.risk.vega_fraction
    ):
        reasons.append("vega budget")
    if candidate.conservative_pnl <= 0:
        reasons.append("no conservative edge")
    return RiskAssessment(approved=not reasons, reasons=reasons, reserved_loss=reserved)
