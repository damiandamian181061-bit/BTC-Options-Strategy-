import numpy as np
import pytest
import QuantLib as ql

from btc_options.pricing import (
    black_price,
    black_greeks,
    implied_vol,
    structural_price,
    merton_price,
)
from btc_options.rough import rough_bergomi
from btc_options.surfaces import SSVI, fit_ssvi
from btc_options.structural import prices_for_strikes


@pytest.mark.parametrize("option_type", ["call", "put"])
@pytest.mark.parametrize("strike", [80, 100, 120])
def test_black_independent_quantlib(option_type, strike):
    kind = ql.Option.Call if option_type == "call" else ql.Option.Put
    reference = ql.blackFormula(kind, strike, 100, 0.6 * np.sqrt(0.25), 0.98)
    price = black_price(100, strike, 0.25, 0.6, 0.98, option_type)
    assert price == pytest.approx(reference, abs=1e-10)
    assert implied_vol(price, 100, strike, 0.25, 0.98, option_type) == pytest.approx(0.6, abs=1e-8)


def test_analytical_greeks_match_bumps():
    f, k, t, v = 100, 110, 0.2, 0.6
    h = 0.001
    g = black_greeks(f, k, t, v)
    value = black_price(f, k, t, v)
    assert g["delta"] == pytest.approx(
        (black_price(f + h, k, t, v) - black_price(f - h, k, t, v)) / (2 * h), abs=1e-8
    )
    assert g["gamma"] == pytest.approx(
        (black_price(f + h, k, t, v) - 2 * value + black_price(f - h, k, t, v)) / h**2, rel=1e-4
    )
    assert g["vega"] == pytest.approx(
        (black_price(f, k, t, v + 1e-5) - black_price(f, k, t, v - 1e-5)) / 2e-5 * 0.01, rel=1e-5
    )


def test_pricing_bounds_and_zero_time():
    assert black_price(100, 120, 0, 0.6, option_type="put") == 20
    assert np.isnan(implied_vol(150, 100, 100, 0.2))
    with pytest.raises(ValueError):
        black_price(100, 100, 0.2, -0.5)


def test_heston_independent_quantlib():
    date = ql.Date(2, 10, 2026)
    ql.Settings.instance().evaluationDate = date
    rate = ql.YieldTermStructureHandle(ql.FlatForward(date, 0.0, ql.Actual365Fixed()))
    spot = ql.QuoteHandle(ql.SimpleQuote(100))
    model = ql.HestonModel(ql.HestonProcess(rate, rate, spot, 0.25, 2.0, 0.25, 0.3, -0.5))
    option = ql.VanillaOption(
        ql.PlainVanillaPayoff(ql.Option.Call, 110), ql.EuropeanExercise(date + 90)
    )
    option.setPricingEngine(ql.AnalyticHestonEngine(model))
    result = structural_price(
        100, 110, 90 / 365, dict(v0=0.25, kappa=2, theta=0.25, sigma_v=0.3, rho=-0.5)
    )
    assert result.healthy
    assert result.price == pytest.approx(option.NPV(), abs=1e-7)


@pytest.mark.parametrize("strike", [80, 100, 120])
def test_bates_merton_limit(strike):
    p = dict(
        v0=0.25, kappa=2.0, theta=0.25, sigma_v=0.002, rho=0, lambda_j=0.8, mu_j=-0.15, delta_j=0.2
    )
    expected = merton_price(100, strike, 0.5, 0.5, 0.8, -0.15, 0.2)
    actual = structural_price(100, strike, 0.5, p, model="bates")
    assert actual.healthy
    assert actual.price == pytest.approx(expected, abs=0.0001)
    # The fast calibration engine uses fixed quadrature, with a looser tolerance.
    assert prices_for_strikes(100, [strike], 0.5, **p)[0] == pytest.approx(expected, abs=0.003)


def test_bates_zero_jumps_and_put_parity():
    p = dict(v0=0.3, kappa=2, theta=0.25, sigma_v=0.5, rho=-0.4)
    h = structural_price(100, 110, 0.3, p)
    b = structural_price(
        100, 110, 0.3, {**p, "lambda_j": 0, "mu_j": -0.1, "delta_j": 0.2}, model="bates"
    )
    put = structural_price(100, 110, 0.3, p, option_type="put")
    assert b.price == pytest.approx(h.price, abs=1e-8)
    assert h.price - put.price == pytest.approx(-10, abs=1e-8)


