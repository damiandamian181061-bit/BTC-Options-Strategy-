"""Continuous-time forward pricing and independently checkable structural engines."""

from dataclasses import dataclass
from math import exp, factorial, sqrt

import numpy as np
from scipy.integrate import quad
from scipy.optimize import brentq
from scipy.special import ndtr

from .structural import heston_cd, jump_log_cf


YEAR_SECONDS = 365.0 * 86400


def black_price(forward, strike, maturity, volatility, discount=1.0, option_type="call"):
    if option_type not in ("call", "put"):
        raise ValueError("invalid option type")
    f, k, t, v, d = np.broadcast_arrays(forward, strike, maturity, volatility, discount)
    if not all(np.all(np.isfinite(x)) for x in (f, k, t, v, d)):
        raise ValueError("non-finite pricing inputs")
    if np.any(f <= 0) or np.any(k <= 0) or np.any(t < 0) or np.any(v < 0) or np.any(d <= 0):
        raise ValueError("invalid pricing domain")
    sign = 1 if option_type == "call" else -1
    total = v * np.sqrt(t)
    safe = np.maximum(total, 1e-15)
    d1 = np.log(f / k) / safe + safe / 2
    d2 = d1 - safe
    value = d * sign * (f * ndtr(sign * d1) - k * ndtr(sign * d2))
    result = np.where(total > 1e-14, value, d * np.maximum(sign * (f - k), 0))
    return float(result) if result.ndim == 0 else result


def implied_vol(price, forward, strike, maturity, discount=1.0, option_type="call"):
    if maturity <= 0:
        return float("nan")
    lower = black_price(forward, strike, maturity, 0, discount, option_type)
    upper = discount * (forward if option_type == "call" else strike)
    if not lower < price < upper:
        return float("nan")
    try:
        return brentq(
            lambda v: black_price(forward, strike, maturity, v, discount, option_type) - price,
            1e-9,
            10,
            xtol=1e-11,
        )
    except ValueError:
        return float("nan")


def black_greeks(forward, strike, maturity, volatility, discount=1.0, option_type="call"):
    """Forward Black Greeks per unit of underlying.

    delta: d price / d forward; gamma: d delta / d forward; theta: price change per
    calendar day (zero-rate carry); vega: price change per one volatility point.
    """
    if option_type not in ("call", "put"):
        raise ValueError("invalid option type")
    if maturity <= 0 or volatility <= 0:
        itm = forward > strike if option_type == "call" else forward < strike
        sign = 1 if option_type == "call" else -1
        return dict(delta=float(discount * sign * itm), gamma=0.0, theta=0.0, vega=0.0)
    total = volatility * sqrt(maturity)
    d1 = np.log(forward / strike) / total + total / 2
    density = np.exp(-0.5 * d1**2) / sqrt(2 * np.pi)
    return dict(
        delta=float(discount * (ndtr(d1) - (option_type == "put"))),
        gamma=float(discount * density / (forward * total)),
        theta=float(-discount * forward * density * volatility / (2 * sqrt(maturity) * 365)),
        vega=float(discount * forward * density * sqrt(maturity) * 0.01),
    )


@dataclass(frozen=True)
class PriceResult:
    price: float
    numerical_error: float
    healthy: bool


def structural_price(forward, strike, maturity, params, option_type="call", model="heston"):
    """Adaptive inversion; forward numeraire with discount applied by the caller."""
    if (
        not all(np.isfinite(x) for x in (forward, strike, maturity))
        or min(forward, strike) <= 0
        or maturity < 0
    ):
        raise ValueError("invalid structural pricing domain")
    if maturity <= 0:
        return PriceResult(black_price(forward, strike, 0, 0, option_type=option_type), 0, True)
    if model not in ("heston", "bates") or option_type not in ("call", "put"):
        raise ValueError("unknown pricing model/type")
    required = ("v0", "kappa", "theta", "sigma_v", "rho")
    if not all(k in params for k in required):
        raise ValueError("missing structural parameters")
    if (
        not all(np.isfinite(params[k]) for k in required)
        or min(params["v0"], params["theta"], params["kappa"]) <= 0
        or params["sigma_v"] < 0
        or abs(params["rho"]) >= 1
    ):
        raise ValueError("invalid structural parameters")
    if model == "bates":
        if (
            not all(k in params and np.isfinite(params[k]) for k in ("lambda_j", "mu_j", "delta_j"))
            or min(params["lambda_j"], params["delta_j"]) < 0
        ):
            raise ValueError("invalid jump parameters")
    if params["sigma_v"] < 1e-5:
        # Deterministic variance: Black (Heston) or the Merton mixture (Bates).
        variance = params["theta"] + (params["v0"] - params["theta"]) * (
            1 - exp(-params["kappa"] * maturity)
        ) / (params["kappa"] * maturity)
        if model == "heston":
            value = black_price(forward, strike, maturity, sqrt(variance), option_type=option_type)
        else:
            value = merton_price(
                forward,
                strike,
                maturity,
                sqrt(variance),
                params["lambda_j"],
                params["mu_j"],
                params["delta_j"],
            )
            if option_type == "put":
                value += strike - forward
        return PriceResult(value, 0, True)
    errors = []
    probabilities = []
    for branch in (1, 2):

        def integrand(u, branch=branch):
            c, dv = heston_cd(
                u,
                maturity,
                params["kappa"],
                params["theta"],
                params["sigma_v"],
                params["rho"],
                branch,
            )
            log_cf = c + dv * params["v0"] + 1j * u * np.log(forward / strike)
            if model == "bates":
                log_cf += jump_log_cf(
                    u, maturity, params["lambda_j"], params["mu_j"], params["delta_j"], branch
                )
            return float((np.exp(log_cf) / (1j * u)).real)

        value, error = quad(integrand, 1e-9, np.inf, epsabs=1e-9, epsrel=1e-8, limit=300)
        probabilities.append(0.5 + value / np.pi)
        errors.append(error / np.pi)
    p1, p2 = probabilities
    price = (
        forward * p1 - strike * p2
        if option_type == "call"
        else (strike * (1 - p2) - forward * (1 - p1))
    )
    error = forward * errors[0] + strike * errors[1]
    bound = max(forward - strike, 0) if option_type == "call" else max(strike - forward, 0)
    upper = forward if option_type == "call" else strike
    healthy = (
        np.isfinite(price)
        and bound - 1e-7 <= price <= upper + 1e-7
        and (error < max(1e-5, forward * 1e-7))
    )
    return PriceResult(float(price), float(error), bool(healthy))


def merton_price(forward, strike, maturity, volatility, intensity, jump_mean, jump_vol):
    """Independent Poisson-mixture reference for the deterministic-variance Bates limit."""
    mean = exp(jump_mean + jump_vol**2 / 2) - 1
    return sum(
        exp(-intensity * maturity)
        * (intensity * maturity) ** n
        / factorial(n)
        * black_price(
            forward * exp(-intensity * mean * maturity + n * jump_mean + n * jump_vol**2 / 2),
            strike,
            maturity,
            sqrt(volatility**2 + n * jump_vol**2 / maturity),
        )
        for n in range(60)
    )
