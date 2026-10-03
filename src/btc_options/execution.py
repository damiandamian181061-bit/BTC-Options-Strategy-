"""Persistent limit/IOC lifecycle with conservative, delayed depth consumption."""

from datetime import timedelta
from math import ceil, floor
from typing import Protocol

from .contracts import expiry_payoff
from .domain import OrderIntent, OrderState, SpreadCandidate, identifier
from .strategy import fee


class ExecutionAdapter(Protocol):
    async def submit(self, intent, snapshot): ...
    async def advance(self, snapshot): ...
    async def cancel(self, intent): ...
    async def reconcile(self, snapshot): ...


def intent_for(session, candidate_id, instrument, side, amount, snapshot):
    q, i = snapshot.quotes[instrument], snapshot.instruments[instrument]
    price = q.ask if side == "buy" else q.bid
    tick = i.tick(price)
    limit = (ceil(price / tick) if side == "buy" else floor(price / tick)) * tick
    if limit <= 0:
        raise ValueError("no executable price")
    return OrderIntent(
        session=session,
        candidate_id=candidate_id,
        instrument=instrument,
        side=side,
        amount=amount,
        limit=limit,
        created_at=snapshot.asof,
    )


class PaperAdapter:
    def __init__(self, ledger, session, settings):
        if settings.mode != "paper":
            raise ValueError("paper adapter cannot own exchange sessions")
        self.ledger, self.session, self.settings = ledger, session, settings

    async def submit(self, intent, snapshot):
        existing = self.ledger.db.execute(
            "SELECT payload FROM orders WHERE id=?", (intent.id,)
        ).fetchone()
        if existing:
            return OrderIntent.model_validate_json(existing[0])
        positions = self.ledger.positions(self.session)
        holding = positions.get(intent.instrument, 0)
        if (
            (intent.side == "sell" and holding > 0) or (intent.side == "buy" and holding < 0)
        ) and intent.amount > abs(holding) + 1e-9:
            raise ValueError("reducing order would reverse simulated position")
        if intent.side == "sell" and positions.get(intent.instrument, 0) <= 0:
            row = self.ledger.db.execute(
                "SELECT candidate FROM spreads WHERE id=? AND session=?",
                (intent.candidate_id, self.session),
            ).fetchone()
            if row is None:
                raise ValueError("short requires reserved spread")
            c = SpreadCandidate.model_validate_json(row[0])
            if (
                intent.instrument != c.short
                or positions.get(c.long, 0) + positions.get(c.short, 0) < intent.amount - 1e-9
            ):
                raise ValueError("short exceeds filled protection")
        if intent.side == "buy" and positions.get(intent.instrument, 0) >= 0:
            cost = intent.limit * intent.amount + fee(
                intent.limit, snapshot.quotes[intent.instrument].index, intent.amount, self.settings
            )
            if cost > self.ledger.session(self.session)["cash"]:
                raise ValueError("insufficient simulated cash")
        intent.state = OrderState.ACKNOWLEDGED
        self.ledger.order(intent)
        return intent

    async def cancel(self, intent):
        if intent.state != OrderState.FILLED:
            intent.state = OrderState.CANCELLED
            self.ledger.order(intent)

    async def advance(self, snapshot):
        consumed = {}
        for intent in self.ledger.orders(self.session):
            if intent.state not in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
                continue
            due = intent.created_at + timedelta(milliseconds=self.settings.latency_ms)
            q = snapshot.quotes.get(intent.instrument)
            if snapshot.asof < due or q is None or q.receive_time < due or q.exchange_time < due:
                continue
            if (
                not q.valid
                or (snapshot.asof - q.exchange_time).total_seconds()
                > self.settings.max_quote_age_seconds
            ):
                continue
            levels = q.asks if intent.side == "buy" else q.bids
            levels = levels or [
                (q.ask, q.ask_size) if intent.side == "buy" else (q.bid, q.bid_size)
            ]
            levels = sorted(levels, reverse=intent.side == "sell")
            for price, size in levels:
                eligible = price <= intent.limit if intent.side == "buy" else price >= intent.limit
                if not eligible:
                    break
                key = (intent.instrument, intent.side, price)
                available = max(0, size - consumed.get(key, 0))
                quantity = min(available, intent.amount - intent.filled)
                if quantity <= 1e-9:
                    continue
                charge = fee(price, q.index, quantity, self.settings)
                self.ledger.fill(intent, identifier(), quantity, price, charge, snapshot.asof)
                consumed[key] = consumed.get(key, 0) + quantity
                if intent.state == OrderState.FILLED:
                    break
            # Executable IOC only. Resting fills are not inferred from touched prices.
            await self.cancel(intent)

    async def reconcile(self, snapshot):
        for row in self.ledger.spreads(self.session):
            candidate = SpreadCandidate.model_validate_json(row["candidate"])
            p = self.ledger.positions(self.session)
            if p.get(candidate.short, 0) < -max(p.get(candidate.long, 0), 0) - 1e-9:
                self.ledger.halt(self.session, "unprotected short detected")
                raise ValueError("unprotected short detected")
        self.ledger.event(
            self.session,
            "reconciliation",
            {"ok": True, "positions": self.ledger.positions(self.session)},
            snapshot.asof,
        )

    def settle(self, record, snapshot):
        instrument = snapshot.instruments[record.instrument]
        if (
            record.source != snapshot.source
            or record.expiry != instrument.expiry
            or record.confirmed_at > snapshot.asof
        ):
            raise ValueError("settlement source, expiry or availability mismatch")
        if instrument.settlement != "USDC" or instrument.contract_size != 1:
            raise ValueError("unsupported settlement units")
        amount = self.ledger.positions(self.session).get(record.instrument, 0)
        payoff = expiry_payoff(instrument.strike, record.delivery_price, instrument.option_type)
        charge = fee(payoff, record.delivery_price, abs(amount), self.settings, delivery=True)
        return self.ledger.settle(self.session, record, instrument, charge)


