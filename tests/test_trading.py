import json
from datetime import timedelta

import pytest

from btc_options.domain import OrderState, MarketSnapshot
from btc_options.execution import PaperAdapter, SpreadExecutor, intent_for
from btc_options.risk import assess
from btc_options.storage import Ledger, MarketStore
from conftest import advance


def test_risk_and_payoff_bound(candidate, market, settings, ledger, session):
    result = assess(candidate, market, ledger, session, settings)
    assert result.approved, result.reasons
    ledger.reserve(session, candidate)
    assert not assess(candidate, market, ledger, session, settings).approved
    li, si = market.instruments[candidate.long], market.instruments[candidate.short]
    for terminal in (0, 50000, 89500, 100000, 1000000):
        payoff = (
            max(li.strike - terminal, 0) - max(si.strike - terminal, 0)
        ) * candidate.amount + candidate.credit
        assert -candidate.worst_loss <= payoff <= candidate.credit


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("credit", 9, "credit exceeds executable quote"),
        ("partial_loss", 0, "understated protection risk"),
        ("costs", 0, "understated execution costs"),
    ],
)
def test_final_risk_recomputes_quote_bounds(
    candidate, market, settings, ledger, session, field, value, reason
):
    assessment = assess(
        candidate.model_copy(update={field: value}), market, ledger, session, settings
    )
    assert not assessment.approved and reason in assessment.reasons


def test_persistent_halt_and_mode_separation(settings, ledger, session, now):
    ledger.halt(session, "operator")
    other = Ledger(settings.runtime_dir / "ledger.sqlite")
    try:
        assert other.session(session)["halt"] == "operator"
        assert other.start(settings, session) == session
        assert other.session(other.start(settings))["halt"] is None
        changed = settings.model_copy(update={"mode": "live", "strategy_capital": 100})
        with pytest.raises(ValueError):
            other.start(changed, session)
    finally:
        other.close()


def test_drawdown_daily_loss_persist(settings, ledger, session, now):
    assert ledger.observe_equity(session, 9890, now, settings.risk) == "daily loss limit"
    assert (
        ledger.observe_equity(session, 10000, now + timedelta(days=1), settings.risk)
        == "daily loss limit"
    )


async def test_latency_no_same_snapshot_fill(ledger, session, settings, market, candidate):
    adapter = PaperAdapter(ledger, session, settings)
    order = intent_for(session, candidate.id, candidate.long, "buy", 0.01, market)
    await adapter.submit(order, market)
    await adapter.advance(market)
    assert not ledger.positions(session)
    await adapter.advance(advance(market))
    assert ledger.positions(session)[candidate.long] == 0.01
    stored = ledger.orders(session)[0]
    assert stored.state == OrderState.FILLED
    assert stored.average == pytest.approx(market.quotes[candidate.long].ask)


async def test_partial_protection_and_short_first_exit(
    ledger, session, settings, market, candidate
):
    candidate = candidate.model_copy(
        update={
            "amount": 0.02,
            "credit": candidate.credit * 2,
            "worst_loss": candidate.worst_loss * 2,
            "partial_loss": candidate.partial_loss * 2,
            "costs": candidate.costs * 2,
        }
    )
    adapter = PaperAdapter(ledger, session, settings)
    executor = SpreadExecutor(adapter, ledger, session)
    await executor.enter(candidate, market)
    first = advance(market)
    q = first.quotes[candidate.long].model_copy(
        update={"ask_size": 0.01, "asks": [(first.quotes[candidate.long].ask, 0.01)]}
    )
    first = first.model_copy(update={"quotes": {**first.quotes, candidate.long: q}})
    await executor.manage(first, settings)
    assert ledger.positions(session)[candidate.long] == 0.01
    short_order = ledger.orders(session)[-1]
    assert short_order.instrument == candidate.short and short_order.amount == 0.01
    second = advance(first)
    await executor.manage(second, settings)
    assert ledger.positions(session)[candidate.short] == -0.01
    await executor.reduce()
    await executor.manage(advance(second), settings)
    assert ledger.orders(session)[-1].side == "buy"
    assert ledger.orders(session)[-1].instrument == candidate.short
    for _ in range(4):
        second = advance(second, 2)
        await executor.manage(second, settings)
    assert not ledger.positions(session)
    assert not ledger.spreads(session)
    expected = 10000 + sum(
        (1 if o.side == "sell" else -1) * o.average * o.filled - o.fees
        for o in ledger.orders(session)
    )
    assert ledger.session(session)["cash"] == pytest.approx(expected)


