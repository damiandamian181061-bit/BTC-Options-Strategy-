"""
Calibration: find model parameters that best reproduce observed option
prices across strikes and maturities.

Objective: relative squared price error summed across the whole surface,
    sum_i ((model_price_i - market_price_i) / market_price_i) ** 2
computed by grouping quotes by maturity T and pricing all strikes for that
T in one vectorized call (see models/heston.py, models/bates.py) — this is
what keeps calibration to a few seconds rather than several minutes.

Optimization: differential_evolution (global — the smile-fitting landscape
is non-convex) followed by a local Nelder-Mead polish.
"""
import numpy as np
from scipy.optimize import differential_evolution, minimize, minimize_scalar

from models.black_scholes import bs_price
from models.heston import heston_prices_for_strikes
from models.bates import bates_prices_for_strikes


def _rel_sq_error(model_prices, market_prices):
    return np.sum(((np.asarray(model_prices) - np.asarray(market_prices)) / np.asarray(market_prices)) ** 2)


def _by_maturity_and_type(df, S0):
    """Group (K array, market_price array, S0_for_this_group) by (maturity T,
    option_type). If df has its own 'S0' column (a per-maturity forward —
    needed for a real multi-expiry BTC surface, where cost-of-carry/funding
    means longer-dated expiries price off a higher effective forward than
    spot), that value is used per group; otherwise every group falls back
    to the single scalar S0 passed in (the synthetic single-spot workflow)."""
    groups = []
    for (T, otype), sub in df.groupby(["T", "option_type"]):
        group_S0 = sub["S0"].iloc[0] if "S0" in sub.columns else S0
        groups.append((T, otype, sub.K.values, sub.market_price.values, group_S0))
    return groups


def calibrate_flat_bs(df, S0, r):
    """Best single (flat) volatility minimizing price error across the WHOLE
    surface. Black-Scholes has only one free parameter, so it cannot fit a
    smile — this is the honest baseline showing exactly how badly a single
    number does across strikes and maturities at once."""
    has_S0_col = "S0" in df.columns

    def objective(sigma):
        errs = 0.0
        for row in df.itertuples():
            row_S0 = row.S0 if has_S0_col else S0
            p = bs_price(row_S0, row.K, row.T, r, sigma, row.option_type)
            errs += ((p - row.market_price) / row.market_price) ** 2
        return errs

    res = minimize_scalar(objective, bounds=(0.05, 3.0), method="bounded")
    return {"sigma": res.x}, res.fun


def calibrate_heston(df, S0, r, seed=1, sigma_v_max=3.0):
    groups = _by_maturity_and_type(df, S0)
    bounds = [
        (0.01, 4.0),    # v0
        (0.1, 10.0),    # kappa
        (0.01, 4.0),    # theta
        (0.05, sigma_v_max),  # sigma_v
        (-0.99, 0.99),  # rho
    ]

    def objective(x):
        v0, kappa, theta, sigma_v, rho = x
        total = 0.0
        for T, otype, K_arr, mkt, group_S0 in groups:
            prices = heston_prices_for_strikes(group_S0, K_arr, T, r, v0, kappa, theta, sigma_v, rho,
                                                option_type=otype)
            total += _rel_sq_error(prices, mkt)
        return total

    de = differential_evolution(objective, bounds, seed=seed, maxiter=80, popsize=25,
                                 tol=1e-8, polish=False, workers=1, mutation=(0.5, 1.5),
                                 recombination=0.8)
    local = minimize(objective, de.x, method="Nelder-Mead", bounds=bounds,
                      options={"xatol": 1e-7, "fatol": 1e-10, "maxiter": 800})
    x = local.x if local.fun < de.fun else de.x
    params = dict(v0=x[0], kappa=x[1], theta=x[2], sigma_v=x[3], rho=x[4])
    return params, min(local.fun, de.fun)


def calibrate_bates(df, S0, r, seed=1, sigma_v_max=3.0, delta_j_min=0.01):
    groups = _by_maturity_and_type(df, S0)
    bounds = [
        (0.01, 4.0),    # v0
        (0.1, 10.0),    # kappa
        (0.01, 4.0),    # theta
        (0.05, sigma_v_max),  # sigma_v
        (-0.99, 0.99),  # rho
        (0.0, 5.0),     # lambda_j
        (-0.5, 0.5),    # mu_j
        (delta_j_min, 0.6),  # delta_j
    ]

    def objective(x):
        v0, kappa, theta, sigma_v, rho, lambda_j, mu_j, delta_j = x
        total = 0.0
        for T, otype, K_arr, mkt, group_S0 in groups:
            prices = bates_prices_for_strikes(group_S0, K_arr, T, r, v0, kappa, theta, sigma_v, rho,
                                               lambda_j, mu_j, delta_j, option_type=otype)
            total += _rel_sq_error(prices, mkt)
        return total

    de = differential_evolution(objective, bounds, seed=seed, maxiter=90, popsize=30,
                                 tol=1e-8, polish=False, workers=1, mutation=(0.5, 1.5),
                                 recombination=0.8)
    local = minimize(objective, de.x, method="Nelder-Mead", bounds=bounds,
                      options={"xatol": 1e-7, "fatol": 1e-10, "maxiter": 1000})
    x = local.x if local.fun < de.fun else de.x
    params = dict(v0=x[0], kappa=x[1], theta=x[2], sigma_v=x[3], rho=x[4],
                   lambda_j=x[5], mu_j=x[6], delta_j=x[7])
    return params, min(local.fun, de.fun)
