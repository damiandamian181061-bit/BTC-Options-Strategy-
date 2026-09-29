"""
Heston (1993) stochastic volatility model.

dS_t = r*S_t*dt + sqrt(v_t)*S_t*dW1_t
dv_t = kappa*(theta - v_t)*dt + sigma_v*sqrt(v_t)*dW2_t
corr(dW1, dW2) = rho

Pricing via the semi-closed-form characteristic function (Little Trap
formulation, Albrecher et al. 2007 — numerically stable, avoids the
branch-cut issues of the original 1993 formula).

Performance note: the characteristic function phi_j(u) depends only on
(T, model params), not on the strike K. A strike only enters through the
cheap exp(-i*u*ln(K)) term. So for calibration (many strikes, same T), we
evaluate phi_j(u) ONCE on a fixed quadrature grid and reuse it across all
strikes — this is what makes calibrate.py fast enough to run in seconds
rather than minutes. Fixed 64-point Gauss-Legendre replaces adaptive
scipy.integrate.quad, which was the bottleneck (a fresh adaptive
integration per strike, per candidate parameter set, during calibration).
"""
import numpy as np

_N_NODES = 64
_U_MIN = 1e-4
_U_MAX = 120.0
_nodes, _weights = np.polynomial.legendre.leggauss(_N_NODES)
_U = 0.5 * (_U_MAX - _U_MIN) * _nodes + 0.5 * (_U_MAX + _U_MIN)
_W = 0.5 * (_U_MAX - _U_MIN) * _weights


def _phi_j_CD(u, T, r, kappa, theta, sigma_v, rho, j):
    """Vectorized C(u), D(u) terms of the characteristic function (S0-independent)."""
    i = 1j
    if j == 1:
        b = kappa - rho * sigma_v
        u_j = 0.5
    else:
        b = kappa
        u_j = -0.5

    a = kappa * theta
    d = np.sqrt((rho * sigma_v * i * u - b) ** 2 - sigma_v ** 2 * (2 * u_j * i * u - u ** 2))
    g = (b - rho * sigma_v * i * u - d) / (b - rho * sigma_v * i * u + d)

    exp_dT = np.exp(-d * T)
    C = r * i * u * T + (a / sigma_v ** 2) * (
        (b - rho * sigma_v * i * u - d) * T - 2 * np.log((1 - g * exp_dT) / (1 - g))
    )
    D = ((b - rho * sigma_v * i * u - d) / sigma_v ** 2) * ((1 - exp_dT) / (1 - g * exp_dT))
    return C, D


def heston_prices_for_strikes(S0, K_array, T, r, v0, kappa, theta, sigma_v, rho, option_type="call"):
    """Vectorized Heston prices for an ARRAY of strikes at a single maturity T.
    option_type: 'call' or 'put' (puts via put-call parity, since P1/P2 already give the call)."""
    i = 1j
    K_array = np.atleast_1d(np.asarray(K_array, dtype=float))
    x = np.log(S0)
    lnK = np.log(K_array)

    P = {}
    for j in (1, 2):
        C, D = _phi_j_CD(_U, T, r, kappa, theta, sigma_v, rho, j)
        phi = np.exp(C + D * v0 + i * _U * x)          # shape (N_NODES,)
        # integrand for each strike: Re[ exp(-i*u*lnK) * phi(u) / (i*u) ]
        phase = np.exp(-i * np.outer(_U, lnK))          # (N_NODES, N_strikes)
        integrand = (phase * (phi / (i * _U))[:, None]).real
        integral = _W @ integrand                        # (N_strikes,)
        P[j] = 0.5 + integral / np.pi

    call_prices = S0 * P[1] - K_array * np.exp(-r * T) * P[2]
    call_prices = np.maximum(call_prices, 0.0)
    if option_type == "call":
        return call_prices
    put_prices = call_prices - S0 + K_array * np.exp(-r * T)  # put-call parity, q=0
    return np.maximum(put_prices, 0.0)


def heston_call_price(S0, K, T, r, v0, kappa, theta, sigma_v, rho):
    """Single-strike convenience wrapper (used for one-off sanity checks)."""
    return float(heston_prices_for_strikes(S0, [K], T, r, v0, kappa, theta, sigma_v, rho)[0])


def feller_satisfied(kappa, theta, sigma_v):
    """Feller condition: 2*kappa*theta > sigma_v^2 keeps v_t strictly positive."""
    return 2 * kappa * theta > sigma_v ** 2
