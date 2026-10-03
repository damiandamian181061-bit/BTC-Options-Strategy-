"""Transactional execution ledger and append-only, partitioned market history."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import expiry_payoff
from .domain import MarketSnapshot, OrderIntent, OrderState, identifier, utc_now


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, mode TEXT NOT NULL, created TEXT NOT NULL,
          cash REAL NOT NULL, initial REAL NOT NULL, highwater REAL NOT NULL,
          day TEXT NOT NULL, day_equity REAL NOT NULL, halt TEXT, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT REFERENCES sessions(id),
          timestamp TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS spreads (
          id TEXT PRIMARY KEY, session TEXT REFERENCES sessions(id), status TEXT NOT NULL,
          candidate TEXT NOT NULL, reserved REAL NOT NULL, opened TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS positions (
          session TEXT REFERENCES sessions(id), instrument TEXT, amount REAL NOT NULL,
          PRIMARY KEY(session,instrument));
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, session TEXT REFERENCES sessions(id), payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills (
          id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(id), amount REAL NOT NULL,
          price REAL NOT NULL, fee REAL NOT NULL, timestamp TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence (
          id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settlements (
          session TEXT REFERENCES sessions(id), instrument TEXT, payload TEXT NOT NULL,
          cashflow REAL NOT NULL, fee REAL NOT NULL, PRIMARY KEY(session,instrument));
        """)
        if "last_equity" not in {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}:
            self.db.execute("ALTER TABLE sessions ADD COLUMN last_equity REAL NOT NULL DEFAULT 0")
            self.db.commit()
        if "candidate_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(settlements)")}:
            self.db.execute("ALTER TABLE settlements ADD COLUMN candidate_id TEXT")
            self.db.commit()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def start(self, settings, resume=None, now=None):
        now = now or utc_now()
        if resume:
            record = self.session(resume)
            if record["mode"] != settings.mode:
                raise ValueError("resuming across execution modes is forbidden")
            if json.loads(record["config"]) != settings.model_dump(mode="json"):
                raise ValueError("resume requires the original configuration")
            return resume
        capital = settings.paper_balance if settings.mode == "paper" else settings.strategy_capital
        if capital is None:
            raise ValueError("exchange sessions require configured strategy capital")
        sid = identifier()
        with self.transaction():
            self.db.execute(
                "INSERT INTO sessions(id,mode,created,cash,initial,highwater,day,day_equity,halt,config,last_equity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    settings.mode,
                    now.isoformat(),
                    capital,
                    capital,
                    capital,
                    now.date().isoformat(),
                    capital,
                    None,
                    settings.model_dump_json(),
                    capital,
                ),
            )
        return sid

    def session(self, sid):
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError("unknown session")
        return dict(row)

    def event(self, sid, kind, payload, timestamp=None):
        payload = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        with self.transaction():
            self.db.execute(
                "INSERT INTO events(session,timestamp,kind,payload) VALUES (?,?,?,?)",
                (sid, (timestamp or utc_now()).isoformat(), kind, json.dumps(payload)),
            )

    def halt(self, sid, reason):
        with self.transaction():
            self.db.execute("UPDATE sessions SET halt=COALESCE(halt,?) WHERE id=?", (reason, sid))

    def positions(self, sid):
        return {
            r["instrument"]: r["amount"]
            for r in self.db.execute(
                "SELECT * FROM positions WHERE session=? AND ABS(amount)>0.000000001", (sid,)
            )
        }

    def spreads(self, sid):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM spreads WHERE session=? AND status!='CLOSED'", (sid,)
            )
        ]

    def reserve(self, sid, candidate):
        # Risk check and reservation must be performed under this writer transaction.
        with self.transaction():
            session = self.session(sid)
            cfg = json.loads(session["config"])
            rows = self.spreads(sid)
            budget = min(session["cash"], session["highwater"]) * cfg["risk"]["aggregate"]
            if session["halt"] or len(rows) >= cfg["risk"]["max_positions"]:
                raise ValueError("reservation blocked by halt/position limit")
            reserved = max(candidate.worst_loss, candidate.partial_loss)
            if reserved + sum(r["reserved"] for r in rows) > budget + 1e-8:
                raise ValueError("aggregate risk reservation exceeded")
            self.db.execute(
                "INSERT INTO spreads VALUES (?,?,?,?,?,?)",
                (
                    candidate.id,
                    sid,
                    "OPENING",
                    candidate.model_dump_json(),
                    reserved,
                    candidate.created_at.isoformat(),
                ),
            )

    def spread_status(self, cid, status):
        with self.transaction():
            self.db.execute(
                "UPDATE spreads SET status=?,reserved=CASE WHEN ?='CLOSED' THEN 0 ELSE reserved END WHERE id=?",
                (status, status, cid),
            )

    def order(self, intent):
        with self.transaction():
            self.db.execute(
                "INSERT INTO orders VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (intent.id, intent.session, intent.model_dump_json()),
            )

    def orders(self, sid):
        return [
            OrderIntent.model_validate_json(r[0])
            for r in self.db.execute(
                "SELECT payload FROM orders WHERE session=? ORDER BY rowid", (sid,)
            )
        ]

    def fill(self, intent, fill_id, amount, price, fee, timestamp):
        if min(amount, price) <= 0 or fee < 0:
            raise ValueError("invalid fill")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM fills WHERE id=?", (fill_id,)).fetchone():
                return False
            old = self.db.execute("SELECT payload FROM orders WHERE id=?", (intent.id,)).fetchone()
            if old is None:
                raise ValueError("fill without persisted intent")
            current = OrderIntent.model_validate_json(old[0])
            if current.filled + amount > current.amount + 1e-9:
                raise ValueError("overfill")
            if current.state in (OrderState.FILLED, OrderState.REJECTED):
                raise ValueError("fill in terminal state")
            old_filled = current.filled
            current.filled += amount
            current.average = (current.average * old_filled + amount * price) / current.filled
            current.fees += fee
            current.state = (
                OrderState.FILLED
                if current.filled >= current.amount - 1e-9
                else OrderState.PARTIALLY_FILLED
            )
            signed = amount if current.side == "buy" else -amount
            self.db.execute(
                "INSERT INTO fills VALUES (?,?,?,?,?,?)",
                (fill_id, intent.id, amount, price, fee, timestamp.isoformat()),
            )
            self.db.execute(
                "INSERT INTO positions VALUES (?,?,?) ON CONFLICT(session,instrument) DO UPDATE SET amount=amount+excluded.amount",
                (intent.session, intent.instrument, signed),
            )
            self.db.execute(
                "UPDATE sessions SET cash=cash-? WHERE id=?", (signed * price + fee, intent.session)
            )
            self.db.execute(
                "UPDATE orders SET payload=? WHERE id=?", (current.model_dump_json(), current.id)
            )
            intent.filled, intent.average, intent.fees, intent.state = (
                current.filled,
                current.average,
                current.fees,
                current.state,
            )
        return True

    def equity(self, sid, snapshot):
        value = self.session(sid)["cash"]
        for name, amount in self.positions(sid).items():
            if name not in snapshot.quotes:
                raise ValueError(f"missing liquidation quote: {name}")
            quote = snapshot.quotes[name]
            value += amount * (quote.bid if amount > 0 else quote.ask)
        return value

    def settle(self, sid, record, instrument, fee):
        with self.transaction():
            if self.db.execute(
                "SELECT 1 FROM settlements WHERE session=? AND instrument=?", (sid, instrument.name)
            ).fetchone():
                return False
            row = self.db.execute(
                "SELECT amount FROM positions WHERE session=? AND instrument=?",
                (sid, instrument.name),
            ).fetchone()
            amount = row[0] if row else 0
            cashflow = amount * expiry_payoff(
                instrument.strike, record.delivery_price, instrument.option_type
            )
            candidates = [
                r
                for r in self.spreads(sid)
                if instrument.name
                in (json.loads(r["candidate"])["long"], json.loads(r["candidate"])["short"])
            ]
            if abs(amount) > 1e-9 and len(candidates) != 1:
                raise ValueError("settlement requires a uniquely attributable active spread")
            candidate_id = candidates[0]["id"] if candidates else None
            self.db.execute(
                "INSERT INTO settlements(session,instrument,payload,cashflow,fee,candidate_id) VALUES (?,?,?,?,?,?)",
                (sid, instrument.name, record.model_dump_json(), cashflow, fee, candidate_id),
            )
            self.db.execute(
                "UPDATE positions SET amount=0 WHERE session=? AND instrument=?",
                (sid, instrument.name),
            )
            self.db.execute("UPDATE sessions SET cash=cash+?-? WHERE id=?", (cashflow, fee, sid))
        self.event(sid, "settlement", record, record.confirmed_at)
        return True

    def observe_equity(self, sid, equity, now, policy):
        with self.transaction():
            row = self.session(sid)
            day_base = (
                row["day_equity"]
                if row["day"] == now.date().isoformat()
                else (row["last_equity"] or row["initial"])
            )
            highwater = max(row["highwater"], equity)
            halt = row["halt"]
            if equity <= day_base * (1 - policy.daily_loss):
                halt = halt or "daily loss limit"
            if equity <= highwater * (1 - policy.drawdown):
                halt = halt or "drawdown limit"
            self.db.execute(
                "UPDATE sessions SET highwater=?,day=?,day_equity=?,halt=?,last_equity=? WHERE id=?",
                (highwater, now.date().isoformat(), day_base, halt, equity, sid),
            )
        return halt