def test_ssvi_constraints_and_interpolation():
    theta = np.array([0.04, 0.08, 0.16])
    psi = 0.7 * np.sqrt(theta)
    # Skew flattening with maturity, as in BTC; rho*psi increments respect the calendar bound.
    reference = SSVI(np.array([0.1, 0.2, 0.4]), theta, psi, np.array([-0.45, -0.3, -0.2]))
    assert reference.health()
    t = np.repeat([0.1, 0.2, 0.4], 11)
    k = np.tile(np.linspace(-0.4, 0.4, 11), 3)
    iv = np.sqrt(reference.total_variance(k, t) / t)
    fitted = fit_ssvi(t, k, iv)
    assert fitted.health()
    assert fitted.fit_rmse < 0.002
    dense_k = np.linspace(-0.4, 0.4, 201)
    dense_t = np.linspace(0.1, 0.4, 61)
    w = np.array([fitted.total_variance(dense_k, m) for m in dense_t])
    assert np.min(np.diff(w, axis=0)) >= -1e-12
    assert np.all(fitted.total_variance(k, 0.15) >= fitted.total_variance(k, 0.1))
    grid = np.linspace(68, 145, 1000)
    prices = fitted.price(100, grid, 0.2)
    assert np.min(np.diff(prices, 2)) > -1e-7
    assert np.max(np.diff(prices)) < 0
    with pytest.raises(ValueError):
        fitted.volatility(100, 100, 0.5)


def test_rough_flat_limit_and_reproducibility():
    a = rough_bergomi(100, 100, 0.2, variance=0.36, eta=0, paths=4000, steps=32)
    b = rough_bergomi(100, 100, 0.2, variance=0.36, eta=0, paths=4000, steps=32)
    assert a == b
    assert abs(a.price - black_price(100, 100, 0.2, 0.6)) < 6 * a.standard_error + 0.02
    assert abs(a.martingale_ratio - 1) < 0.015


def test_structural_calibration_health_and_invalid_expiring_wings(market):
    from btc_options.calibration import calibrate

    # Use a liquid synthetic chain to test fit mechanics, not model superiority.
    names = [
        n
        for n, i in market.instruments.items()
        if i.option_type == "call" and 88000 <= i.strike <= 110000
    ]
    sample = market.model_copy(update={"quotes": {n: market.quotes[n] for n in names}})
    result = calibrate(sample)
    assert result.result.healthy
    assert result.result.measure == "risk_neutral"
    assert np.isfinite(result.result.values["holdout_iv_rmse"])
    assert result.result.values["max_quadrature_error"] < 1e-7
    expired = sample.model_copy(update={"asof": max(i.expiry for i in sample.instruments.values())})
    with pytest.raises(ValueError, match="liquid observations"):
        calibrate(expired)


def test_essvi_rejects_calendar_arbitrage_and_fits_varying_skew():
    theta = np.array([0.01, 0.02])
    psi = np.array([0.1, 0.1001])
    # Skew changes faster than psi grows: crossing slices, so health must fail.
    assert not SSVI(np.array([0.1, 0.2]), theta, psi, np.array([-0.5, 0.3])).health()
    rng = np.random.default_rng(3)
    mats = np.array([0.02, 0.05, 0.1, 0.2])
    theta = 0.45**2 * mats
    reference = SSVI(mats, theta, 0.9 * np.sqrt(theta), np.array([-0.05, -0.1, -0.15, -0.18]))
    assert reference.health()
    t = np.repeat(mats, 15)
    k = np.tile(np.linspace(-0.3, 0.2, 15), 4)
    iv = np.sqrt(reference.total_variance(k, t) / t) + rng.normal(0, 0.002, len(t))
    fitted = fit_ssvi(t, k, iv)
    assert fitted.health() and fitted.fit_rmse < 0.004
    assert fitted.rho[0] > fitted.rho[-1]
