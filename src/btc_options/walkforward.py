"""Temporal structural-model comparison: fit earlier, score later executable quotes."""

import numpy as np

from .calibration import calibrate
from .pricing import YEAR_SECONDS, implied_vol, structural_price


def compare(snapshots, model="heston", stride=1):
    if len(snapshots) < 3 or stride < 1:
        raise ValueError("at least three chronological snapshots and positive stride required")
    rows = []
    previous = None
    for index in range(0, len(snapshots) - 1, stride):
        training, testing = snapshots[index], snapshots[index + 1]
        if training.asof >= testing.asof:
            raise ValueError("walk-forward ordering violation")
        fitted = calibrate(training, model, previous)
        drift = (
            0
            if previous is None
            else max(
                abs(fitted.parameters[k] - previous[k]) / max(abs(previous[k]), 0.1)
                for k in previous
            )
        )
        previous = fitted.parameters
        vols = {}
        for name, q in training.quotes.items():
            expiry = training.instruments[name].expiry
            vols.setdefault(expiry, []).append(q.iv)
        model_error, baseline_error = [], []
        for name, q in testing.quotes.items():
            inst = testing.instruments[name]
            if inst.settlement != "USDC" or inst.expiry not in vols or not q.valid:
                continue
            if (
                (testing.asof - q.exchange_time).total_seconds() > 15
                or q.bid_size <= 0
                or q.ask_size <= 0
            ):
                continue
            t = (inst.expiry - testing.asof).total_seconds() / YEAR_SECONDS
            if t <= 0:
                continue
            observed = implied_vol(
                (q.bid + q.ask) / 2, q.forward, inst.strike, t, option_type=inst.option_type
            )
            value = structural_price(
                q.forward, inst.strike, t, fitted.parameters, inst.option_type, model
            )
            if not value.healthy or not np.isfinite(observed):
                continue
            predicted = implied_vol(
                value.price, q.forward, inst.strike, t, option_type=inst.option_type
            )
            flat = float(np.median(vols[inst.expiry]))
            if np.isfinite(predicted):
                model_error.append((predicted - observed) ** 2)
                baseline_error.append((flat - observed) ** 2)
        rows.append(
            dict(
                train_cutoff=training.asof.isoformat(),
                test_cutoff=testing.asof.isoformat(),
                observations=len(model_error),
                calibration_healthy=fitted.result.healthy,
                max_parameter_relative_change=drift,
                model_iv_rmse=float(np.sqrt(np.mean(model_error))) if model_error else None,
                black_iv_rmse=float(np.sqrt(np.mean(baseline_error))) if baseline_error else None,
            )
        )
    valid = [r for r in rows if r["observations"] > 0 and r["calibration_healthy"]]
    return dict(
        model=model,
        version="compensated-forward-2",
        measure="risk_neutral",
        folds=rows,
        beats_baseline_in_all_valid_folds=bool(valid)
        and len(valid) == len(rows)
        and all(r["model_iv_rmse"] < r["black_iv_rmse"] for r in valid),
        promoted=False,
        limitations=[
            "No structural model is promoted automatically.",
            "Require expiry/event coverage and parameter stability review.",
        ],
    )
