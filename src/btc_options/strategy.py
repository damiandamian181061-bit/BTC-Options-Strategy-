"""Defined-risk credit verticals. Physical scenarios are separate from implied valuation."""

import json
from datetime import datetime
from math import floor
from pathlib import Path

import numpy as np

from . import greeks
from .domain import SpreadCandidate
from .pricing import YEAR_SECONDS, black_price
from .surfaces import fit_ssvi


def fee(price, index, amount, settings, delivery=False):
    rate = settings.delivery_fee_rate if delivery else settings.fee_rate
    return min(rate * index, settings.fee_premium_cap * price) * amount


def event_context(settings, now):
    if not settings.event_calendar:
        return {
            "calendar_available": False,
            "blocked": False,
            "events": [],
            "note": "No curated event calendar; unknown events remain a risk.",
        }
    data = json.loads(Path(settings.event_calendar).read_text(encoding="utf-8"))
    events = []
    for event in data:
        start, end = (
            datetime.fromisoformat(event["block_from"]),
            datetime.fromisoformat(event["block_until"]),
        )
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("invalid event window")
        if start <= now <= end:
            events.append(event["description"])
    return {"calendar_available": True, "blocked": bool(events), "events": events}


def surface_for(snapshot, settings):
    data = []
    for name, q in snapshot.quotes.items():
        inst = snapshot.instruments[name]
        age = (snapshot.asof - q.exchange_time).total_seconds()
        if inst.settlement != "USDC" or age > settings.max_quote_age_seconds or not q.valid:
            continue
        maturity = (inst.expiry - snapshot.asof).total_seconds() / YEAR_SECONDS
        # Sub-day expiries carry pin/event noise that distorts a joint surface fit.
        if maturity < 1 / 365 or q.bid_size <= 0 or q.ask_size <= 0:
            continue
        # Weight by quote precision: bid/ask width in volatility units.
        spread_vol = (q.ask - q.bid) / q.vega / 100 if q.vega > 0 else 0.05
        data.append((maturity, np.log(inst.strike / q.forward), q.iv, 1 / max(spread_vol, 0.005)))
    if len(data) < 6 or len(set(t for t, _, _, _ in data)) < 2:
        raise ValueError("SSVI requires six liquid quotes across at least two expiries")
    return fit_ssvi(*map(np.array, zip(*data, strict=True)))


def exit_horizon_days(settings, days_to_expiry):
    return min(
        settings.max_holding_hours / 24, days_to_expiry - settings.exit_before_expiry_hours / 24
    )


class PathSet:
    """Shared physical paths for one expiry: forward, remaining maturity and IV level factor."""

    def __init__(self, forecast, surface, forward, maturity, horizon, count, shift):
        returns = forecast.return_paths(horizon, count)
        n = returns.shape[0]
        dt = np.ones(n)
        dt[-1] = horizon - (n - 1)
        rng = np.random.default_rng(11)
        noise = rng.standard_normal(returns.shape)
        # Log IV level relative to today: AR(1) toward the DVOL long-run level, physical beta
        # to spot and idiosyncratic vol-of-vol, all estimated from DVOL history.
        level = np.zeros(returns.shape[1])
        factor = np.empty_like(returns)
        for j in range(n):
            level = (
                level
                + forecast.iv_kappa * dt[j] * (forecast.iv_target - level)
                + forecast.iv_beta * returns[j]
                + forecast.iv_vol * np.sqrt(dt[j]) * noise[j]
            )
            factor[j] = level
        self.forward = forward * np.exp(np.cumsum(returns, axis=0))
        self.iv_factor = np.exp(factor)
        self.remaining = np.maximum(maturity - np.cumsum(dt) / 365, 1e-6)[:, None]
        self.surface, self.shift, self.cache = surface, shift, {}

    def value(self, strike, option_type, offset=0.0):
        """`offset` anchors the leg to its own market IV; the surface supplies dynamics."""
        key = (strike, option_type)
        if key not in self.cache:
            # Sticky moneyness: the smile moves with the forward.
            vol = self.surface.volatility(strike, self.forward, self.remaining, clip=True)
            vol = np.maximum((vol + offset + self.shift) * self.iv_factor, 0.01)
            self.cache[key] = black_price(
                self.forward, strike, self.remaining, vol, option_type=option_type
            )
        return self.cache[key]


def simulate_exit(short_v, long_v, credit, half_spreads, width, settings, forward, entry_fees):
    """Per-unit P&L under the execution exits: take-profit on executable value, stop on mid
    value (a stop on executable value fires on the entry bid/ask alone), then time exit."""
    mid = short_v - long_v
    if np.any(mid < -1e-6) or np.any(mid > width + 1e-6):
        return None
    executable = np.clip(mid + sum(half_spreads), 0, width)
    hit = (executable <= credit * (1 - settings.take_profit_fraction)) | (
        mid >= credit * settings.stop_credit_multiple
    )
    hit[-1] = True
    step = hit.argmax(axis=0)
    cols = np.arange(mid.shape[1])
    index = forward[step, cols]
    exit_fees = np.minimum(
        settings.fee_rate * index,
        settings.fee_premium_cap * (short_v[step, cols] + half_spreads[0]),
    ) + np.minimum(
        settings.fee_rate * index,
        settings.fee_premium_cap * np.maximum(long_v[step, cols] - half_spreads[1], 0),
    )
    return credit - executable[step, cols] - entry_fees - exit_fees