async def test_restart_recovers_pending_order(settings, ledger, session, market, candidate):
    adapter = PaperAdapter(ledger, session, settings)
    await SpreadExecutor(adapter, ledger, session).enter(candidate, market)
    replacement = Ledger(settings.runtime_dir / "ledger.sqlite")
    try:
        await PaperAdapter(replacement, session, settings).advance(advance(market))
        assert replacement.positions(session)[candidate.long] == 0.01
    finally:
        replacement.close()


async def test_fill_idempotence_and_overfill(ledger, session, settings, market, candidate):
    order = intent_for(session, candidate.id, candidate.long, "buy", 0.01, market)
    ledger.order(order)
    assert ledger.fill(order, "test1", 0.005, 100, 0.1, market.asof)
    assert not ledger.fill(order, "test1", 0.005, 100, 0.1, market.asof)
    with pytest.raises(ValueError):
        ledger.fill(order, "test2", 0.01, 100, 0.1, market.asof)
    assert ledger.positions(session)[candidate.long] == 0.005


def test_future_snapshot_rejected(market):
    payload = market.model_dump(mode="json")
    name = next(iter(payload["quotes"]))
    payload["quotes"][name]["receive_time"] = (market.asof + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        MarketSnapshot.model_validate(payload)


def test_parquet_and_duckdb_roundtrip(settings, market):
    store = MarketStore(settings.runtime_dir / "market")
    store.snapshot(market)
    frame = store.query()
    assert len(frame) == 1
    assert json.loads(frame.iloc[0]["payload"])["source"] == "synthetic"


def test_stale_and_understated_risk_block(ledger, session, settings, market, candidate):
    stale = market.model_copy(update={"asof": market.asof + timedelta(seconds=20)})
    assert not assess(candidate, stale, ledger, session, settings).approved
    broken = candidate.model_copy(update={"worst_loss": 1.0, "partial_loss": 1.0})
    assert not assess(broken, market, ledger, session, settings).approved


async def test_bid_ask_round_trip_alone_does_not_stop_out(
    ledger, session, settings, market, candidate
):
    def quoted(snapshot, name, bid, ask):
        q = snapshot.quotes[name].model_copy(
            update={"bid": bid, "ask": ask, "bids": [(bid, 5)], "asks": [(ask, 5)]}
        )
        return {**snapshot.quotes, name: q}

    market = market.model_copy(update={"quotes": quoted(market, candidate.short, 100, 140)})
    market = market.model_copy(update={"quotes": quoted(market, candidate.long, 30, 50)})
    amount = candidate.amount
    candidate = candidate.model_copy(
        update={
            "credit": 50 * amount,
            "worst_loss": (candidate.width - 50) * amount + candidate.costs,
        }
    )
    executor = SpreadExecutor(PaperAdapter(ledger, session, settings), ledger, session)
    await executor.enter(candidate, market)
    snapshot = market
    for _ in range(4):
        snapshot = advance(snapshot)
        await executor.manage(snapshot, settings)
    # Executable close (140 - 30) is over 2x the 50 credit, but mid (80) is not.
    assert [s["status"] for s in ledger.spreads(session)] == ["OPEN"]
    late = settings.model_copy(update={"exit_before_expiry_hours": 24 * 60})
    await executor.manage(advance(snapshot), late)
    assert [s["status"] for s in ledger.spreads(session)] == ["CLOSING"]
