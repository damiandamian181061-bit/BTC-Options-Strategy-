"""Bid/ask-scaled structural calibration with holdouts and adaptive verification."""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from .domain import ModelResult
from .pricing import YEAR_SECONDS, implied_vol, structural_price
from .structural import prices_for_strikes


@dataclass
class Calibration:
    parameters: dict[str, float]
    result: ModelResult


def calibrate(snapshot, model="heston", previous=None):
    if model not in ("heston", "bates"):
        raise ValueError("unsupported model")
    rows = []
    for name, q in snapshot.quotes.items():
        i = snapshot.instruments[name]
        t = (i.expiry - snapshot.asof).total_seconds() / YEAR_SECONDS
        if t <= 0 or not q.valid or q.bid_size <= 0 or q.ask_size <= 0:
            continue
        # Expiring wings are ill-conditioned for fixed inversion and do not match
        # this platform's 14–45 day strategy. Reject them rather than clipping prices.
        if t * 365 < 7 or not 0.01 <= abs(q.delta) <= 0.99 or i.settlement != "USDC":
            continue
        if (snapshot.asof - q.exchange_time).total_seconds() > 15:
            continue
        # Normalize by the explicit expiry forward, with zero numeraire rate.
        k = i.strike / q.forward
        mid = (q.bid + q.ask) / 2 / q.forward
        if i.option_type == "put":
            mid += 1 - k
        if not max(1 - k, 0) < mid < 1 or not np.isfinite(implied_vol(mid, 1, k, t)):
            continue
        rows.append((t, k, mid, max((q.ask - q.bid) / q.forward, 1e-5), q.iv))
    if len(rows) < 12:
        raise ValueError("structural validation needs at least 12 liquid observations")
    rows = np.array(sorted(rows, key=lambda r: (r[0], r[1])))
    test = np.arange(len(rows)) % 3 == 1
    train = ~test
    names = ["v0", "kappa", "theta", "sigma_v", "rho"]
    x0 = [0.3, 2, 0.3, 0.6, -0.3]
    lower, upper = [1e-4, 0.05, 1e-4, 0.02, -0.98], [4, 20, 4, 4, 0.98]
    if model == "bates":
        names += ["lambda_j", "mu_j", "delta_j"]
        x0 += [0.5, -0.1, 0.2]
        lower += [0, -0.8, 0.001]
        upper += [5, 0.5, 1]
    if previous:
        x0 = [previous[n] for n in names]
    x0 = np.clip(x0, np.array(lower) + 1e-8, np.array(upper) - 1e-8)

    def prices(x, data):
        p = dict(zip(names, x, strict=True))
        result = np.empty(len(data))
        for maturity in np.unique(data[:, 0]):
            select = data[:, 0] == maturity
            result[select] = prices_for_strikes(1.0, data[select, 1], maturity, **p)
        for index in np.flatnonzero(~np.isfinite(result)):
            maturity, strike = data[index, :2]
            checked = structural_price(1, strike, maturity, p, model=model)
            if checked.healthy:
                result[index] = checked.price
        return result

    def residual(x):
        predicted = prices(x, rows[train])
        if not np.all(np.isfinite(predicted)):
            return np.full(train.sum() + len(names), 1e6)
        regularizer = 0.05 * (x - x0) / (np.array(upper) - np.array(lower))
        return np.r_[(predicted - rows[train, 2]) / rows[train, 3], regularizer]

    fit = least_squares(residual, x0, bounds=(lower, upper), loss="soft_l1", max_nfev=250)
    params = dict(zip(names, map(float, fit.x), strict=True))
    holdout = rows[test]
    model_iv, black_iv, errors = [], [], []
    for t, k, _mid, _spread, _iv in holdout:
        checked = structural_price(1, k, t, params, model=model)
        errors.append(checked.numerical_error)
        model_iv.append(implied_vol(checked.price, 1, k, t))
        matched = rows[train & (rows[:, 0] == t)]
        black_iv.append(
            float(np.median(matched[:, 4])) if len(matched) else float(np.median(rows[train, 4]))
        )
    observed = np.array([implied_vol(r[2], 1, r[1], r[0]) for r in holdout])
    rmse = float(np.sqrt(np.mean((np.asarray(model_iv) - observed) ** 2)))
    baseline = float(np.sqrt(np.mean((np.asarray(black_iv) - observed) ** 2)))
    healthy = bool(fit.success and np.isfinite(rmse) and max(errors) < 1e-7)
    reasons = []
    if not healthy:
        reasons.append("optimizer, IV inversion, or adaptive quadrature failed")
    if rmse >= baseline:
        reasons.append("strike holdout did not beat per-expiry flat Black")
    if params["sigma_v"] ** 2 > 2 * params["kappa"] * params["theta"]:
        reasons.append("Feller condition violated; variance can approach zero")
    if np.any(np.isclose(fit.x, lower, rtol=0, atol=1e-3)) or np.any(
        np.isclose(fit.x, upper, rtol=0, atol=1e-3)
    ):
        reasons.append("parameter near calibration bound")
    result = ModelResult(
        model=model,
        version="compensated-forward-2",
        cutoff=snapshot.asof,
        values={
            **params,
            "holdout_iv_rmse": rmse if np.isfinite(rmse) else 99.0,
            "black_holdout_iv_rmse": baseline if np.isfinite(baseline) else 99.0,
            "max_quadrature_error": float(max(errors)),
            "training_cost": float(fit.cost),
        },
        healthy=healthy,
        reasons=reasons,
        measure="risk_neutral",
    )
    return Calibration(params, result)
