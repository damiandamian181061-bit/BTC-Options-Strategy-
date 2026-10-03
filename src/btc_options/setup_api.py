"""Paper setup status and history import. The Jev key lives in .env, never in the browser."""

import io
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException, Request

from .domain import identifier, utc_now
from .operations import atomic_text, read_json, service_status
from .service import parse_prices

MAX_UPLOAD = 16 * 1024 * 1024


async def bounded_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, "Upload exceeds the allowed size")
    return bytes(body)


def register_setup(app, runtime: Path, managed=False):
    def authorize(request):
        if not managed:
            raise HTTPException(403, "Setup changes require the managed paper stack")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if request.headers.get("origin") != expected:
            raise HTTPException(403, "Setup changes must originate from this local dashboard")

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "dashboard", "setup_enabled": managed}

    @app.get("/api/setup")
    def setup():
        statuses = {role: service_status(runtime, role) for role in ("collector", "paper")}
        latest = read_json(runtime / "latest.json")
        market = {"fresh": False, "quotes": 0}
        if latest:
            try:
                age = (utc_now() - datetime.fromisoformat(latest["asof"])).total_seconds()
                market = {
                    "fresh": 0 <= age <= 15 and latest.get("source") == "production",
                    "quotes": len(latest.get("quotes", {})),
                    "age_seconds": age,
                    "source": latest.get("source"),
                }
                market["fresh"] = market["fresh"] and any(
                    q.get("valid", True) for q in latest.get("quotes", {}).values()
                )
            except (ValueError, KeyError, TypeError):
                pass
        progress = {
            "completed_days": 0,
            "required_days": 60,
            "ready": False,
            "reason": "Waiting for the paper worker to evaluate recorded or imported history.",
        }
        overlay = None
        path = runtime / "ledger.sqlite"
        pointer = read_json(runtime / "services" / "paper-session.json")
        if path.exists():
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    db.execute("PRAGMA query_only=ON")
                    sid = pointer.get("session") if pointer else None
                    if not sid:
                        row = db.execute(
                            "SELECT id FROM sessions ORDER BY created DESC LIMIT 1"
                        ).fetchone()
                        sid = row[0] if row else None
                    for kind in ("history_progress", "jev_status"):
                        row = db.execute(
                            "SELECT payload FROM events WHERE session=? AND kind=? ORDER BY seq DESC LIMIT 1",
                            (sid, kind),
                        ).fetchone()
                        if row:
                            if kind == "history_progress":
                                progress = json.loads(row[0])
                            else:
                                overlay = json.loads(row[0])
            except (sqlite3.DatabaseError, OSError, ValueError):
                pass
        receipt = read_json(runtime / "services" / "history-import.json")
        return {
            "managed": managed,
            "controls_enabled": managed,
            "mode": "paper" if managed else "cli",
            "services": statuses,
            "market": market,
            "history": progress,
            "history_import": receipt,
            # The worker reports whether it loaded TYPESAFE_API_KEY from .env.
            "jev": {
                "configured": bool(overlay and overlay.get("available")),
                "model": "jev-1.13.0",
            },
        }

    @app.post("/api/setup/history")
    async def import_history(request: Request):
        authorize(request)
        if request.headers.get("content-type", "").split(";")[0] != "text/csv":
            raise HTTPException(415, "Upload a UTF-8 timestamp,price CSV")
        raw = await bounded_body(request, MAX_UPLOAD)
        try:
            prices = parse_prices(io.StringIO(raw.decode("utf-8-sig")))
            if (prices.index > utc_now()).any():
                raise ValueError("future observation")
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise HTTPException(
                400,
                "Invalid CSV: use unique UTC timestamp,price rows with positive finite prices and no future observations",
            ) from None
        normalized = prices.rename("price").rename_axis("timestamp").to_csv()
        archive = runtime / "imports" / f"{identifier()}.csv"
        atomic_text(archive, normalized)
        atomic_text(runtime / "underlying.csv", normalized)
        receipt = {
            "observations": len(prices),
            "first": prices.index[0].isoformat(),
            "last": prices.index[-1].isoformat(),
            "imported_at": utc_now().isoformat(),
            "name": unquote(request.headers.get("x-file-name", "history.csv"))[:120],
        }
        atomic_text(runtime / "services" / "history-import.json", json.dumps(receipt))
        return {**receipt, "status": "imported; forecast validation follows in the paper worker"}
