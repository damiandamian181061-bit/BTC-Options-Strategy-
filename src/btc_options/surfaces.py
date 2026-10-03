"""Extended SSVI (per-slice skew) with calendar/butterfly no-arbitrage by construction.

Hendriks & Martini (2019): slices (theta_i, psi_i = theta_i * phi_i, rho_i) are free of
calendar arbitrage when theta and psi are non-decreasing and
|rho_{i+1} psi_{i+1} - rho_i psi_i| <= psi_{i+1} - psi_i. Gatheral & Jacquier (2014)
butterfly sufficiency: psi (1 + |rho|) < 4 and psi^2 (1 + |rho|) <= 4 theta.
"""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from .pricing import black_price, black_greeks

VERSION = "essvi-hm-1"


@dataclass
class SSVI:
    maturities: np.ndarray
    theta: np.ndarray
    psi: np.ndarray
    rho: np.ndarray
    fit_rmse: float = 0
    k_min: float = -0.5
    k_max: float = 0.5
    slice_rmse: np.ndarray | None = None

    def slice_error(self, maturity):
        """Precision-weighted IV residual RMS of the bracketing slices (global RMSE fallback)."""
        if self.slice_rmse is None:
            return self.fit_rmse
        return float(np.interp(maturity, self.maturities, self.slice_rmse))

    def _params(self, maturity):
        maturity = np.asarray(maturity, float)
        if np.any(maturity < self.maturities[0] - 1e-12) or np.any(
            maturity > self.maturities[-1] + 1e-12
        ):
            raise ValueError("surface maturity extrapolation is not authorized")
        theta = np.interp(maturity, self.maturities, self.theta)
        # Linear in theta for (psi, rho*psi) preserves the slice no-arbitrage conditions.
        psi = np.interp(theta, self.theta, self.psi)
        rho = np.interp(theta, self.theta, self.rho * self.psi) / psi
        return theta, psi, rho

    def total_variance(self, k, maturity):
        theta, psi, rho = self._params(maturity)
        x = psi / theta * np.asarray(k)
        return theta / 2 * (1 + rho * x + np.sqrt((x + rho) ** 2 + 1 - rho**2))

    def volatility(self, strike, forward, maturity, clip=False):
        """`clip` holds vol flat beyond the calibrated domain (scenario revaluation only)."""
        k = np.log(np.asarray(strike) / forward)
        maturity = np.asarray(maturity, float)
        if clip:
            k = np.clip(k, self.k_min, self.k_max)
            maturity = np.clip(maturity, self.maturities[0], self.maturities[-1])
        elif np.any(k < self.k_min) or np.any(k > self.k_max):
            raise ValueError("strike outside calibrated surface domain")
        return np.sqrt(self.total_variance(k, maturity) / maturity)

    def price(self, forward, strike, maturity, option_type="call"):
        return black_price(
            forward,
            strike,
            maturity,
            self.volatility(strike, forward, maturity),
            option_type=option_type,
        )

    def residual_band(self, strike, forward, maturity, multiple=2):
        """Fit-residual band, not a calibrated statistical confidence interval."""
        vol = self.volatility(strike, forward, maturity)
        return np.maximum(vol - multiple * self.fit_rmse, 0.0001), vol + multiple * self.fit_rmse

    def greeks(self, forward, strike, maturity, option_type="call"):
        """Sticky log-moneyness spot bumps and parallel market-IV vega, per underlying unit."""
        vol = float(self.volatility(strike, forward, maturity))
        base = black_greeks(forward, strike, maturity, vol, option_type=option_type)
        bump = forward * 1e-4
        up = self.price(forward + bump, strike, maturity, option_type)
        down = self.price(forward - bump, strike, maturity, option_type)
        value = self.price(forward, strike, maturity, option_type)
        base["delta"] = float((up - down) / (2 * bump))
        base["gamma"] = float((up - 2 * value + down) / bump**2)
        return base

    def health(self, tol=1e-9):
        a = self.rho * self.psi
        scale = 1 + np.abs(self.rho)
        return bool(
            np.all(self.theta > 0)
            and np.all(np.diff(self.theta) >= -tol)
            and np.all(np.diff(self.psi) >= -tol)
            and np.all(np.abs(self.rho) < 1)
            and np.all(np.abs(np.diff(a)) <= np.diff(self.psi) + tol)
            and np.all(self.psi * scale < 4)
            and np.all(self.psi**2 * scale <= 4 * self.theta * (1 + 1e-6))
        )

    def as_dict(self):
        return dict(
            maturities=self.maturities.tolist(),
            theta=self.theta.tolist(),
            psi=self.psi.tolist(),
            rho=self.rho.tolist(),
            fit_rmse=self.fit_rmse,
            slice_rmse=None if self.slice_rmse is None else self.slice_rmse.tolist(),
            k_min=self.k_min,
            k_max=self.k_max,
            healthy=self.health(),
            version=VERSION,
        )


