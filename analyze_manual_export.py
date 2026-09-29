"""
Runs the real-data study from Deribit UI exports (no API access needed —
this is what this project actually used, since its sandbox has no network
route to deribit.com, but you can always just visit the site).

Two studies, matching what this project found:

1. SINGLE ULTRA-SHORT EXPIRY (30SEP26, ~1 day at time of snapshot): the
   full wide-wing strike range exposes a real floating-point precision
   failure in the characteristic-function pricer at extreme short T (see
   models/heston.py, models/bates.py docstrings) — some far-wing prices
   come back invalid. This study reports BOTH the full set and a
   numerically clean near-the-money band, and shows why they disagree.

2. MULTI-MATURITY TERM STRUCTURE (5 expiries, 10 days to 1 year): combines
   several expiries into one surface, each priced against its own
   delta-implied forward (BTC's cost-of-carry means longer-dated forwards
   sit well above spot). At this scale, Heston and Bates converge to
   almost the same fit — the dominant strain is a structural vol-of-vol
   limitation (both models get pushed to their vol-of-vol search bound),
   not jump risk specifically.

Edit EXPIRIES below to point at different export files / dates to rerun
with fresh data.
"""
from datetime import datetime, timezone
import pandas as pd

from data import load_manual_export, load_multi_expiry
from calibrate import calibrate_flat_bs, calibrate_heston, calibrate_bates
from report import fit_quality_summary, plot_smile, compute_greeks_table, plot_greeks_grid

SNAPSHOT_TIME = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)

# ---- Study 1: single ultra-short expiry ----
SHORT_EXPIRY_FILE = "sample_data/real_exports/30SEP26.xlsx"
SHORT_EXPIRY_DATE = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)

# ---- Study 2: multi-maturity term structure ----
TERM_STRUCTURE = [
    ("sample_data/real_exports/9OCT26.tsv",  datetime(2026, 10, 9,  8, 0, tzinfo=timezone.utc)),
    ("sample_data/real_exports/30OCT26.tsv", datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)),
    ("sample_data/real_exports/25DEC26.tsv", datetime(2026, 12, 25, 8, 0, tzinfo=timezone.utc)),
    ("sample_data/real_exports/26MAR27.tsv", datetime(2027, 3, 26,  8, 0, tzinfo=timezone.utc)),
    ("sample_data/real_exports/24SEP27.tsv", datetime(2027, 9, 24,  8, 0, tzinfo=timezone.utc)),
]


def run_calibration_and_report(df, S0, r, label, smile_out, greeks_out=None):
    results = {}
    results["Black-Scholes"], _ = calibrate_flat_bs(df, S0, r)
    results["Heston"], _ = calibrate_heston(df, S0, r)
    results["Bates"], _ = calibrate_bates(df, S0, r)

    summary = fit_quality_summary(df, S0, r, results)
    print(f"\n{label}")
    print(summary.to_string(index=False))
    for name, params in results.items():
        print(f"  {name}: " + ", ".join(f"{k}={v:.4f}" for k, v in params.items()))

    plot_smile(df, S0, r, results, smile_out, market_label="Market (Deribit, live)",
               suptitle=label)
    print(f"  saved {smile_out}")

    if greeks_out:
        greeks_df = compute_greeks_table(df, S0, r, results)
        plot_greeks_grid(greeks_df, greeks_out, suptitle=f"Greeks — {label}")
        print(f"  saved {greeks_out}")
        return results, summary, greeks_df
    return results, summary, None


def study_1_short_expiry():
    df, S0, r = load_manual_export(SHORT_EXPIRY_FILE, SHORT_EXPIRY_DATE, now_utc=SNAPSHOT_TIME)
    print(f"\n{'='*70}\nSTUDY 1: {SHORT_EXPIRY_FILE} (~1 day) — full wide-wing set\n{'='*70}")
    print(f"{len(df)} OTM quotes, delta-implied forward S0=${S0:,.0f}")

    # Full set: expect some far-wing quotes to fail IV inversion downstream
    # due to the precision issue — that failure IS the finding, not a bug
    # to hide. See README "Known limitations".
    run_calibration_and_report(df, S0, r, "BTC 30SEP26 (~1 day) — full wide-wing set",
                                "outputs/smile_30sep26_full.png")

    # Numerically clean near-the-money band — the trustworthy comparison
    clean = df[(df.moneyness >= 0.964) & (df.moneyness <= 1.036)].reset_index(drop=True)
    run_calibration_and_report(clean, S0, r, "BTC 30SEP26 (~1 day) — clean near-ATM band",
                                "outputs/smile_30sep26_clean.png")


def study_2_term_structure():
    df, r = load_multi_expiry(TERM_STRUCTURE, now_utc=SNAPSHOT_TIME)
    S0_fallback = df["S0"].iloc[0]  # only used if a group somehow lacks its own S0
    print(f"\n{'='*70}\nSTUDY 2: term structure, {len(TERM_STRUCTURE)} expiries "
          f"({df['T'].min()*365:.0f}d to {df['T'].max()*365:.0f}d)\n{'='*70}")
    print(f"{len(df)} OTM quotes total")

    results, summary, greeks_df = run_calibration_and_report(
        df, S0_fallback, r, "BTC implied vol term structure — 5 real Deribit expiries",
        "outputs/smile_term_structure.png", "outputs/greeks_term_structure.png")

    summary.to_csv("outputs/fit_quality_term_structure.csv", index=False)
    greeks_df.to_csv("outputs/greeks_term_structure.csv", index=False)
    df.to_csv("outputs/market_data_term_structure.csv", index=False)


if __name__ == "__main__":
    import os
    os.makedirs("outputs", exist_ok=True)
    study_1_short_expiry()
    study_2_term_structure()
    print("\nDone. See outputs/ for plots and CSVs.")
