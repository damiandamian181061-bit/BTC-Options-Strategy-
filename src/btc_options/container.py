"""Paper-only container entrypoints, health checks, and persistent session resume."""

import argparse
import asyncio
import json
import os
import signal
from contextlib import suppress
from pathlib import Path

from .config import Settings
from .domain import utc_now
from .operations import PaperLease, atomic_text, heartbeat, read_json, service_status


def settings():
    return Settings(
        runtime_dir=Path(os.getenv("BTC_RUNTIME_DIR", "/data")),
        mode="paper",
        paper_balance=float(os.getenv("BTC_PAPER_BALANCE", "10000")),
        jev_mode="shadow",
    )


def managed_session(cfg, new_session=False):
    from .storage import Ledger

    if cfg.mode != "paper":
        raise ValueError("container services authorize paper mode only")
    pointer = cfg.runtime_dir / "services" / "paper-session.json"
    previous = read_json(pointer)
    if pointer.exists() and previous is None and not new_session:
        raise ValueError("invalid saved session pointer; inspect before creating a new session")
    resume = previous["session"] if previous and not new_session else None
    ledger = Ledger(cfg.runtime_dir / "ledger.sqlite")
    try:
        sid = ledger.start(cfg, resume=resume)
        atomic_text(pointer, json.dumps({"session": sid, "mode": "paper"}))
        return sid
    finally:
        ledger.close()


async def managed_task(role, cfg, awaitable, session=None):
    async def pulse():
        while True:
            heartbeat(cfg.runtime_dir, role, session=session, mode="paper")
            await asyncio.sleep(5)

    pulse_task = asyncio.create_task(pulse())
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        heartbeat(cfg.runtime_dir, role, "failed", session=session, error=type(exc).__name__)
        raise
    finally:
        pulse_task.cancel()
        with suppress(asyncio.CancelledError):
            await pulse_task
        existing = service_status(cfg.runtime_dir, role)
        if existing.get("state") != "failed":
            heartbeat(cfg.runtime_dir, role, "stopped", session=session)


async def worker(role, new_session=False):
    cfg = settings()
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)
    if role == "collector":
        from .market import collect

        await managed_task(role, cfg, collect(cfg, seconds=float("inf")))
    else:
        from .service import run

        with PaperLease(cfg.runtime_dir / "services" / "paper.lock"):
            sid = managed_session(cfg, new_session)
            await managed_task(
                role, cfg, run(cfg, seconds=float("inf"), resume=sid, recorded_only=True), sid
            )


def healthy(role, runtime):
    if role == "dashboard":
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=3) as response:
            return response.status == 200
    if not service_status(runtime, role)["responsive"]:
        return False
    if role == "collector":
        latest = read_json(runtime / "latest.json")
        if not latest or latest.get("source") != "production" or not latest.get("quotes"):
            return False
        if not any(q.get("valid", True) for q in latest["quotes"].values()):
            return False
        from datetime import datetime

        return 0 <= (utc_now() - datetime.fromisoformat(latest["asof"])).total_seconds() <= 15
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Managed paper platform; live remains an armed CLI option"
    )
    parser.add_argument("role", choices=("dashboard", "collector", "paper", "health"))
    parser.add_argument("target", nargs="?", choices=("dashboard", "collector", "paper"))
    parser.add_argument("--new-session", action="store_true")
    args = parser.parse_args()
    cfg = settings()
    if args.role == "health":
        try:
            status = healthy(args.target, cfg.runtime_dir)
        except (OSError, ValueError, TypeError, KeyError):
            status = False
        raise SystemExit(0 if status else 1)
    if args.role == "dashboard":
        import uvicorn
        from .api import create_app

        front = Path(__file__).parent / "frontend_dist"
        if not front.exists():
            front = Path(__file__).resolve().parents[2] / "frontend" / "dist"
        app = create_app(cfg.runtime_dir, front, managed=True)
        uvicorn.run(app, host="0.0.0.0", port=8765, access_log=False)
        return
    try:
        asyncio.run(worker(args.role, args.new_session))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        heartbeat(cfg.runtime_dir, args.role, "failed", error=type(exc).__name__)
        raise


if __name__ == "__main__":
    main()
