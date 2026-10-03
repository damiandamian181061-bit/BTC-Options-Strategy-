"""Physical variance models, causal inputs, and tail scenarios. No option-implied probabilities.

Ensemble (equal weights; combination is more robust than picking a winner):
- direct multi-horizon log-HAR (Corsi 2009), one regression per horizon;
- HAR-X adding DVOL implied variance and downside semivariance (Patton & Sheppard 2015)
  when that history is supplied;
- EWMA(0.94) of daily realized variance;
- GJR-GARCH(1,1)-t on daily returns when its fit is healthy.
Last-day persistence is reported as a benchmark only: out of sample it is by far the worst.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from arch import arch_model

from .domain import ModelResult

HORIZONS = (1, 7, 14, 28, 45)
IV_BETA_DEFAULT, IV_VOL_DEFAULT = 0.0, 0.045


def _resample_returns(prices, cutoff):
    if not isinstance(prices.index, pd.DatetimeIndex) or prices.index.tz is None:
        raise ValueError("prices require a UTC-aware time index")
    if (prices.index > cutoff).any():
        raise ValueError("future prices in forecast input")
    sampled = prices.sort_index().resample("5min").last()
    return np.log(sampled).diff()


def _complete_days(series, returns, cutoff, min_coverage):
    coverage = returns.notna().resample("1D").sum() / 288
    complete = series.index + pd.Timedelta(days=1) <= pd.Timestamp(cutoff)
    return series[complete & (coverage.reindex(series.index) >= min_coverage)]


def realized_variance(prices: pd.Series, cutoff, min_coverage=0.90):
    returns = _resample_returns(prices, cutoff)
    rv = returns.pow(2).resample("1D").sum(min_count=1)
    return _complete_days(rv, returns, cutoff, min_coverage)


def downside_semivariance(prices: pd.Series, cutoff, min_coverage=0.90):
    returns = _resample_returns(prices, cutoff)
    rs = returns.clip(upper=0).pow(2).resample("1D").sum(min_count=1)
    return _complete_days(rs, returns, cutoff, min_coverage)


@dataclass
class ForecastBundle:
    cutoff: object
    daily_variance: np.ndarray
    residuals: np.ndarray
    models: list[ModelResult]
    iv_beta: float = IV_BETA_DEFAULT
    iv_vol: float = IV_VOL_DEFAULT
    iv_kappa: float = 0.0
    iv_target: float = 0.0

    def integrated(self, days):
        if days <= 0:
            raise ValueError("forecast horizon must be positive")
        n = max(1, int(np.ceil(days)))
        if n > len(self.daily_variance):
            raise ValueError("forecast horizon exceeds available steps")
        fraction = days - (n - 1)
        return float(self.daily_variance[: n - 1].sum() + fraction * self.daily_variance[n - 1])

    def return_paths(self, days, count=4096, seed=7):
        """Per-step log returns (steps, count); daily steps with a final fractional step.

        Filtered historical simulation: moving blocks of standardized residuals keep fat
        tails and some clustering, scaled by the forecast variance path, zero drift."""
        if days <= 0 or count < 1 or not len(self.residuals):
            raise ValueError("positive horizon/count and residual history are required")
        n = max(1, int(np.ceil(days)))
        if n > len(self.daily_variance):
            raise ValueError("scenario horizon exceeds forecast")
        rng = np.random.default_rng(seed)
        starts = rng.integers(0, len(self.residuals), count)
        steps = np.array([self.residuals[(starts + j) % len(self.residuals)] for j in range(n)])
        variance = self.daily_variance[:n].copy()
        variance[-1] *= days - (n - 1)
        return steps * np.sqrt(variance)[:, None] - variance[:, None] / 2

    def log_return_scenarios(self, days=1, count=4096, seed=7):
        return self.return_paths(days, count, seed).sum(axis=0)


def garch_health(params, forecast_variance, realized):
    """Short daily samples can converge to degenerate fits; reject them explicitly."""
    reasons = []
    if params["nu"] <= 2.5:
        reasons.append("Student-t tails too heavy (nu <= 2.5): variance barely finite")
    persistence = params["alpha[1]"] + params.get("gamma[1]", 0) / 2 + params["beta[1]"]
    if persistence >= 0.999:
        reasons.append("non-stationary persistence")
    if np.max(forecast_variance) > 4 * np.mean(realized):
        reasons.append("forecast exceeds 4x average realized variance")
    return reasons


def _trailing_contiguous(series):
    """Longest daily run ending at the latest observation; one bad day must not block all."""
    if series.empty:
        return series
    gaps = np.flatnonzero(series.index.to_series().diff().to_numpy() != pd.Timedelta(days=1))
    return series.iloc[gaps[-1] :] if len(gaps) else series


def _har_features(logv, extra):
    rows = []
    for i in range(29, len(logv)):
        row = [1, logv[i], logv[i - 6 : i + 1].mean(), logv[i - 29 : i + 1].mean()]
        rows.append(row + [e[i] for e in extra])
    return np.array(rows)


def direct_har(values, extra=(), horizons=HORIZONS):
    """Average daily variance over each horizon from a direct log-HAR regression."""
    logv = np.log(values)
    extra = [np.asarray(e, float) for e in extra]
    x = _har_features(logv, extra)
    out = {}
    for h in horizons:
        targets = np.array(
            [np.log(values[i + 1 : i + 1 + h].mean()) for i in range(29, len(values) - h)]
        )
        if len(targets) < 20 + x.shape[1]:
            continue
        design = x[: len(targets)]
        penalty = np.diag([1e-8] + [0.05] * (x.shape[1] - 1))
        beta = np.linalg.solve(design.T @ design + penalty, design.T @ targets)
        # Duan smearing: unbiased level from a log regression.
        smear = float(np.clip(np.mean(np.exp(targets - design @ beta)), 0.5, 3))
        predicted = float(np.clip(x[-1] @ beta, logv.min() - 1, logv.max() + 1))
        out[h] = np.exp(predicted) * smear
    if not out:
        raise ValueError("insufficient history for the HAR regression")
    return out


def path_from_horizons(averages, horizon):
    """Daily variance path whose integrals match each horizon's average variance."""
    hs = np.array(sorted(averages), float)
    integral = np.r_[0, hs * np.array([averages[h] for h in sorted(averages)])]
    grid = np.arange(horizon + 1, dtype=float)
    # Beyond the longest regression horizon the last average rate continues.
    rate = averages[max(averages)]
    cum = np.interp(grid, np.r_[0, hs], integral, right=np.nan)
    tail = grid > hs[-1]
    cum[tail] = integral[-1] + (grid[tail] - hs[-1]) * rate
    daily = np.diff(np.maximum.accumulate(cum))
    return np.maximum(daily, 0.05 * min(averages.values()))


