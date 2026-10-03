import httpx
import pytest

from btc_options.api import create_app
from btc_options.container import managed_session
from btc_options.demo import forecast
from btc_options.execution import PaperAdapter
from btc_options.jev import Jev
from btc_options.operations import queue_command, read_controls, take_commands
from btc_options.service import TradingService
from btc_options.storage import Ledger, MarketStore
from conftest import advance

ORIGIN = {"Origin": "http://localhost"}


def client(settings, controls=True):
    app = create_app(settings.runtime_dir, controls=controls)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


def service(settings, ledger, session, jev=None, **controls):
    value = TradingService(settings, ledger, session, PaperAdapter(ledger, session, settings), jev)
    value.auto_trade = controls.get("auto_trade", False)
    value.jev_gate = controls.get("jev_gate", False)
    return value


def last_command(ledger, session):
    import json

    row = ledger.db.execute(
        "SELECT payload FROM events WHERE session=? AND kind='command' ORDER BY seq DESC",
        (session,),
    ).fetchone()
    return json.loads(row[0])


async def test_manual_entry_is_repriced_and_risk_checked(
    settings, ledger, session, market, candidate, monkeypatch
):
    monkeypatch.setattr("btc_options.service.candidates", lambda *args: [candidate])
    trader = service(settings, ledger, session)
    await trader.step(market, forecast(market.asof))
    assert not ledger.orders(session)  # Auto-trade off: scan only.
    command = {"id": "c1", "action": "enter", "long": candidate.long, "short": candidate.short}
    await trader.command(command, market, forecast(market.asof))
    assert last_command(ledger, session)["status"] == "accepted"
    assert ledger.orders(session)[0].instrument == candidate.long
    await trader.command({**command, "id": "c2"}, market, forecast(market.asof))
    assert last_command(ledger, session)["reason"] == "position limit reached"


async def test_jev_gate_blocks_manual_entry_without_approval(
    settings, ledger, session, market, candidate, monkeypatch
):
    monkeypatch.setattr("btc_options.service.candidates", lambda *args: [candidate])
    cfg = settings.model_copy(update={"jev_mode": "shadow"})
    jev = Jev(key="")
    try:
        trader = service(cfg, ledger, session, jev, jev_gate=True)
        command = {"action": "enter", "long": candidate.long, "short": candidate.short}
        await trader.command(command, market, forecast(market.asof))
    finally:
        await jev.close()
    assert last_command(ledger, session)["reason"] == "Jev did not approve this trade"
    assert not ledger.orders(session)


async def test_manual_entry_requires_forecast_and_matching_spread(
    settings, ledger, session, market, candidate, monkeypatch
):
    trader = service(settings, ledger, session)
    command = {"action": "enter", "long": candidate.long, "short": candidate.short}
    await trader.command(command, market, None)
    assert "warming up" in last_command(ledger, session)["reason"]
    monkeypatch.setattr("btc_options.service.candidates", lambda *args: [])
    await trader.command(command, market, forecast(market.asof))
    assert "no longer passes" in last_command(ledger, session)["reason"]


async def test_close_and_halt_commands_exit_short_first(
    settings, ledger, session, market, candidate
):
    trader = service(settings, ledger, session)
    await trader.executor.enter(candidate, market)
    for _ in range(3):
        market = advance(market)
        await trader.executor.manage(market, settings)
    assert ledger.spreads(session)[0]["status"] == "OPEN"
    await trader.command({"action": "close", "spread": "nope"}, market, None)
    assert last_command(ledger, session)["status"] == "rejected"
    await trader.command({"action": "close", "spread": candidate.id}, market, None)
    await trader.executor.manage(advance(market), settings)
    order = ledger.orders(session)[-1]
    assert order.side == "buy" and order.instrument == candidate.short
    await trader.command({"action": "halt"}, market, None)
    assert ledger.session(session)["halt"] == "operator kill switch"


