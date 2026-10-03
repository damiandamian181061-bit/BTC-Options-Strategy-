"""Rough Bergomi research challenger using a hybrid Volterra simulation and antithetics.

This research estimator does not authorize trading. Discretization error must be
measured separately from the reported sampling error by refining the time grid.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class RoughResult:
    price: float
    standard_error: float
    martingale_ratio: float
    paths: int
    steps: int
    seed: int


def rough_bergomi(
    forward,
    strike,
    maturity,
    variance=0.49,
    hurst=0.10,
    eta=1.5,
    rho=-0.6,
    paths=10000,
    steps=128,
    seed=7,
    option_type="call",
):
    if not (0 < hurst < 0.5 and abs(rho) < 1 and maturity > 0 and variance > 0):
        raise ValueError("invalid rough Bergomi domain")
    if paths < 100 or paths % 2 or steps < 8 or option_type not in ("call", "put"):
        raise ValueError("even paths >=100, steps >=8, and a valid option type are required")
    rng = np.random.default_rng(seed)
    dt = maturity / steps
    half = paths // 2
    z = rng.normal(size=(half, steps))
    independent = rng.normal(size=(half, steps))
    near_noise = rng.normal(size=(half, steps))
    z = np.r_[z, -z]
    independent = np.r_[independent, -independent]
    near_noise = np.r_[near_noise, -near_noise]
    dw = np.sqrt(dt) * z
    alpha = hurst - 0.5
    # Exact singular last-cell integral, jointly sampled with its Brownian increment.
    covariance = dt ** (alpha + 1) / (alpha + 1)
    variance_cell = dt ** (2 * hurst) / (2 * hurst)
    near = covariance / dt * dw + np.sqrt(max(variance_cell - covariance**2 / dt, 0)) * near_noise
    volterra = np.zeros((paths, steps))
    for i in range(1, steps):
        value = near[:, i - 1].copy()
        for j in range(i - 1):
            lag = i - j
            cell = ((lag * dt) ** (alpha + 1) - ((lag - 1) * dt) ** (alpha + 1)) / (
                (alpha + 1) * dt
            )
            value += cell * dw[:, j]
        volterra[:, i] = np.sqrt(2 * hurst) * value
    times = np.arange(steps) * dt
    v = variance * np.exp(eta * volterra - 0.5 * eta**2 * times ** (2 * hurst))
    asset_dw = rho * dw + np.sqrt(1 - rho**2) * np.sqrt(dt) * independent
    terminal = forward * np.exp((np.sqrt(v) * asset_dw - 0.5 * v * dt).sum(axis=1))
    sign = 1 if option_type == "call" else -1
    payoff = np.maximum(sign * (terminal - strike), 0)
    # Control variate uses a disjoint pilot so its fitted coefficient does not bias evaluation.
    pilot = max(20, half // 5)
    pilot_pair = (payoff[:pilot] + payoff[half : half + pilot]) / 2
    control_pair = (terminal[:pilot] + terminal[half : half + pilot]) / 2
    beta = np.cov(pilot_pair, control_pair)[0, 1] / max(np.var(control_pair, ddof=1), 1e-12)
    pairs = (payoff[pilot:half] + payoff[half + pilot :]) / 2
    controls = (terminal[pilot:half] + terminal[half + pilot :]) / 2
    adjusted = pairs - beta * (controls - forward)
    return RoughResult(
        float(adjusted.mean()),
        float(adjusted.std(ddof=1) / np.sqrt(len(adjusted))),
        float(terminal.mean() / forward),
        paths,
        steps,
        seed,
    )