def implied_dynamics(implied, returns):
    """Physical AR(1) for log DVOL: daily spot beta, residual vol, mean reversion and the
    long-run level as a log ratio to the latest DVOL."""
    default = (IV_BETA_DEFAULT, IV_VOL_DEFAULT, 0.0, 0.0)
    if implied is None:
        return default
    level = np.log(np.sqrt(implied))
    frame = pd.concat(
        [level.diff(), level.shift(1), returns.set_axis(returns.index - pd.Timedelta(days=1))],
        axis=1,
    ).dropna()
    if len(frame) < 60:
        return default
    dy, lag, ret = (frame.iloc[:, i].to_numpy() for i in range(3))
    mean = float(level.mean())
    design = np.column_stack([lag - mean, ret])
    coef, *_ = np.linalg.lstsq(design, dy - dy.mean(), rcond=None)
    # Short samples overstate mean reversion; cap it at a ~1-week half-life.
    kappa = float(np.clip(-coef[0], 0, 0.1))
    resid = dy - design @ coef
    return float(coef[1]), float(np.std(resid, ddof=3)), kappa, mean - float(level.iloc[-1])


def forecast(
    rv: pd.Series,
    returns: pd.Series,
    cutoff,
    horizon=60,
    minimum=60,
    downside: pd.Series | None = None,
    implied: pd.Series | None = None,
):
    """`implied` is daily DVOL variance per day (index = day measured, like `rv`)."""
    if rv.index.tz is None or returns.index.tz is None:
        raise ValueError("forecast data must be timezone aware")
    if (rv.index + pd.Timedelta(days=1) > cutoff).any() or (returns.index > cutoff).any():
        raise ValueError("unavailable observations in forecast training")
    for series in (downside, implied):
        if series is not None and (series.index + pd.Timedelta(days=1) > cutoff).any():
            raise ValueError("unavailable observations in forecast training")
    for index in (rv.index, returns.index):
        if index.has_duplicates or not index.is_monotonic_increasing:
            raise ValueError("daily forecast observations must be unique and ordered")
    rv, returns = _trailing_contiguous(rv), _trailing_contiguous(returns)
    values = rv.to_numpy(float)
    ret = returns.to_numpy(float)
    if len(values) < minimum or len(ret) < minimum or np.any(values <= 0):
        raise ValueError(f"need {minimum} completed daily variance/return observations")
    if not np.all(np.isfinite(values)) or not np.all(np.isfinite(ret)):
        raise ValueError("non-finite forecast inputs")

    har = direct_har(values)
    members = {"HAR": path_from_horizons(har, horizon)}
    harx_reasons = []
    extras = []
    if implied is not None and downside is not None:
        iv = implied.reindex(rv.index)
        ds = downside.reindex(rv.index)
        if iv.notna().all() and ds.notna().all() and (iv > 0).all():
            extras = [np.log(iv.to_numpy(float)), np.log(np.maximum(ds.to_numpy(float), 1e-10))]
        else:
            harx_reasons.append("implied/downside history does not cover the training window")
    else:
        harx_reasons.append("no DVOL/downside history supplied")
    harx = None
    if extras:
        harx = direct_har(values, extras)
        members["HAR-X"] = path_from_horizons(harx, horizon)

    ewma = values[0]
    for value in values[1:]:
        ewma = 0.94 * ewma + 0.06 * value
    members["EWMA"] = np.full(horizon, ewma)

    fitted = arch_model(ret * 100, mean="Zero", vol="GARCH", p=1, o=1, q=1, dist="t").fit(
        disp="off", show_warning=False
    )
    garch = fitted.forecast(horizon=horizon, reindex=False).variance.values[-1] / 10000
    if fitted.convergence_flag != 0 or not np.all(np.isfinite(garch)) or np.any(garch <= 0):
        garch_reasons = ["GARCH failed convergence/variance checks"]
    else:
        garch_reasons = garch_health(fitted.params, garch, values)
    if not garch_reasons:
        members["GJR-GARCH-t"] = garch
        scale = fitted.conditional_volatility / 100
    else:
        # A degenerate fit is reported, not averaged in; residuals use realized scale.
        scale = np.sqrt(np.mean(values))
    variance = np.mean(list(members.values()), axis=0)
    residuals = ret / np.maximum(scale, 1e-9)
    residuals -= residuals.mean()
    iv_beta, iv_vol, iv_kappa, iv_target = implied_dynamics(implied, returns)

    def result(name, version, one_day, reasons, included):
        return ModelResult(
            model=name,
            version=version,
            cutoff=cutoff,
            values={"one_day_variance": float(one_day), "in_ensemble": float(included)},
            healthy=not reasons,
            reasons=reasons,
            measure="physical",
        )

    results = [
        result("HAR", "log-har-direct-1", har[min(har)], [], True),
        result(
            "HAR-X",
            "har-dvol-semivar-direct-1",
            harx[min(harx)] if harx else 0,
            harx_reasons,
            bool(harx),
        ),
        result(
            "GJR-GARCH-t",
            "gjr11t-1",
            garch[0] if np.isfinite(garch[0]) else 0,
            garch_reasons,
            not garch_reasons,
        ),
        result("EWMA", "ewma094-1", ewma, [], True),
        result("persistence", "last-completed-rv-1", values[-1], [], False),
        ModelResult(
            model="ensemble",
            version="equal-weight-2",
            cutoff=cutoff,
            values={
                "one_day_variance": float(variance[0]),
                "members": float(len(members)),
                "iv_beta": iv_beta,
                "iv_daily_vol": iv_vol,
                "iv_kappa": iv_kappa,
                "iv_target_log_ratio": iv_target,
            },
            healthy=True,
            measure="physical",
        ),
    ]
    return ForecastBundle(
        cutoff, variance, residuals, results, iv_beta, iv_vol, iv_kappa, iv_target
    )