class MarketStore:
    def __init__(self, root: Path, batch_size=1):
        self.root = root
        self.batch_size = batch_size
        self.pending = []
        root.mkdir(parents=True, exist_ok=True)

    def append(self, channel, payload, timestamp=None):
        timestamp = timestamp or utc_now()
        if self.pending and self.pending[0]["received_at"][:10] != timestamp.strftime("%Y-%m-%d"):
            self.flush()
        record = dict(
            schema_version=1,
            received_at=timestamp.isoformat(),
            channel=channel,
            payload=json.dumps(payload),
        )
        self.pending.append(record)
        if len(self.pending) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        directory = self.root / self.pending[0]["received_at"][:10]
        directory.mkdir(exist_ok=True)
        target = directory / f"{identifier()}.parquet"
        temporary = target.with_suffix(".tmp")
        pq.write_table(pa.Table.from_pylist(self.pending), temporary, compression="zstd")
        os.replace(temporary, target)
        self.pending = []

    def snapshot(self, snapshot):
        self.append("snapshot", snapshot.model_dump(mode="json"), snapshot.asof)
        self.flush()
        target = self.root.parent / "latest.json"
        temporary = target.with_name(f"latest-{identifier()}.tmp")
        temporary.write_text(snapshot.model_dump_json(), encoding="utf-8")
        os.replace(temporary, target)

    def invalidate_latest(self):
        target = self.root.parent / "latest.json"
        if target.exists():
            try:
                snapshot = MarketSnapshot.model_validate_json(target.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.append("cache_error", {"reason": "invalid latest snapshot"})
                self.flush()
                return
            snapshot = snapshot.model_copy(
                update={
                    "id": identifier(),
                    "asof": utc_now(),
                    "quotes": {
                        n: q.model_copy(update={"valid": False}) for n, q in snapshot.quotes.items()
                    },
                }
            )
            self.snapshot(snapshot)

    def query(self, sql="SELECT * FROM market ORDER BY received_at"):
        files = list(self.root.rglob("*.parquet"))
        if not files:
            return []
        with duckdb.connect() as connection:
            connection.read_parquet([str(p) for p in files]).create_view("market")
            return connection.sql(sql).df()
