"""Deterministic synthetic fixtures, always labeled; never evidence for live release."""

from datetime import timedelta

import numpy as np

from .domain import Instrument, MarketSnapshot, Quote, utc_now
from .forecasts import ForecastBundle
from .pricing import black_greeks, black_price


def snapshot(now=None, spot=100000.0, volatility=0.55):
    now = now or utc_now()
    instruments, quotes = {}, {}
    for days in (21, 35):
        expiry = (now + timedelta(days=days)).replace(hour=8, minute=0, second=0, microsecond=0)
        t = (expiry - now).total_seconds() / 86400 / 365
        for strike in range(75000, 130001, 1000):
            for option_type in ("put", "call"):
                name = f"BTC_USDC-{expiry.strftime('%d%b%y').upper()}-{strike}-{option_type[0].upper()}"
                i = Instrument(
                    name=name, expiry=expiry, strike=strike, option_type=option_type, tick_size=0.01
                )
                iv = volatility + 0.15 * np.log(strike / spot) ** 2
                value = black_price(spot, strike, t, iv, option_type=option_type)
                greeks = black_greeks(spot, strike, t, iv, option_type=option_type)
                bid, ask = max(0.01, value - 0.10), value + 0.10
                instruments[name] = i
                quotes[name] = Quote(
                    instrument=name,
                    exchange_time=now,
                    receive_time=now,
                    bid=bid,
                    ask=ask,
                    bid_size=1,
                    ask_size=1,
                    index=spot,
                    forward=spot,
                    mark=value,
                    iv=iv,
                    delta=greeks["delta"],
                    vega=greeks["vega"],
                    bids=[(bid, 1)],
                    asks=[(ask, 1)],
                )
    return MarketSnapshot(
        asof=now, instruments=instruments, quotes=quotes, source="synthetic", transport="synthetic"
    )


def forecast(now):
    rng = np.random.default_rng(42)
    return ForecastBundle(
        now, np.full(60, 0.25**2 / 365), rng.standard_t(8, 1000) * np.sqrt(6 / 8), []
    )
