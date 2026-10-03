"""Fast fixed-quadrature Heston and Bates prices on a forward numeraire.

The characteristic function depends only on maturity and parameters, so it is
evaluated once per maturity and reused across strikes. Calibration uses these
vectorized prices; `pricing.structural_price` provides the adaptive reference.

Heston uses the "little trap" formulation (Albrecher et al. 2007). Bates adds
log-normal jumps whose compensated transform differs between the share-measure
(P1) and money-market-measure (P2) branches.
"""

import numpy as np

_nodes, _weights = np.polynomial.legendre.leggauss(128)
_U_MIN, _U_MAX = 1e-9, 200.0
_U = 0.5 * (_U_MAX - _U_MIN) * _nodes + 0.5 * (_U_MAX + _U_MIN)
_W = 0.5 * (_U_MAX - _U_MIN) * _weights


def heston_cd(u, maturity, kappa, theta, sigma_v, rho, branch):
    """C(u), D(u) of the Heston characteristic function for branch P1 or P2."""
    b = kappa - rho * sigma_v if branch == 1 else kappa
    u_j = 0.5 if branch == 1 else -0.5
    iu = 1j * u
    d = np.sqrt((rho * sigma_v * iu - b) ** 2 - sigma_v**2 * (2 * u_j * iu - u**2))
    g = (b - rho * sigma_v * iu - d) / (b - rho * sigma_v * iu + d)
    decay = np.exp(-d * maturity)
    c = (kappa * theta / sigma_v**2) * (
        (b - rho * sigma_v * iu - d) * maturity - 2 * np.log((1 - g * decay) / (1 - g))
    )
    dv = ((b - rho * sigma_v * iu - d) / sigma_v**2) * ((1 - decay) / (1 - g * decay))
    return c, dv


def jump_log_cf(u, maturity, lambda_j, mu_j, delta_j, branch):
    """Compensated Merton jump term; P1 uses the exponentially tilted jump law."""
    g = (1.0 if branch == 1 else 0.0) + 1j * u
    mean = np.exp(mu_j + 0.5 * delta_j**2) - 1
    return lambda_j * maturity * (np.exp(mu_j * g + 0.5 * delta_j**2 * g**2) - 1 - g * mean)


def prices_for_strikes(
    forward,
    strikes,
    maturity,
    v0,
    kappa,
    theta,
    sigma_v,
    rho,
    lambda_j=0.0,
    mu_j=0.0,
    delta_j=0.0,
    option_type="call",
):
    """Undiscounted prices for many strikes at one maturity. Invalid values become NaN."""
    strikes = np.atleast_1d(np.asarray(strikes, dtype=float))
    phase = np.exp(-1j * np.outer(_U, np.log(strikes / forward)))
    probabilities = []
    for branch in (1, 2):
        c, dv = heston_cd(_U, maturity, kappa, theta, sigma_v, rho, branch)
        log_cf = c + dv * v0
        if lambda_j:
            log_cf = log_cf + jump_log_cf(_U, maturity, lambda_j, mu_j, delta_j, branch)
        integrand = (phase * (np.exp(log_cf) / (1j * _U))[:, None]).real
        probabilities.append(0.5 + (_W @ integrand) / np.pi)
    calls = forward * probabilities[0] - strikes * probabilities[1]
    intrinsic = np.maximum(forward - strikes, 0)
    tolerance = 1e-8 * max(forward, float(np.max(strikes)))
    invalid = (calls < intrinsic - tolerance) | (calls > forward + tolerance)
    calls = np.where(invalid, np.nan, np.maximum(calls, intrinsic))
    if option_type == "call":
        return calls
    return np.where(invalid, np.nan, np.maximum(calls - forward + strikes, 0.0))