def _unpack(x, n):
    theta = np.cumsum(np.exp(x[:n]))
    psi = np.cumsum(np.exp(x[n : 2 * n]))
    steps = np.tanh(x[2 * n :])
    a = np.empty(n)
    a[0] = psi[0] * steps[0]
    for i in range(1, n):
        a[i] = a[i - 1] + (psi[i] - psi[i - 1]) * steps[i]
    return theta, psi, a / psi


def fit_ssvi(maturities, log_moneyness, iv, weights=None):
    t = np.asarray(maturities, float)
    k = np.asarray(log_moneyness, float)
    iv = np.asarray(iv, float)
    if len(t) < 6 or not (len(t) == len(k) == len(iv)) or np.any(t <= 0):
        raise ValueError("surface requires at least six valid synchronized quotes")
    if not np.all(np.isfinite(iv)) or np.any(iv <= 0):
        raise ValueError("invalid implied volatility")
    w = np.ones_like(t) if weights is None else np.asarray(weights, float)
    if len(w) != len(t) or np.any(w <= 0) or not np.all(np.isfinite(w)):
        raise ValueError("invalid surface weights")
    w = w / w.mean()
    unique = np.unique(t)
    n = len(unique)
    index = np.searchsorted(unique, t)
    observed = iv**2 * t
    atm = []
    for i in range(n):
        mask = index == i
        near = np.argsort(np.abs(k[mask]))[:3]
        atm.append(np.median(observed[mask][near]))
    theta0 = np.maximum.accumulate(np.maximum(atm, 1e-6))
    psi0 = np.maximum.accumulate(np.minimum(np.sqrt(theta0), 1.5))
    x0 = np.r_[
        np.log(np.maximum(np.diff(np.r_[0, theta0]), 1e-7)),
        np.log(np.maximum(np.diff(np.r_[0, psi0]), 1e-5)),
        np.full(n, np.arctanh(-0.1)),
    ]
    lower = np.r_[np.full(2 * n, -18.0), np.full(n, -3.0)]
    upper = np.r_[np.full(2 * n, 3.0), np.full(n, 3.0)]

    def residual(x):
        theta, psi, rho = _unpack(x, n)
        th, ps, rh = theta[index], psi[index], rho[index]
        xk = ps / th * k
        model = th / 2 * (1 + rh * xk + np.sqrt((xk + rh) ** 2 + 1 - rh**2))
        fit = (np.sqrt(np.maximum(model, 1e-12) / t) - iv) * np.sqrt(w)
        scale = 1 + np.abs(rho)
        # Margins keep the optimum strictly inside the butterfly-free region.
        penalty = np.r_[
            np.maximum(0, psi * scale - 3.96),
            np.maximum(0, psi**2 * scale - 3.96 * theta) / np.maximum(theta, 1e-6),
        ]
        return np.r_[fit, 10 * penalty]

    result = least_squares(
        residual,
        np.clip(x0, lower, upper),
        bounds=(lower, upper),
        loss="soft_l1",
        f_scale=0.02,
        max_nfev=2000,
    )
    theta, psi, rho = _unpack(result.x, n)
    s = SSVI(unique, theta, psi, rho, k_min=float(k.min()), k_max=float(k.max()))
    error = np.sqrt(s.total_variance(k, t) / t) - iv
    s.fit_rmse = float(np.sqrt(np.mean(error**2)))
    s.slice_rmse = np.array(
        [np.sqrt(np.average(error[index == i] ** 2, weights=w[index == i])) for i in range(n)]
    )
    if result.status <= 0 or not s.health():
        raise ValueError("surface failed calibration health checks")
    return s
