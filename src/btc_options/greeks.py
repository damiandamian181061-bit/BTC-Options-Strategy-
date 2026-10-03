"""Leg, spread and portfolio Greeks from quoted forwards and mark implied volatility.

Units are USDC linear options per BTC: delta in BTC, gamma in BTC per USDC of
forward move, theta in USDC per calendar day, vega in USDC per volatility point.
Implied volatility is an annualized decimal. Aggregated IV is |vega|-weighted.
"""

from .domain import Greeks
from .pricing import YEAR_SECONDS, black_greeks, implied_vol


def maturity(instrument, asof):
    return max((instrument.expiry - asof).total_seconds() / YEAR_SECONDS, 0.0)


def mid_iv(instrument, quote, asof):
    """Black volatility implied by the executable mid, independent of the exchange mark."""
    t = maturity(instrument, asof)
    if t <= 0 or quote.bid <= 0:
        return None
    value = implied_vol(
        (quote.bid + quote.ask) / 2,
        quote.forward,
        instrument.strike,
        t,
        option_type=instrument.option_type,
    )
    return value if value == value else None


def leg(instrument, quote, asof, amount=1.0, volatility=None):
    """Greeks for a signed amount of one option, priced at mark IV unless a vol is given."""
    vol = quote.iv if volatility is None else volatility
    g = black_greeks(
        quote.forward,
        instrument.strike,
        maturity(instrument, asof),
        vol,
        option_type=instrument.option_type,
    )
    return Greeks(iv=vol, **{k: v * amount for k, v in g.items()})


def total(items):
    """Sum Greeks; IV becomes the |vega|-weighted average of the inputs."""
    items = list(items)
    weight = sum(abs(g.vega) for g in items if g.iv is not None)
    iv = sum(abs(g.vega) * g.iv for g in items if g.iv is not None) / weight if weight else None
    return Greeks(
        iv=iv,
        delta=sum(g.delta for g in items),
        gamma=sum(g.gamma for g in items),
        theta=sum(g.theta for g in items),
        vega=sum(g.vega for g in items),
    )


def portfolio(amounts, snapshot):
    """Per-instrument and total Greeks of held amounts; unquoted legs are reported missing."""
    legs, missing = {}, []
    for name, amount in amounts.items():
        quote, instrument = snapshot.quotes.get(name), snapshot.instruments.get(name)
        if quote is None or instrument is None:
            missing.append(name)
            continue
        legs[name] = leg(instrument, quote, snapshot.asof, amount)
    return legs, total(legs.values()), missing
