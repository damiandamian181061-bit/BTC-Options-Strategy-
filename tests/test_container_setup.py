import json
import os

import httpx
import pytest

from btc_options.api import create_app
from btc_options.container import managed_session
from btc_options.jev import api_key, verify_key
from btc_options.operations import PaperLease, heartbeat, service_status
from btc_options.storage import Ledger


def client(settings, managed=True):
    app = create_app(settings.runtime_dir, managed=managed)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


def test_managed_restart_resumes_balance_and_halt(settings):
    sid = managed_session(settings)
    ledger = Ledger(settings.runtime_dir / "ledger.sqlite")
    ledger.halt(sid, "operator audit")
    ledger.close()
    assert managed_session(settings) == sid
    ledger = Ledger(settings.runtime_dir / "ledger.sqlite")
    assert ledger.session(sid)["halt"] == "operator audit"
    assert ledger.session(sid)["cash"] == 10000
    ledger.close()
    with pytest.raises(ValueError, match="original configuration"):
        managed_session(settings.model_copy(update={"paper_balance": 20000}))
    with pytest.raises(ValueError, match="paper mode only"):
        managed_session(settings.model_copy(update={"mode": "live", "strategy_capital": 10000}))


def test_managed_paper_has_only_one_writer(settings):
    path = settings.runtime_dir / "paper.lock"
    with PaperLease(path):
        with pytest.raises(ValueError, match="already owns"):
            with PaperLease(path):
                pass
    with PaperLease(path):
        pass


def test_heartbeat_distinguishes_crashed_and_running(settings):
    assert not service_status(settings.runtime_dir, "paper")["responsive"]
    heartbeat(settings.runtime_dir, "paper")
    assert service_status(settings.runtime_dir, "paper")["responsive"]
    heartbeat(settings.runtime_dir, "paper", "stopped")
    assert not service_status(settings.runtime_dir, "paper")["responsive"]


@pytest.mark.parametrize(
    "managed,origin", [(False, "http://localhost"), (True, "https://evil.example"), (True, None)]
)
async def test_setup_rejects_unmanaged_or_foreign_writes(settings, managed, origin):
    headers = {"Origin": origin} if origin else {}
    headers["Content-Type"] = "text/csv"
    async with client(settings, managed) as api:
        response = await api.post(
            "/api/setup/history", content="timestamp,price\n", headers=headers
        )
    assert response.status_code == 403
    assert not (settings.runtime_dir / "underlying.csv").exists()


def test_jev_key_comes_from_env_then_dotenv_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        'DERIBIT_CLIENT_SECRET=exchange-secret\n# comment\nexport TYPESAFE_API_KEY="from-file"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BTC_ENV_FILE", str(env))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DERIBIT_CLIENT_SECRET", raising=False)
    assert api_key() == "from-file"
    assert "DERIBIT_CLIENT_SECRET" not in os.environ
    env.write_text("TYPESAFE_API_KEY=rotated\n", encoding="utf-8")
    assert api_key() == "rotated"  # Re-read so edits apply without restarting.
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-environment")
    assert api_key() == "from-environment"
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("BTC_ENV_FILE", str(tmp_path / "missing.env"))
    assert api_key() == ""


async def test_setup_never_exposes_a_key_route(settings, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-private-key")
    async with client(settings) as api:
        state = await api.get("/api/setup")
        posted = await api.post(
            "/api/setup/jev", json={"key": "x"}, headers={"Origin": "http://localhost"}
        )
    assert "test-only-private-key" not in state.text
    assert posted.status_code in (404, 405)


@pytest.mark.parametrize("case", ["ok", "unauthorized", "wrong_model"])
async def test_connection_probe_validates_pinned_model_without_orders(case):
    def handle(request):
        packet = json.loads(request.content)
        assert packet["model"] == "jev-1.13.0"
        assert packet["state"]["mode"] == "paper"
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        return httpx.Response(
            401 if case == "unauthorized" else 200,
            json={
                "model": "jev-latest" if case == "wrong_model" else "jev-1.13.0",
                "answers": {"connection": {"type": "noul", "noul": 1}},
            },
        )

    if case == "ok":
        await verify_key("test-only", httpx.MockTransport(handle))
    else:
        with pytest.raises(ValueError):
            await verify_key("test-only", httpx.MockTransport(handle))


async def test_history_import_is_atomic_and_cannot_trade(settings):
    sid = managed_session(settings)
    data = "timestamp,price\n2026-01-01T00:00:00Z,90000\n2026-01-01T00:05:00Z,90010\n"
    async with client(settings) as api:
        response = await api.post(
            "/api/setup/history",
            content=data,
            headers={"Origin": "http://localhost", "Content-Type": "text/csv"},
        )
        assert response.status_code == 200 and response.json()["observations"] == 2
        status = await api.get("/api/setup")
        assert not status.json()["history"]["ready"]
    ledger = Ledger(settings.runtime_dir / "ledger.sqlite")
    assert not ledger.orders(sid) and ledger.session(sid)["cash"] == 10000
    assert len(list((settings.runtime_dir / "imports").glob("*.csv"))) == 1
    ledger.close()


@pytest.mark.parametrize(
    "data",
    [
        "timestamp,price\n2026-01-01T00:00:00Z,nan\n",
        "timestamp,price\n2026-01-01T00:00:00Z,90000\n2026-01-01T00:00:00Z,90001\n",
        "timestamp,price\n2999-01-01T00:00:00Z,90000\n",
        "timestamp,price\n",
    ],
)
async def test_bad_history_cannot_replace_existing_import(settings, data):
    target = settings.runtime_dir / "underlying.csv"
    target.parent.mkdir(exist_ok=True)
    target.write_text("retained history", encoding="utf-8")
    async with client(settings) as api:
        response = await api.post(
            "/api/setup/history",
            content=data,
            headers={"Origin": "http://localhost", "Content-Type": "text/csv"},
        )
    assert response.status_code == 400
    assert target.read_text(encoding="utf-8") == "retained history"


async def test_setup_rejects_large_upload_and_dns_rebinding(settings, monkeypatch):
    monkeypatch.setattr("btc_options.setup_api.MAX_UPLOAD", 16)
    async with client(settings) as api:
        response = await api.post(
            "/api/setup/history",
            content="x" * 17,
            headers={"Origin": "http://localhost", "Content-Type": "text/csv"},
        )
        assert response.status_code == 413
        assert (await api.get("/api/setup", headers={"Host": "evil.example"})).status_code == 400


async def test_managed_paper_waits_for_collector_without_private_or_rest_calls(
    settings, monkeypatch
):
    from btc_options.service import run

    async def fail(*args, **kwargs):
        raise AssertionError("managed paper must use the recorder")

    monkeypatch.setattr("btc_options.market.PublicDeribit.snapshot", fail)
    sid = await run(settings, seconds=0.01, recorded_only=True)
    ledger = Ledger(settings.runtime_dir / "ledger.sqlite")
    assert not ledger.orders(sid)
    ledger.close()
