import json
from datetime import timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from btc_options.api import create_app
from btc_options.forecasts import forecast, realized_variance
from btc_options.jev import Jev
from btc_options.live import LiveAdapter
from btc_options.market import BookSequence, PublicDeribit
from btc_options.replay import read_snapshots, session_report
from btc_options.validation import verify_evidence


def response(model="jev-1.13.0", confidence=0.9):
    return {
        "model": model,
        "answers": {
            "context": {
                "type": "choice",
                "choice": "TRADE",
                "probabilities": {"TRADE": 0.9, "SKIP": 0.1},
                "confidence": confidence,
            }
        },
    }


@pytest.mark.parametrize(
    "case", ["good", "missing", "uncertain", "wrong_version", "timeout", "malformed", "obsolete"]
)
async def test_jev_failures(case, candidate, now):
    async def handle(request):
        if case == "timeout":
            raise httpx.ReadTimeout("simulated")
        payload = response(
            model="jev-latest" if case == "wrong_version" else "jev-1.13.0",
            confidence=0.6 if case == "uncertain" else 0.9,
        )
        if case == "malformed":
            payload["answers"]["context"]["probabilities"]["TRADE"] = float("nan")
            return httpx.Response(200, content=json.dumps(payload))
        return httpx.Response(200, json=payload)

    model = Jev(key="" if case == "missing" else "fake-test", transport=httpx.MockTransport(handle))
    try:
        result = await model.evaluate(
            candidate,
            {"events": []},
            now=now + timedelta(seconds=20) if case == "obsolete" else now,
        )
        assert result.action == ("TRADE" if case == "good" else "SKIP")
        assert "fake-test" not in result.model_dump_json()
    finally:
        await model.close()


async def test_public_client_forbids_private():
    client = PublicDeribit(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"result": []}))
    )
    try:
        with pytest.raises(ValueError):
            await client.call("private/buy", instrument_name="test")
    finally:
        await client.close()


def test_live_unarmed_and_unreleased(ledger, session, settings, tmp_path):
    cfg = settings.model_copy(update={"mode": "live", "strategy_capital": 100})
    with pytest.raises(ValueError, match="arm"):
        LiveAdapter(ledger, session, cfg)
    with pytest.raises(ValueError, match="release"):
        LiveAdapter(ledger, session, cfg, armed=True)
    with pytest.raises(ValueError, match="missing release"):
        verify_evidence({}, cfg)


def test_orderbook_gap_requires_new_snapshot():
    book = BookSequence()
    book.apply(
        {"type": "snapshot", "change_id": 10, "bids": [["new", 100, 1]], "asks": [["new", 101, 1]]}
    )
    with pytest.raises(ValueError):
        book.apply(
            {"type": "change", "change_id": 12, "prev_change_id": 11, "bids": [], "asks": []}
        )
    assert book.sequence is None and not book.bids


def test_forecasts_causal(now):
    idx = pd.date_range(
        end=pd.Timestamp(now).floor("D") - pd.Timedelta(days=1), periods=100, tz="UTC"
    )
    rng = np.random.default_rng(7)
    rv = pd.Series(np.exp(rng.normal(-7, 0.25, 100)), index=idx)
    ret = pd.Series(rng.standard_t(8, 100) * 0.025, index=idx + pd.Timedelta(days=1))
    bundle = forecast(rv, ret, now)
    assert all(m.measure == "physical" for m in bundle.models)
    assert bundle.integrated(30) > 0
    assert bundle.log_return_scenarios().shape == (4096,)
    future = rv.copy()
    future.index += pd.Timedelta(days=2)
    with pytest.raises(ValueError, match="unavailable"):
        forecast(future, ret, now)


def test_realized_variance_excludes_missing_and_partial_days(now):
    index = pd.date_range(now - timedelta(days=2), now, freq="5min")
    prices = pd.Series(np.exp(np.arange(len(index)) * 0.001), index=index)
    rv = realized_variance(prices, now)
    assert len(rv) == 1
    with pytest.raises(ValueError):
        realized_variance(prices, now - timedelta(minutes=1))


async def test_dashboard_cannot_mutate(ledger, session, settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings.runtime_dir)),
        base_url="http://localhost",
    ) as client:
        state = await client.get("/api/state")
        mutation = await client.post("/api/state", json={"trade": True})
    assert not state.json()["controls"]["enabled"]
    assert mutation.status_code == 405
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings.runtime_dir)),
        base_url="http://localhost",
    ) as client:
        trade = await client.post(
            "/api/trade", json={"long": "a", "short": "b"}, headers={"Origin": "http://localhost"}
        )
    assert trade.status_code == 403
    assert ledger.session(session)["cash"] == 10000


def test_synthetic_replay_is_not_performance_evidence(tmp_path, market):
    path = tmp_path / "snapshots.jsonl"
    path.write_text(market.model_dump_json())
    with pytest.raises(ValueError, match="excludes"):
        read_snapshots(path)


def test_no_trades_no_fabricated_metrics(ledger, session):
    report = session_report(ledger, session)
    assert report["win_rate"] is None and report["profit_factor"] is None
    assert report["closed_spreads"] == 0 and not report["production_only"]


def test_degenerate_garch_is_reported_not_averaged():
    from btc_options.forecasts import garch_health

    realized = np.full(60, 0.4**2 / 365)
    healthy = {"nu": 6.0, "alpha[1]": 0.1, "beta[1]": 0.85}
    assert garch_health(healthy, realized * 1.2, realized) == []
    heavy = {**healthy, "nu": 2.1}
    assert any("tails" in r for r in garch_health(heavy, realized, realized))
    assert garch_health({**healthy, "beta[1]": 0.9}, realized, realized) == [
        "non-stationary persistence"
    ]
    assert garch_health(healthy, realized * 5, realized) == [
        "forecast exceeds 4x average realized variance"
    ]


def test_forecast_uses_implied_and_downside_history(now):
    idx = pd.date_range(
        end=pd.Timestamp(now).floor("D") - pd.Timedelta(days=1), periods=200, tz="UTC"
    )
    rng = np.random.default_rng(5)
    rv = pd.Series(np.exp(rng.normal(-7, 0.3, 200)), index=idx)
    ret = pd.Series(rng.standard_t(6, 200) * 0.025, index=idx + pd.Timedelta(days=1))
    implied = rv.rolling(7, min_periods=1).mean() * 1.1
    bundle = forecast(rv, ret, now, downside=rv / 2, implied=implied)
    models = {m.model: m for m in bundle.models}
    assert models["HAR-X"].healthy and models["HAR-X"].values["in_ensemble"] == 1
    assert models["persistence"].values["in_ensemble"] == 0
    assert np.all(bundle.daily_variance > 0) and len(bundle.daily_variance) == 60
    paths = bundle.return_paths(10.5, count=500)
    assert paths.shape == (11, 500)
    leaked = implied.copy()
    leaked.index += pd.Timedelta(days=3)
    with pytest.raises(ValueError, match="unavailable"):
        forecast(rv, ret, now, downside=rv / 2, implied=leaked)


def test_forecast_survives_an_old_coverage_gap(now):
    idx = pd.date_range(
        end=pd.Timestamp(now).floor("D") - pd.Timedelta(days=1), periods=150, tz="UTC"
    )
    rng = np.random.default_rng(8)
    rv = pd.Series(np.exp(rng.normal(-7, 0.25, 150)), index=idx).drop(idx[20])
    ret = pd.Series(rng.standard_t(8, 150) * 0.025, index=idx + pd.Timedelta(days=1))
    assert forecast(rv, ret, now).integrated(30) > 0
