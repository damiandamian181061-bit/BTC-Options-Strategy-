"""Dashboard API. The ledger is opened read-only; paper actions are queued for the worker.

Trade, close and halt requests never touch an execution adapter here. The single
paper writer consumes them, re-prices entries on fresh quotes, applies the Jev gate
and the independent risk check, and records the outcome in the ledger.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import greeks
from .domain import MarketSnapshot, utc_now
from .operations import queue_command, read_controls, read_json, write_controls
from .setup_api import bounded_body, register_setup

ACTIVITY = (
    "command",
    "risk",
    "entry_blocked",
    "jev",
    "jev_advisory",
    "management_error",
    "settlement",
    "forecast_unavailable",
)


def latest_snapshot(runtime):
    path = runtime / "latest.json"
    try:
        return MarketSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def spread_pnl(orders, settlements, held, snapshot):
    """Cash already exchanged plus the executable value of what is still held."""
    value = sum(
        (o["average"] * o["filled"] if o["side"] == "sell" else -o["average"] * o["filled"])
        - o["fees"]
        for o in orders
    )
    value += sum(cash - fee for cash, fee in settlements)
    for name, amount in held.items():
        quote = snapshot.quotes.get(name) if snapshot else None
        if abs(amount) < 1e-9:
            continue
        if quote is None or not quote.valid:
            return None
        value += amount * (quote.bid if amount > 0 else quote.ask)
    return value


def create_app(
    runtime: Path,
    frontend: Path | None = None,
    *,
    managed=False,
    controls=False,
):
    app = FastAPI(title="BTC Options", docs_url="/api/docs", redoc_url=None)
    controls_enabled = managed or controls
    if controls_enabled:
        # Blocks DNS rebinding; the Origin check alone would match a rebound host.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    register_setup(app, runtime, managed)

    def connect():
        path = runtime / "ledger.sqlite"
        if not path.exists():
            raise HTTPException(404, "No sessions yet; start paper trading first")
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    def authorize(request):
        if not controls_enabled:
            raise HTTPException(403, "Trading controls are disabled for this dashboard")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if request.headers.get("origin") != expected:
            raise HTTPException(403, "Requests must originate from this local dashboard")

    def paper_session(db, session):
        row = db.execute(
            "SELECT * FROM sessions WHERE id=?"
            if session
            else "SELECT * FROM sessions ORDER BY created DESC LIMIT 1",
            (session,) if session else (),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown session")
        return row

    async def json_body(request):
        try:
            body = json.loads(await bounded_body(request, 10000) or b"{}")
        except ValueError:
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Invalid request")
        return body

    def queue(request, payload):
        authorize(request)
        pointer = read_json(runtime / "services" / "paper-session.json")
        with closing(connect()) as db:
            row = paper_session(db, pointer.get("session") if pointer else None)
        if row["mode"] != "paper":
            raise HTTPException(403, "Dashboard trading is paper-only; use the CLI for exchanges")
        return {"queued": True, **queue_command(runtime, payload)}

    @app.get("/api/sessions")
    def sessions():
        if not (runtime / "ledger.sqlite").exists():
            return []
        with closing(connect()) as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,mode,created,initial,cash,halt FROM sessions ORDER BY created DESC"
                )
            ]

    @app.get("/api/state")
    def state(session: str | None = None):
        snapshot = latest_snapshot(runtime)
        with closing(connect()) as db:
            row = paper_session(db, session)
            sid = row["id"]

            def latest(kind, limit=1):
                rows = db.execute(
                    "SELECT timestamp,payload FROM events WHERE session=? AND kind=? "
                    "ORDER BY seq DESC LIMIT ?",
                    (sid, kind, limit),
                ).fetchall()
                return [{"timestamp": r[0], **json.loads(r[1])} for r in rows]

            equity_rows = db.execute(
                "SELECT timestamp,payload FROM events WHERE session=? AND kind='equity' "
                "ORDER BY seq DESC LIMIT 2000",
                (sid,),
            ).fetchall()[::-1]
            series = [[r[0], json.loads(r[1])["equity"]] for r in equity_rows]
            series = series[:: max(1, len(series) // 300)] + series[-1:]
            activity = [
                {"timestamp": r[0], "kind": r[1], **json.loads(r[2])}
                for r in db.execute(
                    f"SELECT timestamp,kind,payload FROM events WHERE session=? AND kind IN "
                    f"({','.join('?' * len(ACTIVITY))}) ORDER BY seq DESC LIMIT 40",
                    (sid, *ACTIVITY),
                )
            ]
            positions = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT instrument,amount FROM positions WHERE session=? AND ABS(amount)>1e-9",
                    (sid,),
                )
            }
            orders = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM orders WHERE session=? ORDER BY rowid", (sid,)
                )
            ]
            settlements = db.execute(
                "SELECT candidate_id,cashflow,fee FROM settlements WHERE session=?", (sid,)
            ).fetchall()
            spread_rows = db.execute(
                "SELECT * FROM spreads WHERE session=? ORDER BY opened DESC LIMIT 25", (sid,)
            ).fetchall()
            worker = latest("worker_status")
            jev_status = latest("jev_status")
            scan = latest("candidate_scan")
            judgments = latest("jev", 50)
        config = json.loads(row["config"])
        if worker:
            worker = worker[0]
            age = (utc_now() - datetime.fromisoformat(worker["timestamp"])).total_seconds()
            worker.update(
                age_seconds=age,
                responsive=worker["state"] in ("starting", "running") and 0 <= age <= 30,
            )
        else:
            worker = None

        legs, total, missing = (
            greeks.portfolio(positions, snapshot) if snapshot else ({}, greeks.total([]), [])
        )
        position_rows = []
        for name, amount in positions.items():
            instrument = snapshot.instruments.get(name) if snapshot else None
            quote = snapshot.quotes.get(name) if snapshot else None
            position_rows.append(
                {
                    "instrument": name,
                    "amount": amount,
                    "strike": instrument.strike if instrument else None,
                    "expiry": instrument.expiry.isoformat() if instrument else None,
                    "option_type": instrument.option_type if instrument else None,
                    "bid": quote.bid if quote else None,
                    "ask": quote.ask if quote else None,
                    "mark": quote.mark if quote else None,
                    "greeks": legs[name].model_dump() if name in legs else None,
                    "mid_iv": greeks.mid_iv(instrument, quote, snapshot.asof)
                    if instrument and quote
                    else None,
                }
            )

        spreads = []
        for r in spread_rows:
            candidate = json.loads(r["candidate"])
            mine = [o for o in orders if o["candidate_id"] == r["id"]]
            held = {}
            for o in mine:
                held[o["instrument"]] = held.get(o["instrument"], 0) + (
                    o["filled"] if o["side"] == "buy" else -o["filled"]
                )
            settled = [(c, f) for cid, c, f in settlements if cid == r["id"]]
            for name in [n for n in held if n not in positions]:
                held[name] = 0  # Settled at expiry; its cashflow is in `settled`.
            live = (
                greeks.portfolio({n: a for n, a in held.items() if abs(a) > 1e-9}, snapshot)[1]
                if snapshot and r["status"] != "CLOSED"
                else None
            )
            spreads.append(
                {
                    "id": r["id"],
                    "status": r["status"],
                    "opened": r["opened"],
                    "reserved": r["reserved"],
                    "candidate": candidate,
                    "held": {n: a for n, a in held.items() if abs(a) > 1e-9},
                    "greeks": live.model_dump() if live else None,
                    "pnl": spread_pnl(mine, settled, held, snapshot) if mine else 0.0,
                }
            )

        verdicts = {}
        for j in reversed(judgments):
            verdicts[j["candidate_id"]] = j
        ideas = []
        if scan:
            for c in scan[0].get("candidates", []):
                ideas.append({**c, "jev": verdicts.get(c["id"])})

        quote_age = (utc_now() - snapshot.asof).total_seconds() if snapshot else None
        index = next(iter(snapshot.quotes.values())).index if snapshot and snapshot.quotes else None
        return {
            "session": {
                "id": sid,
                "mode": row["mode"],
                "created": row["created"],
                "initial": row["initial"],
                "cash": row["cash"],
                "halt": row["halt"],
                "risk": config.get("risk", {}),
                "max_holding_hours": config.get("max_holding_hours"),
                "take_profit_fraction": config.get("take_profit_fraction"),
                "stop_credit_multiple": config.get("stop_credit_multiple"),
            },
            "equity": series[-1][1] if series else row["last_equity"] or row["cash"],
            "equity_series": series,
            "worker": worker,
            "controls": {
                **read_controls(runtime, config.get("jev_mode", "shadow")),
                "enabled": controls_enabled and row["mode"] == "paper",
            },
            "jev": jev_status[0] if jev_status else None,
            "market": snapshot
            and {
                "asof": snapshot.asof.isoformat(),
                "age_seconds": quote_age,
                "fresh": 0 <= quote_age <= config.get("max_quote_age_seconds", 15)
                and any(q.valid for q in snapshot.quotes.values()),
                "source": snapshot.source,
                "index": index,
                "quotes": len(snapshot.quotes),
            },
            "portfolio": {"greeks": total.model_dump(), "missing": missing},
            "positions": position_rows,
            "spreads": spreads,
            "scan": {
                "timestamp": scan[0]["timestamp"] if scan else None,
                "count": scan[0].get("count", 0) if scan else 0,
                "candidates": ideas,
            },
            "activity": activity,
            "server_time": utc_now().isoformat(),
        }

    @app.get("/api/chain")
    def chain():
        """Every option in the latest snapshot, nearest expiry first, then by strike."""
        snapshot = latest_snapshot(runtime)
        if snapshot is None:
            return {"asof": None, "options": []}
        options = []
        for name, quote in snapshot.quotes.items():
            instrument = snapshot.instruments[name]
            options.append(
                {
                    "instrument": name,
                    "expiry": instrument.expiry.isoformat(),
                    "strike": instrument.strike,
                    "type": instrument.option_type,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "mark": quote.mark,
                    "iv": quote.iv,
                    "delta": quote.delta,
                    "valid": quote.valid,
                }
            )
        options.sort(key=lambda o: (o["expiry"], o["strike"], o["type"]))
        return {"asof": snapshot.asof.isoformat(), "options": options}

    @app.post("/api/controls")
    async def set_controls(request: Request):
        authorize(request)
        body = await json_body(request)
        auto, gate = body.get("auto_trade"), body.get("jev_gate")
        if not isinstance(auto, bool) or not isinstance(gate, bool):
            raise HTTPException(400, "auto_trade and jev_gate must be true or false")
        return write_controls(runtime, auto, gate)

    @app.post("/api/trade")
    async def trade(request: Request):
        authorize(request)
        body = await json_body(request)
        long, short = body.get("long"), body.get("short")
        if not all(isinstance(x, str) and 0 < len(x) < 80 for x in (long, short)):
            raise HTTPException(400, "Choose a spread from the latest scan")
        return queue(request, {"action": "enter", "long": long, "short": short})

    @app.post("/api/close")
    async def close(request: Request):
        authorize(request)
        body = await json_body(request)
        if not isinstance(body.get("spread"), str):
            raise HTTPException(400, "Choose an open spread")
        return queue(request, {"action": "close", "spread": body["spread"]})

    @app.post("/api/halt")
    async def halt(request: Request):
        return queue(request, {"action": "halt"})

    if frontend and frontend.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @app.get("/")
        def index():
            # Asset names are content-hashed; the shell must revalidate to pick up rebuilds.
            return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-cache"})

    return app
