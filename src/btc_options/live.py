"""Explicitly armed Deribit adapter. Ambiguous submissions are never retried."""

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

import httpx

from .domain import OrderState, SpreadCandidate, utc_now
from .market import from_ms


def policy_hash(settings):
    state = settings.model_dump(mode="json")
    state.pop("runtime_dir")
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def implementation_hash():
    digest = hashlib.sha256()
    for file in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def check_release(path, settings, now=None):
    from .validation import verify_evidence

    now = now or utc_now()
    if path is None or not path.is_file():
        raise ValueError("live requires a release manifest produced from validation evidence")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("policy_hash") != policy_hash(settings):
        raise ValueError("release policy does not match this exact live configuration")
    if data.get("implementation_hash") != implementation_hash():
        raise ValueError("implementation changed after validation")
    if from_ms(data["created_ms"]) < now - timedelta(days=7):
        raise ValueError("release manifest expired")
    verify_evidence(data["evidence"], settings)


class LiveAdapter:
    def __init__(
        self,
        ledger,
        session,
        settings,
        *,
        armed=False,
        release=None,
        transport=None,
        recovery_only=False,
    ):
        if settings.mode not in ("testnet", "live") or not armed:
            raise ValueError("exchange execution requires explicit CLI --arm")
        if not settings.strategy_capital:
            raise ValueError("strategy capital must be explicitly configured")
        if settings.mode == "live" and not recovery_only:
            check_release(release, settings)
        prefix = "DERIBIT_TESTNET" if settings.mode == "testnet" else "DERIBIT"
        self.client_id = os.environ.get(prefix + "_CLIENT_ID", "")
        self.secret = os.environ.get(prefix + "_CLIENT_SECRET", "")
        if not self.client_id or not self.secret:
            raise ValueError(f"missing {prefix} credentials")
        self.ledger, self.session, self.settings = ledger, session, settings
        if ledger.session(session)["mode"] != settings.mode:
            raise ValueError("adapter/session execution mode mismatch")
        self.recovery_only = recovery_only
        host = "test.deribit.com" if settings.mode == "testnet" else "www.deribit.com"
        self.base = f"https://{host}/api/v2/"
        self.client = httpx.AsyncClient(timeout=15, transport=transport)
        self.token = None
        self.token_until = utc_now()
        self.ready = False

    async def close(self):
        await self.client.aclose()

    async def rpc(self, method, **params):
        if method.startswith("private/") and (not self.token or utc_now() >= self.token_until):
            auth = await self.rpc(
                "public/auth",
                grant_type="client_credentials",
                client_id=self.client_id,
                client_secret=self.secret,
                scope="account:read trade:read_write wallet:none",
            )
            scopes = auth["scope"].split()
            if (
                any(s.startswith("wallet:") and s != "wallet:none" for s in scopes)
                or "trade:read_write" not in scopes
            ):
                raise ValueError("auth scope exceeds allowed permissions or lacks trading")
            self.token = auth["access_token"]
            self.token_until = utc_now() + timedelta(seconds=max(1, auth["expires_in"] - 30))
        headers = {"Authorization": f"Bearer {self.token}"} if method.startswith("private/") else {}
        # JSON-RPC POST keeps secrets out of URLs and access logs.
        response = await self.client.post(
            self.base + method,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise ValueError(f"Deribit rejected request: code {data['error'].get('code')}")
        return data["result"]

    async def initialize(self, snapshot):
        summary = await self.rpc("private/get_account_summary", currency="USDC", extended=True)
        if (
            not self.recovery_only
            and not self.ledger.spreads(self.session)
            and summary["available_funds"] < self.settings.strategy_capital
        ):
            raise ValueError("insufficient available strategy capital")
        external = await self.rpc("private/get_open_orders_by_currency", currency="USDC")
        owned = {o.id for o in self.ledger.orders(self.session)}
        if any(o.get("label") not in owned for o in external):
            raise ValueError("unmanaged USDC orders: use a dedicated subaccount")
        self.ready = True
        await self.advance(snapshot)
        await self.reconcile(snapshot)

    async def submit(self, intent, snapshot):
        from .domain import OrderIntent

        existing = self.ledger.db.execute(
            "SELECT payload FROM orders WHERE id=?", (intent.id,)
        ).fetchone()
        if existing:
            return OrderIntent.model_validate_json(existing[0])
        if not self.ready:
            raise ValueError("adapter not initialized/reconciled")
        q, instrument = snapshot.quotes[intent.instrument], snapshot.instruments[intent.instrument]
        if instrument.settlement != "USDC" or instrument.contract_size != 1:
            raise ValueError("unsupported execution units")
        if (utc_now() - q.exchange_time).total_seconds() > self.settings.max_quote_age_seconds:
            raise ValueError("stale execution quote")
        if self.settings.mode == "live" and snapshot.source != "production":
            raise ValueError("production source required")
        if (
            abs(
                intent.amount / instrument.amount_step
                - round(intent.amount / instrument.amount_step)
            )
            > 1e-7
        ):
            raise ValueError("invalid amount increment")
        positions = self.ledger.positions(self.session)
        position = positions.get(intent.instrument, 0)
        reducing = (intent.side == "buy" and position < 0) or (
            intent.side == "sell" and position > 0
        )
        if self.recovery_only and not reducing:
            raise ValueError("recovery adapter forbids new exposure")
        if not reducing and self.ledger.session(self.session)["halt"]:
            raise ValueError("entry halted")
        if intent.side == "sell" and not reducing:
            row = self.ledger.db.execute(
                "SELECT candidate FROM spreads WHERE id=? AND session=?",
                (intent.candidate_id, self.session),
            ).fetchone()
            if row is None:
                raise ValueError("unreserved short")
            c = SpreadCandidate.model_validate_json(row[0])
            if (
                intent.instrument != c.short
                or positions.get(c.long, 0) + position < intent.amount - 1e-9
            ):
                raise ValueError("short would exceed confirmed protection")
        if reducing and intent.amount > abs(position) + 1e-9:
            raise ValueError("close would reverse position")
        if (
            abs(
                intent.limit / instrument.tick(intent.limit)
                - round(intent.limit / instrument.tick(intent.limit))
            )
            > 1e-7
        ):
            raise ValueError("invalid price tick")
        self.ledger.order(intent)  # Durable intent BEFORE transport.
        try:
            if not reducing:
                margin = await self.rpc(
                    "private/get_margins",
                    instrument_name=intent.instrument,
                    amount=intent.amount,
                    price=intent.limit,
                )
                summary = await self.rpc("private/get_account_summary", currency="USDC")
                needed = margin["buy" if intent.side == "buy" else "sell"]
                needed = (
                    needed.get("initial_margin", needed) if isinstance(needed, dict) else needed
                )
                if (
                    needed > summary["available_funds"]
                    or summary["initial_margin"] + needed
                    > self.settings.strategy_capital * self.settings.risk.margin_utilization
                ):
                    raise ValueError("exchange margin exceeds allocated budget")
            data = await self.rpc(
                "private/" + intent.side,
                instrument_name=intent.instrument,
                amount=intent.amount,
                type="limit",
                price=intent.limit,
                time_in_force="immediate_or_cancel",
                label=intent.id,
                reduce_only=reducing,
                post_only=False,
            )
            await self._apply(intent, data["order"], data.get("trades", []))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            intent.state = OrderState.RECONCILING
            self.ledger.order(intent)
            self.ledger.halt(self.session, "exchange submission requires reconciliation")
        return intent

    async def _apply(self, intent, order, trades):
        intent.exchange_id = order["order_id"]
        self.ledger.order(intent)
        for trade in trades:
            if trade.get("fee_currency", "USDC") != "USDC":
                raise ValueError("unexpected fee currency")
            self.ledger.fill(
                intent,
                "exchange:" + str(trade["trade_id"]),
                trade["amount"],
                trade["price"],
                trade["fee"],
                from_ms(trade["timestamp"]),
            )
        if abs(intent.filled - order["filled_amount"]) > 1e-8:
            intent.state = OrderState.RECONCILING
            self.ledger.order(intent)
            raise ValueError("trade history does not reconcile filled amount")
        states = {
            "open": OrderState.ACKNOWLEDGED,
            "filled": OrderState.FILLED,
            "cancelled": OrderState.CANCELLED,
            "rejected": OrderState.REJECTED,
        }
        intent.state = states.get(order["order_state"], OrderState.RECONCILING)
        self.ledger.order(intent)

    async def advance(self, snapshot):
        for intent in self.ledger.orders(self.session):
            if intent.state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED):
                continue
            if intent.exchange_id:
                order = await self.rpc("private/get_order_state", order_id=intent.exchange_id)
            else:
                orders = await self.rpc(
                    "private/get_order_state_by_label", currency="USDC", label=intent.id
                )
                if len(orders) != 1:
                    # Empty recent history does NOT prove the original order was not accepted.
                    self.ledger.halt(
                        self.session, "ambiguous order label; historical audit required"
                    )
                    continue
                order = orders[0]
            trades = await self.rpc("private/get_user_trades_by_order", order_id=order["order_id"])
            trades = trades.get("trades", []) if isinstance(trades, dict) else trades
            await self._apply(intent, order, trades)

    async def cancel(self, intent):
        if intent.state == OrderState.FILLED:
            return
        await self.rpc("private/cancel_by_label", currency="USDC", label=intent.id)
        intent.state = OrderState.RECONCILING
        self.ledger.order(intent)

    async def reconcile(self, snapshot):
        external = await self.rpc("private/get_positions", currency="USDC")
        expected = self.ledger.positions(self.session)
        actual = {p["instrument_name"]: p["size"] for p in external if abs(p["size"]) > 1e-9}
        if any(
            abs(actual.get(n, 0) - expected.get(n, 0)) > 1e-8 for n in set(actual) | set(expected)
        ):
            self.ledger.halt(self.session, "exchange position mismatch")
            raise ValueError("exchange position mismatch; reconcile before further submission")
        summary = await self.rpc("private/get_account_summary", currency="USDC", extended=True)
        balance = float(summary["balance"])
        cash = self.ledger.session(self.session)["cash"]
        anchor = self.ledger.db.execute(
            "SELECT payload FROM events WHERE session=? AND kind='account_anchor' ORDER BY seq LIMIT 1",
            (self.session,),
        ).fetchone()
        if anchor is None:
            self.ledger.event(
                self.session,
                "account_anchor",
                {"balance": balance, "ledger_cash": cash},
                snapshot.asof,
            )
        else:
            baseline = json.loads(anchor[0])
            if abs((balance - baseline["balance"]) - (cash - baseline["ledger_cash"])) > 0.0001:
                self.ledger.halt(
                    self.session, "exchange cash mismatch; account-event audit required"
                )
                raise ValueError("exchange cash mismatch")
        self.ledger.event(
            self.session,
            "account",
            {
                k: summary[k]
                for k in ("balance", "equity", "available_funds", "initial_margin")
                if k in summary
            },
            snapshot.asof,
        )
        self.ledger.event(
            self.session, "reconciliation", {"ok": True, "positions": actual}, snapshot.asof
        )