class SpreadExecutor:
    """Protection first on entry, short first on exit, including restart recovery."""

    def __init__(self, adapter, ledger, session):
        self.adapter, self.ledger, self.session = adapter, ledger, session

    async def enter(self, candidate, snapshot):
        self.ledger.reserve(self.session, candidate)
        await self.adapter.submit(
            intent_for(
                self.session, candidate.id, candidate.long, "buy", candidate.amount, snapshot
            ),
            snapshot,
        )

    async def manage(self, snapshot, settings):
        await self.adapter.advance(snapshot)
        await self.adapter.reconcile(snapshot)
        active = {
            o.candidate_id
            for o in self.ledger.orders(self.session)
            if o.state
            in (
                OrderState.SUBMITTING,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.RECONCILING,
            )
        }
        for row in self.ledger.spreads(self.session):
            c = SpreadCandidate.model_validate_json(row["candidate"])
            p = self.ledger.positions(self.session)
            long, short = max(0, p.get(c.long, 0)), max(0, -p.get(c.short, 0))
            if c.id in active:
                continue
            if not all(n in snapshot.quotes for n in (c.long, c.short)):
                self.ledger.halt(self.session, "missing managed leg quotes")
                continue
            if any(
                not snapshot.quotes[n].valid
                or (snapshot.asof - snapshot.quotes[n].exchange_time).total_seconds()
                > settings.max_quote_age_seconds
                for n in (c.long, c.short)
            ):
                self.ledger.halt(self.session, "stale managed leg quotes")
                continue
            if row["status"] == "OPENING":
                orders = [o for o in self.ledger.orders(self.session) if o.candidate_id == c.id]
                sold_before = any(o.instrument == c.short for o in orders)
                if short > 0 and abs(short - long) < 1e-9:
                    self.ledger.spread_status(c.id, "OPEN")
                elif (
                    long > short
                    and not sold_before
                    and not self.ledger.session(self.session)["halt"]
                ):
                    # Re-check executable net credit; do not sell below the approved budget.
                    short_q = snapshot.quotes[c.short]
                    paid = sum(o.average * o.filled for o in orders if o.instrument == c.long)
                    required_credit = (c.width * c.amount + c.costs - c.worst_loss) / c.amount
                    instrument = snapshot.instruments[c.short]
                    quantity = (
                        floor((long - short) / instrument.amount_step + 1e-9)
                        * instrument.amount_step
                    )
                    if (
                        short_q.bid - paid / max(long, 1e-9) + 1e-8 >= required_credit
                        and quantity >= instrument.min_amount
                    ):
                        await self.adapter.submit(
                            intent_for(self.session, c.id, c.short, "sell", quantity, snapshot),
                            snapshot,
                        )
                    else:
                        self.ledger.spread_status(c.id, "CLOSING")
                elif long == 0 and short == 0:
                    self.ledger.spread_status(c.id, "CLOSED")
                else:
                    self.ledger.spread_status(c.id, "CLOSING")
            if row["status"] == "OPEN":
                sq, lq = snapshot.quotes[c.short], snapshot.quotes[c.long]
                close_value = (sq.ask - lq.bid) * short
                # The stop uses mid value: executable value includes the entry bid/ask round
                # trip and would stop a fresh position out before the market moved.
                mid_value = ((sq.ask + sq.bid) / 2 - (lq.ask + lq.bid) / 2) * short
                hours = (snapshot.asof - c.created_at).total_seconds() / 3600
                to_expiry = (
                    snapshot.instruments[c.short].expiry - snapshot.asof
                ).total_seconds() / 3600
                initial_credit = c.credit * short / c.amount
                if (
                    self.ledger.session(self.session)["halt"]
                    or hours >= settings.max_holding_hours
                    or to_expiry <= settings.exit_before_expiry_hours
                    or close_value <= initial_credit * (1 - settings.take_profit_fraction)
                    or mid_value >= initial_credit * settings.stop_credit_multiple
                ):
                    self.ledger.spread_status(c.id, "CLOSING")
            status = self.ledger.db.execute(
                "SELECT status FROM spreads WHERE id=?", (c.id,)
            ).fetchone()[0]
            if status == "CLOSING":
                if short > 1e-9:
                    await self.adapter.submit(
                        intent_for(self.session, c.id, c.short, "buy", short, snapshot), snapshot
                    )
                elif long > 1e-9:
                    await self.adapter.submit(
                        intent_for(self.session, c.id, c.long, "sell", long, snapshot), snapshot
                    )
                else:
                    self.ledger.spread_status(c.id, "CLOSED")

    async def reduce(self):
        for order in self.ledger.orders(self.session):
            if order.state in (
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.SUBMITTING,
            ):
                row = self.ledger.db.execute(
                    "SELECT candidate FROM spreads WHERE id=?", (order.candidate_id,)
                ).fetchone()
                if row:
                    c = SpreadCandidate.model_validate_json(row[0])
                    if (order.instrument == c.long and order.side == "buy") or (
                        order.instrument == c.short and order.side == "sell"
                    ):
                        await self.adapter.cancel(order)
        for row in self.ledger.spreads(self.session):
            self.ledger.spread_status(row["id"], "CLOSING")
