"""
Black-Scholes-Merton pricing and implied volatility inversion.
Used as the baseline model against which Heston and Bates are benchmarked.
"""
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq


def bs_price(S0, K, T, r, sigma, option_type="call"):
    """European option price under Black-Scholes."""
    if sigma <= 0 or T <= 0:
        intrinsic = max(S0 - K, 0.0) if option_type == "call" else max(K - S0, 0.0)
        return intrinsic
    d1 = (np.log(S0 / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if option_type == "call":
        return S0 * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S0 * norm.cdf(-d1)


def implied_vol(price, S0, K, T, r, option_type="call"):
    """Invert Black-Scholes to back out implied volatility from a price.
    Returns np.nan if no solution exists in [1e-6, 5.0] (e.g. price violates
    no-arbitrage bounds).
    """
    intrinsic = max(S0 - K * np.exp(-r * T), 0.0) if option_type == "call" \
        else max(K * np.exp(-r * T) - S0, 0.0)
    if price <= intrinsic + 1e-12:
        return np.nan

    def objective(sigma):
        return bs_price(S0, K, T, r, sigma, option_type) - price

    try:
        return brentq(objective, 1e-6, 5.0, xtol=1e-8, maxiter=200)
    except ValueError:
        return np.nan