def test_command_queue_is_consumed_once(settings):
    queue_command(settings.runtime_dir, {"action": "halt"})
    queue_command(settings.runtime_dir, {"action": "close", "spread": "x"})
    taken = take_commands(settings.runtime_dir)
    assert [c["action"] for c in taken] == ["halt", "close"]
    assert take_commands(settings.runtime_dir) == []


async def test_controls_and_trade_requests_are_guarded(settings):
    managed_session(settings)
    async with client(settings, controls=False) as api:
        assert (await api.post("/api/halt", headers=ORIGIN)).status_code == 403
    async with client(settings) as api:
        foreign = {"Origin": "http://evil.example"}
        assert (await api.post("/api/halt", headers=foreign)).status_code == 403
        rebound = {"Host": "evil.example", "Origin": "http://evil.example"}
        assert (await api.post("/api/halt", headers=rebound)).status_code == 400
        bad = await api.post("/api/controls", json={"auto_trade": "yes"}, headers=ORIGIN)
        assert bad.status_code == 400
        ok = await api.post(
            "/api/controls", json={"auto_trade": False, "jev_gate": True}, headers=ORIGIN
        )
        assert ok.json() == {"auto_trade": False, "jev_gate": True}
        trade = await api.post("/api/trade", json={"long": "L", "short": "S"}, headers=ORIGIN)
        assert trade.status_code == 200
        state = (await api.get("/api/state")).json()
    assert state["controls"] == {"auto_trade": False, "jev_gate": True, "enabled": True}
    assert read_controls(settings.runtime_dir)["jev_gate"]
    [command] = take_commands(settings.runtime_dir)
    assert command["action"] == "enter" and command["long"] == "L"


async def test_live_sessions_cannot_be_traded_from_dashboard(settings):
    cfg = settings.model_copy(update={"mode": "live", "strategy_capital": 1000})
    ledger = Ledger(settings.runtime_dir / "ledger.sqlite")
    ledger.start(cfg)
    ledger.close()
    async with client(settings) as api:
        response = await api.post("/api/halt", headers=ORIGIN)
    assert response.status_code == 403
    assert take_commands(settings.runtime_dir) == []


async def test_state_reports_position_greeks_and_pnl(settings, ledger, session, market, candidate):
    trader = service(settings, ledger, session)
    await trader.executor.enter(candidate, market)
    for _ in range(3):
        market = advance(market)
        await trader.executor.manage(market, settings)
    MarketStore(settings.runtime_dir / "market").snapshot(market)
    async with client(settings) as api:
        state = (await api.get("/api/state")).json()
    [spread] = state["spreads"]
    assert spread["status"] == "OPEN"
    assert spread["held"] == {candidate.long: 0.01, candidate.short: -0.01}
    assert spread["greeks"]["delta"] > 0 and spread["greeks"]["theta"] > 0
    # Entered at the touch and marked at the touch: P&L is minus spread and fees.
    assert spread["pnl"] < 0
    assert spread["pnl"] == pytest.approx(
        state["equity"] - 10000 if state["equity_series"] else spread["pnl"]
    )
    legs = {p["instrument"]: p for p in state["positions"]}
    assert legs[candidate.short]["greeks"]["delta"] > 0  # Short put.
    assert legs[candidate.short]["mid_iv"] > 0
    total = state["portfolio"]["greeks"]
    assert total["vega"] == pytest.approx(sum(p["greeks"]["vega"] for p in legs.values()))


async def test_chain_lists_every_quote_sorted(settings, market):
    async with client(settings) as api:
        assert (await api.get("/api/chain")).json() == {"asof": None, "options": []}
    MarketStore(settings.runtime_dir / "market").snapshot(market)
    async with client(settings) as api:
        chain = (await api.get("/api/chain")).json()
    options = chain["options"]
    assert len(options) == len(market.quotes)
    keys = [(o["expiry"], o["strike"]) for o in options]
    assert keys == sorted(keys)
    assert all(o["type"] in ("call", "put") and o["ask"] >= o["bid"] for o in options)
