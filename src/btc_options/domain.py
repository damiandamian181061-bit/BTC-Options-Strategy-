"""Versioned unit-explicit records shared by every execution mode."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def identifier() -> str:
    return uuid4().hex


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: int = 1


class Instrument(Record):
    name: str
    expiry: datetime
    strike: float = Field(gt=0)
    option_type: Literal["call", "put"]
    settlement: Literal["USDC", "BTC"] = "USDC"
    contract_size: float = Field(default=1, gt=0)
    min_amount: float = Field(default=0.01, gt=0)
    amount_step: float = Field(default=0.01, gt=0)
    tick_size: float = Field(default=5, gt=0)
    tick_steps: list[dict[str, float]] = Field(default_factory=list)
    underlying: str = ""

    @field_validator("expiry")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("expiry requires a timezone")
        return value.astimezone(timezone.utc)

    def tick(self, price: float) -> float:
        tick = self.tick_size
        for step in sorted(self.tick_steps, key=lambda x: x["above_price"]):
            if price > step["above_price"]:
                tick = step["tick_size"]
        return tick


class Quote(Record):
    instrument: str
    exchange_time: datetime
    receive_time: datetime
    bid: float = Field(ge=0)
    ask: float = Field(gt=0)
    bid_size: float = Field(ge=0)
    ask_size: float = Field(ge=0)
    index: float = Field(gt=0)
    forward: float = Field(gt=0)
    mark: float = Field(ge=0)
    iv: float = Field(gt=0)
    delta: float
    vega: float = 0
    bids: list[tuple[float, float]] = Field(default_factory=list)
    asks: list[tuple[float, float]] = Field(default_factory=list)
    sequence: int | None = None
    valid: bool = True

    @field_validator("exchange_time", "receive_time")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("quote timestamps require a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def uncrossed(self):
        if self.bid > self.ask:
            raise ValueError("crossed book")
        return self


class MarketSnapshot(Record):
    id: str = Field(default_factory=identifier)
    asof: datetime
    instruments: dict[str, Instrument]
    quotes: dict[str, Quote]
    source: Literal["production", "testnet", "synthetic", "historical"]
    transport: Literal["rest", "websocket", "synthetic", "import"] = "rest"

    @model_validator(mode="after")
    def causal(self):
        if self.asof.tzinfo is None:
            raise ValueError("snapshot requires a timezone")
        if any(
            q.receive_time > self.asof or q.exchange_time > self.asof for q in self.quotes.values()
        ):
            raise ValueError("snapshot contains future information")
        if any(name not in self.instruments for name in self.quotes):
            raise ValueError("quote without metadata")
        return self


class ModelResult(Record):
    model: str
    version: str
    cutoff: datetime
    values: dict[str, float]
    healthy: bool
    reasons: list[str] = Field(default_factory=list)
    measure: Literal["physical", "risk_neutral"]


class Greeks(Record):
    """Black Greeks in USDC: delta in BTC, gamma per USDC move, theta per day, vega per vol point."""

    iv: float | None = Field(default=None, ge=0)
    delta: float = 0
    gamma: float = 0
    theta: float = 0
    vega: float = 0


class SpreadCandidate(Record):
    id: str = Field(default_factory=identifier)
    snapshot_id: str
    strategy: Literal["bull_put", "bear_call"]
    long: str
    short: str
    amount: float = Field(gt=0)
    width: float = Field(gt=0)
    credit: float = Field(gt=0)
    worst_loss: float = Field(gt=0)
    partial_loss: float = Field(ge=0)
    costs: float = Field(ge=0)
    expected_pnl: float
    conservative_pnl: float
    tail_loss: float = Field(ge=0)
    delta_notional: float
    vega: float
    greeks: Greeks | None = None
    model_versions: dict[str, str]
    created_at: datetime


class RiskAssessment(Record):
    approved: bool
    reasons: list[str]
    reserved_loss: float


class DecisionRecord(Record):
    id: str = Field(default_factory=identifier)
    candidate_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    action: Literal["TRADE", "SKIP", "CLOSE"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: str
    model: str
    request_hash: str
    reasons: list[str]
    request: dict = Field(default_factory=dict)
    response: dict = Field(default_factory=dict)
    latency_ms: float = 0


class OrderState(StrEnum):
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    RECONCILING = "RECONCILING"


class OrderIntent(Record):
    id: str = Field(default_factory=identifier)
    session: str
    candidate_id: str
    instrument: str
    side: Literal["buy", "sell"]
    amount: float = Field(gt=0)
    limit: float = Field(gt=0)
    created_at: datetime
    state: OrderState = OrderState.SUBMITTING
    filled: float = 0
    average: float = 0
    fees: float = 0
    exchange_id: str | None = None


class Settlement(Record):
    instrument: str
    expiry: datetime
    confirmed_at: datetime
    delivery_price: float = Field(gt=0)
    source: Literal["production", "testnet", "historical", "synthetic"]

    @model_validator(mode="after")
    def causal(self):
        if (
            self.expiry.tzinfo is None
            or self.confirmed_at.tzinfo is None
            or self.confirmed_at < self.expiry
        ):
            raise ValueError("settlement must be confirmed after the aware expiry")
        return self
