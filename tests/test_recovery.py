import json
from datetime import timedelta

import httpx
import pytest

from btc_options.domain import Settlement, OrderState
from btc_options.execution import PaperAdapter, SpreadExecutor, intent_for
from btc_options.live import LiveAdapter
from btc_options.contracts import expiry_payoff
from conftest import advance


def test_expiry_payoff_units():
    assert expiry_payoff(100000, 80000, "put", "USDC") == 20000
    assert expiry_payoff(100000, 80000, "put", "BTC") == 0.25


async def test_halt_does_not_cancel_risk_reduction(ledger, session, settings, market, candidate):
    adapter = PaperAdapter(ledger, session, settings)
    executor = SpreadExecutor(adapter, ledger, session)
    await executor.enter(candidate, market)
    for _ in range(3):
        market = advance(market)
        await executor.manage(market, settings)
    ledger.halt(session, "operator")
    for _ in range(6):
        market = advance(market)
        await executor.reduce()
        await executor.manage(market, settings)
    assert not ledger.positions(session)


async def test_settlement_exact_and_idempotent(ledger, session, settings, market, candidate):
    adapter = PaperAdapter(ledger, session, settings)
    executor = SpreadExecutor(adapter, ledger, session)
    await executor.enter(candidate, market)
    for _ in range(3):
        market = advance(market)
        await executor.manage(market, settings)
    expiry = market.instruments[candidate.long].expiry
    settled = market.model_copy(update={"asof": expiry + timedelta(minutes=1)})
    before = ledger.session(session)["cash"]
    for name in (candidate.long, candidate.short):
        record = Settlement(
            instrument=name,
            expiry=expiry,
            confirmed_at=settled.asof,
            delivery_price=50000,
            source="synthetic",
        )
        assert adapter.settle(record, settled)
        assert not adapter.settle(record, settled)
    assert not ledger.positions(session)
    # Spread intrinsic liability is exactly width*amount plus the configured delivery charges.
    assert ledger.session(session)["cash"] == pytest.approx(before - 10 - 0.15)


async def test_ambiguous_live_timeout_never_resubmits(
    settings, ledger, market, candidate, monkeypatch
):
    cfg = settings.model_copy(update={"mode": "testnet", "strategy_capital": 10000})
    sid = ledger.start(cfg, now=market.asof)
    ledger.reserve(sid, candidate)
    monkeypatch.setenv("DERIBIT_TESTNET_CLIENT_ID", "mock-client")
    monkeypatch.setenv("DERIBIT_TESTNET_CLIENT_SECRET", "mock-secret")
    calls = []

    async def handle(request):
        data = json.loads(request.content)
        method = data["method"]
        calls.append(method)
        if method == "public/auth":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "scope": "account:read trade:read_write wallet:none",
                        "access_token": "mock",
                        "expires_in": 1000,
                    }
                },
            )
        if method == "private/get_margins":
            return httpx.Response(200, json={"result": {"buy": 1.0, "sell": 1.0}})
        if method == "private/get_account_summary":
            return httpx.Response(
                200, json={"result": {"available_funds": 10000, "initial_margin": 0}}
            )
        if method == "private/buy":
            raise httpx.ReadTimeout("ambiguous outcome")
        if method == "private/get_order_state_by_label":
            return httpx.Response(200, json={"result": []})
        raise AssertionError(method)

    adapter = LiveAdapter(ledger, sid, cfg, armed=True, transport=httpx.MockTransport(handle))
    adapter.ready = True
    quote = market.quotes[candidate.long]
    # Execution freshness is a wall clock check; mocked timestamps retain all other fixture data.
    from btc_options.domain import utc_now

    now = utc_now()
    market = market.model_copy(
        update={
            "asof": now,
            "source": "testnet",
            "quotes": {
                **market.quotes,
                candidate.long: quote.model_copy(
                    update={"exchange_time": now, "receive_time": now}
                ),
            },
        }
    )
    order = intent_for(sid, candidate.id, candidate.long, "buy", 0.01, market)
    try:
        await adapter.submit(order, market)
        assert order.state == OrderState.RECONCILING
        await adapter.advance(market)
        await adapter.advance(market)
        await adapter.submit(order, market)
        assert calls.count("private/buy") == 1
        assert ledger.session(sid)["halt"]
    finally:
        await adapter.close()


async def test_exchange_cash_reconciliation_halts_on_unmanaged_change(
    settings, ledger, market, monkeypatch
):
    cfg = settings.model_copy(update={"mode": "testnet", "strategy_capital": 10000})
    sid = ledger.start(cfg, now=market.asof)
    monkeypatch.setenv("DERIBIT_TESTNET_CLIENT_ID", "mock-client")
    monkeypatch.setenv("DERIBIT_TESTNET_CLIENT_SECRET", "mock-secret")
    adapter = LiveAdapter(ledger, sid, cfg, armed=True)
    balance = 10000

    async def rpc(method, **params):
        if method == "private/get_positions":
            return []
        return {
            "balance": balance,
            "equity": balance,
            "available_funds": balance,
            "initial_margin": 0,
        }

    monkeypatch.setattr(adapter, "rpc", rpc)
    try:
        await adapter.reconcile(market)
        await adapter.reconcile(market)
        balance = 10001
        with pytest.raises(ValueError, match="cash mismatch"):
            await adapter.reconcile(market)
        assert ledger.session(sid)["halt"]
    finally:
        await adapter.close()


async def test_settlement_is_attributed_only_to_spread_held_at_expiry(
    ledger, session, settings, market, candidate
):
    from btc_options.domain import identifier
    from btc_options.replay import session_report

    adapter = PaperAdapter(ledger, session, settings)
    executor = SpreadExecutor(adapter, ledger, session)
    await executor.enter(candidate, market)
    for _ in range(3):
        market = advance(market)
        await executor.manage(market, settings)
    ledger.spread_status(candidate.id, "CLOSING")
    for _ in range(3):
        market = advance(market)
        await executor.manage(market, settings)
    assert not ledger.positions(session)
    next_candidate = candidate.model_copy(
        update={"id": identifier(), "created_at": market.asof, "snapshot_id": market.id}
    )
    await executor.enter(next_candidate, market)
    for _ in range(3):
        market = advance(market)
        await executor.manage(market, settings)
    expiry = market.instruments[candidate.long].expiry
    settled = market.model_copy(update={"asof": expiry + timedelta(minutes=1)})
    for name in (candidate.long, candidate.short):
        adapter.settle(
            Settlement(
                instrument=name,
                expiry=expiry,
                confirmed_at=settled.asof,
                delivery_price=50000,
                source="synthetic",
            ),
            settled,
        )
    ledger.spread_status(next_candidate.id, "CLOSED")
    report = session_report(ledger, session)
    assert report["closed_spreads"] == 2
    assert report["net_realized"] == pytest.approx(ledger.session(session)["cash"] - 10000)
