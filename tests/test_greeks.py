import pytest

from btc_options import greeks
from btc_options.demo import forecast
from btc_options.pricing import black_greeks, black_price
from btc_options.strategy import candidates


def test_theta_matches_one_day_decay():
    f, k, t, v = 100000, 95000, 30 / 365, 0.55
    g = black_greeks(f, k, t, v, option_type="put")
    one_day = black_price(f, k, t - 1 / 365, v, option_type="put") - black_price(
        f, k, t, v, option_type="put"
    )
    assert g["theta"] == pytest.approx(one_day, rel=0.02)
    assert g["delta"] < 0 and g["gamma"] > 0 and g["vega"] > 0 and g["theta"] < 0


def test_expired_greeks_are_intrinsic_delta_only():
    assert black_greeks(100, 90, 0, 0.5) == dict(delta=1.0, gamma=0.0, theta=0.0, vega=0.0)
    assert black_greeks(100, 90, 0, 0.5, option_type="put")["delta"] == 0


def test_portfolio_greeks_scale_with_signed_amounts(market):
    names = sorted(
        (n for n, i in market.instruments.items() if i.option_type == "put"),
        key=lambda n: (market.instruments[n].expiry, market.instruments[n].strike),
    )
    long, short = names[10], names[12]
    legs, total, missing = greeks.portfolio({long: 0.5, short: -0.5, "unknown": 1}, market)
    assert missing == ["unknown"]
    one = greeks.leg(market.instruments[long], market.quotes[long], market.asof)
    assert legs[long].delta == pytest.approx(one.delta * 0.5)
    assert total.delta == pytest.approx(legs[long].delta + legs[short].delta)
    # A bull put spread is long delta and collects time decay.
    assert total.delta > 0 and total.theta > 0 and total.vega < 0
    low, high = sorted(g.iv for g in legs.values())
    assert low <= total.iv <= high


def test_mid_iv_inverts_the_executable_mid(market):
    name = next(n for n in market.quotes if market.instruments[n].strike == 100000)
    quote, instrument = market.quotes[name], market.instruments[name]
    assert greeks.mid_iv(instrument, quote, market.asof) == pytest.approx(quote.iv, abs=0.01)


def test_candidates_carry_net_greeks(market, settings):
    # Quotes 50% above fair value (IV unchanged) give spreads a real conservative edge.
    market = market.model_copy(
        update={
            "quotes": {
                n: q.model_copy(
                    update={
                        "bid": q.bid * 1.5,
                        "ask": q.ask * 1.5,
                        "bids": [(q.bid * 1.5, 1)],
                        "asks": [(q.ask * 1.5, 1)],
                    }
                )
                for n, q in market.quotes.items()
            }
        }
    )
    ranked = candidates(market, forecast(market.asof), 10000, settings)
    assert ranked
    for c in ranked:
        assert c.greeks is not None and c.greeks.iv > 0
        assert c.delta_notional == pytest.approx(c.greeks.delta * market.quotes[c.short].index)
        assert c.vega == pytest.approx(c.greeks.vega)
        assert (c.greeks.delta > 0) == (c.strategy == "bull_put")
