"""
Greeks via finite-difference bumping — works identically for all three
models (Black-Scholes, Heston, Bates), which is the point: Heston and
Bates have no closed-form Greeks (Heston has FIVE vol-related parameters,
so there's no single "sigma" to differentiate against the way Black-Scholes
has), so bump-and-reprice is the standard, model-agnostic way to get them.

Conventions used here (state them explicitly — Greek conventions vary
across desks and this choice matters for interpreting the output):
  delta - dPrice/dS, central difference, bump ~1bp of spot
  gamma - d2Price/dS2, central difference
  theta - (Price at T-1day) - (Price at T), holding S fixed: the expected
          overnight P&L from time decay alone. Negative for a long option
          in typical (non-deep-ITM, positive-rate) cases.
  vega  - dPrice/d(sqrt(v0)) * 0.01, i.e. price change per 1 percentage
          point move in the CURRENT instantaneous vol level. For Heston/
          Bates this bumps v0 (the initial variance) via its square root;
          for Black-Scholes it bumps sigma directly. This is the natural
          analog of BS vega, but it is a choice, not the only possible
          one — a different "vega" could instead bump theta (long-run
          variance) or sigma_v (vol-of-vol). Bumping v0 is used here
          because it answers the question a trader usually means by vega:
          "if the market's current implied vol moved by 1 point, how much
          would this option's price move?"

Validated against Black-Scholes' own closed-form Greek formulas below
(see test at the bottom of this file / project README) before being
trusted on Heston and Bates.
"""
import numpy as np
from models.black_scholes import bs_price
from models.heston import heston_prices_for_strikes
from models.bates import bates_prices_for_strikes


def _bumped_pricer(model_name, params, r):
    """Returns f(S0, K_arr, T, option_type, dS0=0, dT=0, dvol=0) -> price array,
    applying bumps to spot, time-to-maturity, and current-vol-level respectively."""
    if model_name == "Black-Scholes":
        def f(S0, K_arr, T, option_type, dS0=0.0, dT=0.0, dvol=0.0):
            sigma = max(params["sigma"] + dvol, 1e-6)
            Tb = max(T + dT, 1e-6)
            return np.array([bs_price(S0 + dS0, K, Tb, r, sigma, option_type) for K in K_arr])

    elif model_name == "Heston":
        def f(S0, K_arr, T, option_type, dS0=0.0, dT=0.0, dvol=0.0):
            p = dict(params)
            sigma0 = max(np.sqrt(p["v0"]) + dvol, 1e-6)
            p["v0"] = sigma0 ** 2
            Tb = max(T + dT, 1e-6)
            return heston_prices_for_strikes(S0 + dS0, K_arr, Tb, r, option_type=option_type, **p)

    elif model_name == "Bates":
        def f(S0, K_arr, T, option_type, dS0=0.0, dT=0.0, dvol=0.0):
            p = dict(params)
            sigma0 = max(np.sqrt(p["v0"]) + dvol, 1e-6)
            p["v0"] = sigma0 ** 2
            Tb = max(T + dT, 1e-6)
            return bates_prices_for_strikes(S0 + dS0, K_arr, Tb, r, option_type=option_type, **p)
    else:
        raise ValueError(f"unknown model {model_name}")
    return f


def compute_greeks(model_name, params, r, S0, K_arr, T, option_type,
                    h_S=None, h_T=1 / 365, h_vol=1e-4):
    """Returns dict of arrays (one entry per strike in K_arr): price, delta,
    gamma, theta, vega. See module docstring for exact conventions."""
    if h_S is None:
        h_S = S0 * 1e-4  # ~1bp of spot
    f = _bumped_pricer(model_name, params, r)

    p0 = f(S0, K_arr, T, option_type)
    p_up = f(S0, K_arr, T, option_type, dS0=h_S)
    p_dn = f(S0, K_arr, T, option_type, dS0=-h_S)
    delta = (p_up - p_dn) / (2 * h_S)
    gamma = (p_up - 2 * p0 + p_dn) / (h_S ** 2)

    p_tomorrow = f(S0, K_arr, max(T - h_T, 1e-6), option_type)
    theta = p_tomorrow - p0  # $ P&L per calendar day from time decay alone

    v_up = f(S0, K_arr, T, option_type, dvol=h_vol)
    v_dn = f(S0, K_arr, T, option_type, dvol=-h_vol)
    vega = (v_up - v_dn) / (2 * h_vol) * 0.01  # $ price change per 1 vol point

    return dict(price=p0, delta=delta, gamma=gamma, theta=theta, vega=vega)
