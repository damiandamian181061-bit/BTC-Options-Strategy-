"""Typed policies; secrets are environment variables, never serialized config."""

from pathlib import Path
from typing import Literal
import tomllib

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_trade: float = Field(default=0.0025, gt=0, le=0.02)
    aggregate: float = Field(default=0.01, gt=0, le=0.05)
    daily_loss: float = Field(default=0.01, gt=0, le=0.10)
    drawdown: float = Field(default=0.10, gt=0, le=0.15)
    max_positions: int = Field(default=1, ge=1, le=1)
    margin_utilization: float = Field(default=0.20, gt=0, le=0.5)
    delta_notional: float = Field(default=0.10, gt=0, le=1)
    vega_fraction: float = Field(default=0.01, gt=0, le=0.1)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["paper", "testnet", "live"] = "paper"
    runtime_dir: Path = Path("runtime")
    paper_balance: float = Field(default=10000, gt=0)
    strategy_capital: float | None = Field(default=None, gt=0)
    jev_mode: Literal["off", "shadow", "filter"] = "shadow"
    jev_model: str = "jev-1.13.0"
    evaluation_seconds: int = Field(default=300, ge=1)
    max_quote_age_seconds: float = Field(default=15, gt=0)
    latency_ms: int = Field(default=250, ge=0)
    fee_rate: float = Field(default=0.0003, ge=0)
    fee_premium_cap: float = Field(default=0.125, ge=0, le=1)
    delivery_fee_rate: float = Field(default=0.00015, ge=0)
    min_expiry_days: float = Field(default=14, gt=0)
    max_expiry_days: float = Field(default=45, gt=0)
    short_delta_min: float = Field(default=0.10, ge=0, le=1)
    short_delta_max: float = Field(default=0.20, ge=0, le=1)
    max_holding_hours: float = Field(default=24, gt=0)
    exit_before_expiry_hours: float = Field(default=48, ge=0)
    take_profit_fraction: float = Field(default=0.5, gt=0, lt=1)
    stop_credit_multiple: float = Field(default=2, gt=1)
    min_forecast_days: int = Field(default=60, ge=30)
    event_calendar: str = ""
    risk: RiskPolicy = Field(default_factory=RiskPolicy)

    @model_validator(mode="after")
    def ranges(self):
        if self.min_expiry_days >= self.max_expiry_days:
            raise ValueError("expiry range must increase")
        if self.short_delta_min >= self.short_delta_max:
            raise ValueError("delta range must increase")
        if self.risk.per_trade > self.risk.aggregate:
            raise ValueError("aggregate budget must cover per-trade budget")
        return self

    @classmethod
    def load(cls, path: Path | None = None):
        return cls(**tomllib.loads(path.read_text()) if path else {})
