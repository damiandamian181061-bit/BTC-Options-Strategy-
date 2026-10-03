import pytest

from btc_options.demo import forecast
from btc_options.execution import PaperAdapter
from btc_options.jev import Jev
from btc_options.service import TradingService


@pytest.mark.parametrize("mode,orders", [("shadow", 1), ("filter", 0)])
async def test_missing_jev_key_shadow_and_filter(
    settings, ledger, market, candidate, monkeypatch, mode, orders
):
    cfg = settings.model_copy(update={"jev_mode": mode})
    session = ledger.start(cfg, now=market.asof)
    monkeypatch.setattr("btc_options.service.candidates", lambda *args: [candidate])
    jev = Jev(key="")
    try:
        service = TradingService(cfg, ledger, session, PaperAdapter(ledger, session, cfg), jev)
        await service.step(market, forecast(market.asof))
        assert len(ledger.orders(session)) == orders
        row = ledger.db.execute(
            "SELECT payload FROM events WHERE session=? AND kind='jev'", (session,)
        ).fetchone()
        assert row is not None and "unavailable" in row[0]
    finally:
        await jev.close()


async def test_missing_forecast_still_records_surface(settings, ledger, session, market):
    service = TradingService(settings, ledger, session, PaperAdapter(ledger, session, settings))
    await service.step(market, None)
    assert not ledger.orders(session)
    assert (
        ledger.db.execute(
            "SELECT COUNT(*) FROM events WHERE session=? AND kind='surface'", (session,)
        ).fetchone()[0]
        == 1
    )


def test_midnight_loss_uses_previous_equity(settings, ledger, session, now):
    from datetime import timedelta

    ledger.observe_equity(session, 10000, now, settings.risk)
    assert (
        ledger.observe_equity(session, 9890, now + timedelta(days=1), settings.risk)
        == "daily loss limit"
    )


async def test_paper_run_reports_stopped_worker_without_authenticated_requests(
    settings, monkeypatch
):
    from btc_options.service import run
    from btc_options.storage import Ledger
    from btc_options.api import create_app
    import httpx

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    sid = await run(settings, seconds=0.05, synthetic=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings.runtime_dir)),
        base_url="http://localhost",
    ) as client:
        response = await client.get("/api/state", params={"session": sid})
    assert response.json()["worker"]["state"] == "stopped"
    assert response.json()["worker"]["responsive"] is False
    ledger = Ledger(settings.runtime_dir / "ledger.sqlite")
    assert ledger.session(sid)["mode"] == "paper"
    ledger.close()


async def test_invalid_context_does_not_block_required_exits(
    settings, ledger, session, market, candidate
):
    from datetime import timedelta
    from btc_options.execution import SpreadExecutor
    from conftest import advance

    adapter = PaperAdapter(ledger, session, settings)
    executor = SpreadExecutor(adapter, ledger, session)
    await executor.enter(candidate, market)
    for _ in range(3):
        market = advance(market)
        await executor.manage(market, settings)
    cfg = settings.model_copy(update={"event_calendar": str(settings.runtime_dir / "missing.json")})
    market = advance(market, seconds=timedelta(hours=25).total_seconds())
    service = TradingService(cfg, ledger, session, adapter)
    await service.step(market, None)
    order = ledger.orders(session)[-1]
    assert order.side == "buy" and order.instrument == candidate.short
    row = ledger.db.execute(
        "SELECT payload FROM events WHERE session=? AND kind='entry_blocked' ORDER BY seq DESC LIMIT 1",
        (session,),
    ).fetchone()
    assert "invalid event context" in row[0]
