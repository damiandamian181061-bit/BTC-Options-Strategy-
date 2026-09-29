"""
Runs the full study on the synthetic (Bates-generated) market:
  1. Build market data (calls + puts, OTM convention)
  2. Calibrate flat Black-Scholes, Heston, and Bates to the whole surface
  3. Report implied-vol fit quality for each model
  4. Plot the implied vol smile: market vs. each model, at each maturity
  5. Compute and plot delta/gamma/theta/vega for all three models
"""
import time
from data import generate_synthetic_market
from calibrate import calibrate_flat_bs, calibrate_heston, calibrate_bates
from report import fit_quality_summary, plot_smile, compute_greeks_table, plot_greeks_grid


def main():
    print("=" * 70)
    print("BTC OPTIONS: Black-Scholes vs Heston vs Bates")
    print("=" * 70)

    df, S0, r, true_params = generate_synthetic_market()
    print(f"\nSynthetic market: {len(df)} quotes, S0=${S0:,.0f}, r={r}")
    print("(Ground truth generated from a Bates process — see data.py for params)\n")

    results = {}

    t0 = time.time()
    print("Calibrating flat Black-Scholes ...")
    results["Black-Scholes"], _ = calibrate_flat_bs(df, S0, r)
    print(f"  done in {time.time()-t0:.1f}s")

    t0 = time.time()
    print("Calibrating Heston (global + local refine) ...")
    results["Heston"], _ = calibrate_heston(df, S0, r)
    print(f"  done in {time.time()-t0:.1f}s")

    t0 = time.time()
    print("Calibrating Bates (global + local refine) ...")
    results["Bates"], _ = calibrate_bates(df, S0, r)
    print(f"  done in {time.time()-t0:.1f}s")

    summary = fit_quality_summary(df, S0, r, results)
    print("\n" + "-" * 70)
    print("FIT QUALITY (lower = better; RMSE of implied vol, in vol points)")
    print("-" * 70)
    print(summary.to_string(index=False))

    print("\n" + "-" * 70)
    print("CALIBRATED PARAMETERS")
    print("-" * 70)
    print("\n(ground truth used to generate the synthetic market, for reference)")
    for k, v in true_params.items():
        print(f"  {k:10s} = {v:.4f}")
    for name, params in results.items():
        print(f"\n{name}:")
        for k, v in params.items():
            print(f"  {k:10s} = {v:.4f}")

    plot_smile(df, S0, r, results, "smile_comparison.png",
               market_label="Market (synthetic)",
               suptitle="BTC Implied Volatility Smile: Market vs. Calibrated Models")
    print("\nSaved plot: smile_comparison.png")

    print("\nComputing Greeks (delta, gamma, theta, vega) via finite-difference "
          "bumping for all three models ...")
    greeks_df = compute_greeks_table(df, S0, r, results)
    plot_greeks_grid(greeks_df, "greeks_comparison.png",
                      suptitle="Greeks by Moneyness: Black-Scholes vs Heston vs Bates")
    print("Saved plot: greeks_comparison.png")

    summary.to_csv("fit_quality_summary.csv", index=False)
    df.to_csv("synthetic_market_data.csv", index=False)
    greeks_df.to_csv("greeks_table.csv", index=False)
    print("Saved: fit_quality_summary.csv, synthetic_market_data.csv, greeks_table.csv")


if __name__ == "__main__":
    main()