def candidates(snapshot, forecast, equity, settings, surface=None, paths=2000):
    if forecast.cutoff > snapshot.asof:
        raise ValueError("future forecast")
    surface = surface or surface_for(snapshot, settings)
    pathsets = {}
    result = []
    for short_name, sq in snapshot.quotes.items():
        short = snapshot.instruments[short_name]
        days = (short.expiry - snapshot.asof).total_seconds() / 86400
        if short.settlement != "USDC" or not short.contract_size == 1:
            continue
        if not settings.min_expiry_days <= days <= settings.max_expiry_days:
            continue
        if not settings.short_delta_min <= abs(sq.delta) <= settings.short_delta_max:
            continue
        if (
            not sq.valid
            or (snapshot.asof - sq.exchange_time).total_seconds() > settings.max_quote_age_seconds
        ):
            continue
        t = days / 365
        horizon = exit_horizon_days(settings, days)
        if horizon <= 0:
            continue
        if short.expiry not in pathsets:
            # Starting-surface uncertainty from this expiry's own fit residuals.
            uncertainty = max(2 * surface.slice_error(t), 0.005)
            pathsets[short.expiry] = [
                PathSet(forecast, surface, sq.forward, t, horizon, paths, shift)
                for shift in (0, -uncertainty, uncertainty)
            ]
        for long_name, lq in snapshot.quotes.items():
            long = snapshot.instruments[long_name]
            direction = (
                long.strike < short.strike
                if short.option_type == "put"
                else long.strike > short.strike
            )
            if (
                long.expiry != short.expiry
                or long.option_type != short.option_type
                or not direction
            ):
                continue
            if long.settlement != "USDC" or long.contract_size != 1 or not lq.valid:
                continue
            if (snapshot.asof - lq.exchange_time).total_seconds() > settings.max_quote_age_seconds:
                continue
            if abs(lq.forward / sq.forward - 1) > 0.001 or abs(lq.index / sq.index - 1) > 0.001:
                continue
            credit = sq.bid - lq.ask
            width = abs(long.strike - short.strike)
            if credit <= 0 or credit >= width or min(sq.bid_size, lq.ask_size) <= 0:
                continue
            unit_cost = sum(fee(p, sq.index, 1, settings) for p in (sq.bid, sq.ask, lq.bid, lq.ask))
            entry_fees = fee(sq.bid, sq.index, 1, settings) + fee(lq.ask, sq.index, 1, settings)
            # Reserve fully funded wing premium as well as completed-spread maximum loss.
            unit_risk = max(width - credit + unit_cost, lq.ask + unit_cost)
            step = max(long.amount_step, short.amount_step)
            amount = (
                floor(
                    min(equity * settings.risk.per_trade / unit_risk, sq.bid_size, lq.ask_size)
                    / step
                    + 1e-9
                )
                * step
            )
            if amount < max(long.min_amount, short.min_amount):
                continue
            try:
                sv = surface.volatility(short.strike, sq.forward, t)
                lv = surface.volatility(long.strike, lq.forward, t)
            except ValueError:
                continue
            half_spreads = ((sq.ask - sq.bid) / 2, (lq.ask - lq.bid) / 2)
            outcomes = []
            for ps in pathsets[short.expiry]:
                outcome = simulate_exit(
                    ps.value(short.strike, short.option_type, sq.iv - sv),
                    ps.value(long.strike, long.option_type, lq.iv - lv),
                    credit,
                    half_spreads,
                    width,
                    settings,
                    ps.forward,
                    entry_fees,
                )
                if outcome is None:
                    break
                outcomes.append(outcome * amount)
            if len(outcomes) != 3:
                continue
            pnl = np.asarray(outcomes)
            expected = float(pnl[0].mean())
            # Sampling error and starting-surface uncertainty are separate penalties.
            conservative = float(min(x.mean() - 1.645 * x.std() / np.sqrt(len(x)) for x in pnl))
            if conservative <= 0:
                continue
            net = greeks.total(
                (
                    greeks.leg(long, lq, snapshot.asof, amount, float(lv)),
                    greeks.leg(short, sq, snapshot.asof, -amount, float(sv)),
                )
            )
            result.append(
                SpreadCandidate(
                    snapshot_id=snapshot.id,
                    strategy="bull_put" if short.option_type == "put" else "bear_call",
                    long=long_name,
                    short=short_name,
                    amount=amount,
                    width=width,
                    credit=credit * amount,
                    worst_loss=(width - credit + unit_cost) * amount,
                    partial_loss=(lq.ask + unit_cost) * amount,
                    costs=unit_cost * amount,
                    expected_pnl=expected,
                    conservative_pnl=conservative,
                    tail_loss=min(
                        (width - credit + unit_cost) * amount,
                        max(0, -float(np.quantile(pnl, 0.01))),
                    ),
                    delta_notional=net.delta * sq.index,
                    vega=net.vega,
                    greeks=net,
                    model_versions={
                        "surface": "essvi-hm-1",
                        "forecast": "har-harx-gjr-ewma-2",
                        "scenario": "fhs-paths-iv-dynamics-exits-1",
                    },
                    created_at=snapshot.asof,
                )
            )
    return sorted(
        result, key=lambda c: (-c.conservative_pnl / max(c.worst_loss, c.partial_loss), c.tail_loss)
    )
