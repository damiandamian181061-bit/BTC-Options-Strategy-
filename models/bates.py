"""
Bates (1996) model: Heston stochastic volatility + Merton-style log-normal jumps.

Adds a compound Poisson jump component to the Heston diffusion:
  jump arrival rate:  lambda_j  (jumps per year)
  jump size:          ln(1+J) ~ N(mu_j, delta_j^2)

The risk-neutral drift is compensated so the discounted price process
remains a martingale: the diffusion drift uses r_eff = r - lambda_j*mean_jump,
where mean_jump = exp(mu_j + 0.5*delta_j^2) - 1, and the (uncompensated)
jump characteristic function is multiplied on top. This is algebraically
identical to embedding the compensator directly inside the jump CF (the
more commonly printed textbook form) — see the derivation note in the
project README.

Same vectorized fixed-quadrature approach as heston.py: phi(u) is evaluated
once per maturity and reused across all strikes.
"""
import numpy as np
from models.heston import _phi_j_CD, _U, _W

_I = 1j


def _jump_log_cf(u, T, lambda_j, mu_j, delta_j):
    return lambda_j * T * (np.exp(_I * u * mu_j - 0.5 * u ** 2 * delta_j ** 2) - 1)


def bates_prices_for_strikes(S0, K_array, T, r, v0, kappa, theta, sigma_v, rho,
                              lambda_j, mu_j, delta_j, option_type="call"):
    """Vectorized Bates prices for an array of strikes at one maturity T.
    option_type: 'call' or 'put' (puts via put-call parity)."""
    K_array = np.atleast_1d(np.asarray(K_array, dtype=float))
    x = np.log(S0)
    lnK = np.log(K_array)

    mean_jump = np.exp(mu_j + 0.5 * delta_j ** 2) - 1
    r_eff = r - lambda_j * mean_jump
    jump_log = _jump_log_cf(_U, T, lambda_j, mu_j, delta_j)  # (N_NODES,)

    P = {}
    for j in (1, 2):
        C, D = _phi_j_CD(_U, T, r_eff, kappa, theta, sigma_v, rho, j)
        phi = np.exp(C + D * v0 + jump_log + _I * _U * x)
        phase = np.exp(-_I * np.outer(_U, lnK))
        integrand = (phase * (phi / (_I * _U))[:, None]).real
        integral = _W @ integrand
        P[j] = 0.5 + integral / np.pi

    call_prices = S0 * P[1] - K_array * np.exp(-r * T) * P[2]
    call_prices = np.maximum(call_prices, 0.0)
    if option_type == "call":
        return call_prices
    put_prices = call_prices - S0 + K_array * np.exp(-r * T)  # put-call parity, q=0
    return np.maximum(put_prices, 0.0)


def bates_call_price(S0, K, T, r, v0, kappa, theta, sigma_v, rho, lambda_j, mu_j, delta_j):
    """Single-strike convenience wrapper (used for one-off sanity checks)."""
    return float(bates_prices_for_strikes(S0, [K], T, r, v0, kappa, theta, sigma_v, rho,
                                           lambda_j, mu_j, delta_j)[0])
