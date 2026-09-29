"""
Run this on a machine with normal internet access (not in the sandbox this
project was built in). Requires: pip install -r requirements.txt

Fetches a live BTC options snapshot from Deribit (calls + puts, no API key
needed), calibrates Black-Scholes / Heston / Bates against it, and computes
+ plots delta/gamma/theta/vega for all three — the same study as main.py,
on real market data instead of the synthetic one.
"""
from data import fetch_deribit_market
from calibrate import calibrate_flat_bs, calibrate_heston, calibrate_bates
from report import fit_quality_summary, plot_smile, compute_greeks_table, plot_greeks_grid


def main():
    print("Fetching live BTC options snapshot from Deribit ...")
    df, S0, r = fetch_deribit_market()
    df.to_csv("deribit_snapshot.csv", index=False)
    print(f"Got {len(df)} quotes, S0=${S0:,.0f}. Saved raw snapshot to deribit_snapshot.csv")

    if len(df) < 8:
        print("WARNING: very few usable quotes — widen min_days/max_days in "
              "fetch_deribit_market() if this keeps happening.")

    results = {}
    print("Calibrating flat Black-Scholes ...")
    results["Black-Scholes"], _ = calibrate_flat_bs(df, S0, r)
    print("Calibrating Heston ...")
    results["Heston"], _ = calibrate_heston(df, S0, r)
    print("Calibrating Bates ...")
    results["Bates"], _ = calibrate_bates(df, S0, r)

    summary = fit_quality_summary(df, S0, r, results)
    print("\nFIT QUALITY (lower better, IV RMSE in vol points)")
    print(summary.to_string(index=False))
    for name, params in results.items():
        print(f"\n{name}:")
        for k, v in params.items():
            print(f"  {k:10s} = {v:.4f}")

    plot_smile(df, S0, r, results, "smile_comparison_live.png",
               market_label="Market (Deribit)",
               suptitle="BTC Implied Volatility Smile — LIVE Deribit data vs. Calibrated Models")

    print("\nComputing Greeks (delta, gamma, theta, vega) via finite-difference "
          "bumping for all three models ...")
    greeks_df = compute_greeks_table(df, S0, r, results)
    plot_greeks_grid(greeks_df, "greeks_comparison_live.png",
                      suptitle="Greeks by Moneyness — LIVE Deribit data")

    summary.to_csv("fit_quality_summary_live.csv", index=False)
    greeks_df.to_csv("greeks_table_live.csv", index=False)
    print("\nSaved: smile_comparison_live.png, greeks_comparison_live.png, "
          "fit_quality_summary_live.csv, greeks_table_live.csv, deribit_snapshot.csv")


if __name__ == "__main__":
    main()
