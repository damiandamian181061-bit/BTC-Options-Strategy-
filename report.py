"""
Shared reporting: implied-vol smile plotting and Greeks computation/plotting.
Used by both main.py (synthetic data) and run_on_real_data.py (live Deribit
data) so the two entry points stay in sync rather than duplicating logic.
"""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from models.black_scholes import bs_price, implied_vol
from models.heston import heston_prices_for_strikes
from models.bates import bates_prices_for_strikes
from greeks import compute_greeks

COLORS = {"Black-Scholes": "tab:red", "Heston": "tab:green", "Bates": "tab:blue"}


def model_ivs_for_maturity(model_name, params, K_arr, T, S0, r, option_type):
    if model_name == "Black-Scholes":
        prices = np.array([bs_price(S0, K, T, r, params["sigma"], option_type) for K in K_arr])
    elif model_name == "Heston":
        prices = heston_prices_for_strikes(S0, K_arr, T, r, option_type=option_type, **params)
    elif model_name == "Bates":
        prices = bates_prices_for_strikes(S0, K_arr, T, r, option_type=option_type, **params)
    return np.array([implied_vol(p, S0, K, T, r, option_type) for p, K in zip(prices, K_arr)])


def fit_quality_summary(df, S0, r, results):
    has_S0_col = "S0" in df.columns
    rows = []
    for name, params in results.items():
        model_iv_all, mkt_iv_all = [], []
        for (T, otype), sub in df.groupby(["T", "option_type"]):
            group_S0 = sub["S0"].iloc[0] if has_S0_col else S0
            model_iv_all.append(model_ivs_for_maturity(name, params, sub.K.values, T, group_S0, r, otype))
            mkt_iv_all.append(sub.market_iv.values)
        model_iv_all = np.concatenate(model_iv_all)
        mkt_iv_all = np.concatenate(mkt_iv_all)
        valid = ~np.isnan(model_iv_all) & ~np.isnan(mkt_iv_all)
        rmse = np.sqrt(np.mean((model_iv_all[valid] - mkt_iv_all[valid]) ** 2))
        rows.append({"model": name, "iv_rmse_vol_pts": round(rmse * 100, 3)})
    return pd.DataFrame(rows).sort_values("iv_rmse_vol_pts")


def plot_smile(df, S0, r, results, outfile, market_label="Market", suptitle=None):
    has_S0_col = "S0" in df.columns
    maturities = sorted(df["T"].unique())
    fig, axes = plt.subplots(1, len(maturities), figsize=(5 * len(maturities), 4.5), sharey=True)
    if len(maturities) == 1:
        axes = [axes]

    for ax, T in zip(axes, maturities):
        sub = df[df["T"] == T].sort_values("moneyness")
        group_S0 = sub["S0"].iloc[0] if has_S0_col else S0
        ax.plot(sub.moneyness, sub.market_iv * 100, "ko", label=market_label, zorder=5)
        for name, params in results.items():
            parts = []
            for otype, g in sub.groupby("option_type"):
                iv = model_ivs_for_maturity(name, params, g.K.values, T, group_S0, r, otype)
                parts.append(pd.Series(iv, index=g.index))
            model_iv = pd.concat(parts).loc[sub.index]
            ax.plot(sub.moneyness, model_iv.values * 100, "-", color=COLORS[name], label=name)
        ax.axvline(1.0, color="gray", lw=0.8, ls=":", alpha=0.6)
        ax.set_title(f"{int(round(T * 365))}d to expiry")
        ax.set_xlabel("Moneyness (K/S0)")
        ax.grid(alpha=0.3)

    axes[0].set_ylabel("Implied vol (%)")
    axes[0].legend(fontsize=9)
    fig.suptitle(suptitle or "Implied Volatility Smile: Market vs. Calibrated Models", fontsize=13)
    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    plt.close(fig)


def compute_greeks_table(df, S0, r, results):
    """Greeks for every (model, maturity, option_type, strike) combination
    already in df — reuses the same grid the smile was fit on. Returns a
    long-format DataFrame: T, K, moneyness, option_type, model, price,
    delta, gamma, theta, vega."""
    has_S0_col = "S0" in df.columns
    rows = []
    for name, params in results.items():
        for (T, otype), sub in df.groupby(["T", "option_type"]):
            group_S0 = sub["S0"].iloc[0] if has_S0_col else S0
            g = compute_greeks(name, params, r, group_S0, sub.K.values, T, otype)
            for i, row in enumerate(sub.itertuples()):
                rows.append(dict(
                    T=T, K=row.K, moneyness=row.moneyness, option_type=otype, model=name,
                    price=g["price"][i], delta=g["delta"][i], gamma=g["gamma"][i],
                    theta=g["theta"][i], vega=g["vega"][i],
                ))
    return pd.DataFrame(rows)


def plot_greeks_grid(greeks_df, outfile, suptitle=None):
    """4 rows (delta, gamma, theta, vega) x N maturities. One line per model."""
    maturities = sorted(greeks_df["T"].unique())
    greek_names = ["delta", "gamma", "theta", "vega"]
    fig, axes = plt.subplots(4, len(maturities),
                              figsize=(5 * len(maturities), 14), sharex="col")

    for col, T in enumerate(maturities):
        sub_T = greeks_df[greeks_df["T"] == T].sort_values("moneyness")
        for row, gname in enumerate(greek_names):
            ax = axes[row, col] if len(maturities) > 1 else axes[row]
            for name in sub_T["model"].unique():
                s = sub_T[sub_T["model"] == name]
                ax.plot(s["moneyness"], s[gname], "-o", ms=3, color=COLORS[name], label=name)
            ax.axvline(1.0, color="gray", lw=0.8, ls=":", alpha=0.6)
            ax.axhline(0.0, color="gray", lw=0.6, alpha=0.4)
            ax.grid(alpha=0.3)
            if row == 0:
                ax.set_title(f"{int(round(T * 365))}d to expiry")
            if col == 0:
                ax.set_ylabel(gname.capitalize())
            if row == 3:
                ax.set_xlabel("Moneyness (K/S0)")

    handles, labels = axes[0, 0].get_legend_handles_labels() if len(maturities) > 1 \
        else axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.02))
    fig.suptitle(suptitle or "Greeks by Moneyness: Black-Scholes vs Heston vs Bates",
                 fontsize=13, y=1.05)
    fig.tight_layout()
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    plt.close(fig)
