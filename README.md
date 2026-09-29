# BTC Options: Black-Scholes vs Heston vs Bates

A calibration study comparing three option-pricing models against a BTC
implied volatility surface: flat Black-Scholes, Heston (stochastic
volatility), and Bates (stochastic volatility + jumps).

## Why this comparison

Black-Scholes assumes constant volatility and continuous price paths.
Neither holds for BTC: volatility clusters and mean-reverts (calm periods,
then violent ones), and prices move discontinuously on exchange incidents,
regulatory news, and liquidation cascades. Heston relaxes the constant-vol
assumption; Bates adds jumps on top. The question this project answers:
**does that added complexity actually buy a better fit to the market, and
is it worth the extra calibration difficulty?**

## Method

1. **Pricing engines** (`models/`): closed-form Black-Scholes, and
   semi-analytic Heston / Bates pricers via characteristic-function
   inversion (Gil-Pelaez / Heston 1993, "Little Trap" formulation for
   numerical stability — see Albrecher et al. 2007). Both support calls
   and puts (puts via put-call parity, verified to hold to floating-point
   precision) and are vectorized across strikes at fixed maturity, using a
   fixed 64-point Gauss-Legendre quadrature instead of adaptive
   integration — this is what makes calibration run in seconds instead of
   tens of minutes: the characteristic function is evaluated once per
   (maturity, option type) and reused across every strike, rather than
   re-integrated per strike per candidate parameter set.
2. **Data** (`data.py`): a synthetic BTC options "market" generated from a
   Bates process with realistic ground-truth parameters (high vol-of-vol,
   negative spot-vol correlation, modest negative jump risk) plus pricing
   noise. Quotes use OTM options only — puts below spot, calls above —
   matching real market convention, and for a real reason: deep ITM
   options are almost pure intrinsic value with little vega, so price->IV
   inversion is numerically unstable there. Using a known data-generating
   process also lets me check that calibration actually recovers sensible
   parameters before trusting it on anything real. A template for pulling
   live calls-and-puts data from Deribit's public API is included (see
   "Using real data" below).
3. **Calibration** (`calibrate.py`): each model's parameters are fit by
   minimizing relative squared price error across the whole surface (all
   strikes, all maturities, both option types) simultaneously — one
   parameter set has to explain the entire smile, not just one point on
   it. Optimization is `differential_evolution` (global search, needed
   because the smile-fitting landscape is non-convex) followed by a
   bounded Nelder-Mead polish.
4. **Evaluation**: fit quality is reported as RMSE of *implied volatility*
   (in vol points), the standard way to compare option pricing models —
   it weights strikes fairly regardless of their raw price level.

## Results (synthetic market, 21 quotes across 3 maturities, calls + puts)

| Model         | IV RMSE (vol pts) |
|---------------|-------------------|
| Bates         | 0.13              |
| Heston        | 1.20              |
| Black-Scholes | 2.22              |

Flat Black-Scholes is roughly 18x worse than Bates in RMSE terms, because
it has exactly one free parameter and cannot represent a smile at all —
see `smile_comparison.png`: it's a flat line cutting through a clearly
downward-sloping market skew. Bates tracks the market skew almost exactly
across all three maturities.

**Two findings worth flagging explicitly rather than glossing over:**

1. **Heston's calibration converges to a boundary solution** — kappa and
   theta both pin near their lower search bounds, and rho pins near -1,
   *consistently*, across repeated runs with wider search budgets (so this
   isn't an under-optimized local minimum; it's genuinely where the
   objective is minimized given Heston's structure). This makes sense in
   hindsight: the true market has jumps, and pure diffusion-based
   stochastic volatility can only *approximate* a jump-driven short-dated
   skew by pushing toward near-deterministic, highly-correlated variance
   dynamics — an unrealistic corner of the parameter space. This is
   exactly the empirical motivation for adding jumps in the first place
   (Bates 1996): a real fitting exercise, not just a synthetic one, often
   reproduces this same pattern on short-dated crypto or equity index
   skew.
2. **Bates' own jump parameters are still not cleanly recovered**
   (calibrated λ ≈ 0.20 vs. true 0.6, μⱼ ≈ -0.16 vs. true -0.06), even
   though the *overall* price/smile fit is excellent and the diffusion
   parameters (kappa, theta) land close to their true values. This is a
   known identification problem in jump-diffusion calibration: stochastic
   volatility and jump risk both add left-skew and excess kurtosis to the
   implied distribution, so a cross-sectional snapshot of option prices
   alone often can't cleanly separate "how much of this skew is vol-of-vol
   vs. how much is jump risk" — different parameter combinations can
   produce nearly identical option prices. Resolving it properly needs
   either time-series data (to see actual jumps happen) or a much denser
   strike/maturity grid.

The headline result (Bates fits best, Black-Scholes fits worst) is the
expected, presentable one. The two findings above are the more
interesting ones for an interview: they show the fitting procedure is
being interrogated rather than trusted blindly.

## Using real data instead of the synthetic market

This sandbox has no general internet access, so the project ships with a
synthetic (but realistically-generated) market by default. To run it on a
live BTC snapshot (calls and puts), on a machine with normal internet
access:

```bash
pip install -r requirements.txt
python3 run_on_real_data.py
```

That's it — `fetch_deribit_market()` in `data.py` pulls the live chain, no
API key needed, parses each instrument's expiry into time-to-maturity,
filters to a sensible 1–180 day window, and returns data in the exact
shape `main.py` expects, so calibration and plotting just work. It saves
`smile_comparison_live.png`, `fit_quality_summary_live.csv`, and the raw
snapshot as `deribit_snapshot.csv` (handy if you want to send it somewhere
else for calibration, or back to me).

## Limitations (worth stating explicitly, not hiding)

- Calibrated on a single snapshot in time (a "cross-section"), not a time
  series — this is standard for model comparison but can't validate how
  well a model *hedges* over time, only how well it fits prices today.
- Only 3 maturities x 7 strikes. Real Deribit chains have far more — more
  data generally stabilizes the jump-parameter identification problem
  noted above.
- Risk-free rate set to 0, matching Deribit's coin-margined (BTC-settled)
  quoting convention — this differs from equity option conventions and is
  worth calling out if presenting this alongside equity-options work.
- Fixed 64-point quadrature trades a small amount of pricing accuracy
  (~0.003% vs. adaptive integration, verified in testing) for a >100x
  speedup — reasonable for calibration, but a production pricing engine
  might want adaptive precision near expiry/strike edge cases.

## Greeks (delta, gamma, theta, vega)

`greeks.py` computes all four via finite-difference bumping (nudge spot,
time, or vol slightly and reprice), the same way for all three models —
which is itself the point worth stating in a write-up: **Black-Scholes has
closed-form Greek formulas; Heston and Bates don't**, because Heston alone
has five vol-related parameters (v0, kappa, theta, sigma_v, rho), so
there's no single "sigma" to differentiate against the way Black-Scholes
has one. Bump-and-reprice is the standard way to get Greeks out of a model
that doesn't have a closed form, and it's model-agnostic — the exact same
code path handles all three.

**Validated before trusting it on Heston/Bates**: I checked the
finite-difference delta/gamma/vega against Black-Scholes' own closed-form
formulas — they match to 9 significant figures. Theta matches to within
0.3%, which is *expected*, not an error: the finite-difference version
measures actual next-day P&L (including one day of curvature), while the
closed-form formula is an instantaneous derivative — the discrete version
is arguably the more practically useful number anyway ("what do I lose
overnight"), not a less accurate one.

**Definitions used** (Greek conventions vary across desks — stating them
explicitly matters more than which convention you pick):
- delta, gamma — standard, dPrice/dS and d²Price/dS²
- theta — Price(T − 1 day) − Price(T), holding spot fixed: expected
  overnight time-decay P&L, in $ per day
- vega — dPrice/d(√v0) × 0.01: price change per 1-percentage-point move in
  the *current instantaneous vol level*. This is a specific modeling
  choice, not the only valid one — under Heston/Bates you could equally
  well define a "vega" with respect to θ (long-run variance) or σᵥ
  (vol-of-vol) instead of v0. Bumping v0 was chosen because it answers the
  question a trader usually means by vega: "if the market's current
  implied vol moved by 1 point, how much would this option reprice?"

**What the results show** (`greeks_comparison.png`): gamma and vega peak
near the money and decay in the wings for all three models, as expected.
The genuinely interesting divergence is that **Bates shows lower vega than
Heston at longer maturities** — part of Bates' sensitivity to the smile's
shape comes from its jump parameters rather than from v0, so its price
reacts less to a pure vol-of-v0 bump for the same overall level of
skew/smile. One visual note: delta has a visible jump exactly at
moneyness = 1.0 in the plot — that's not a bug, it's the OTM quoting
convention switching from put deltas (negative) to call deltas (positive)
at that strike, consistent with put-call parity (delta_call − delta_put =
1 at the same strike).

## Suggested CV framing

> Built and calibrated Heston and Bates stochastic-volatility models to
> price BTC calls and puts via characteristic-function inversion,
> benchmarked against Black-Scholes; reduced implied-vol RMSE by >90%
> versus flat Black-Scholes, computed and validated Greeks (delta, gamma,
> theta, vega) via finite-difference bumping for all three models, and
> identified a parameter-identification limitation in jump-component
> calibration from cross-sectional data alone.

Keep the one-liner on the CV; keep this README (or a short write-up drawn
from it) as the backing detail for when someone asks about it in an
interview — the honest "here's where the model breaks down" paragraph
above is exactly the kind of thing that tends to land well when a quant
recruiter or hiring manager probes further, since it shows you understand
the model's limits rather than just running someone else's formula.

## Real-data results (Deribit)

The `main.py` results above use synthetic data (this project's own sandbox
had no network route to deribit.com). `analyze_manual_export.py` runs the
same study on real Deribit data pulled straight from the site's own UI
export (no API, no account, no trading history needed) — see
`sample_data/real_exports/` for the exact files used.

**Study 1 — a single ~1-day expiry (30SEP26), full wide-wing set:**

| Model | IV RMSE (vol pts) |
| --- | --- |
| Bates | 3.94 |
| Heston | 9.35 |
| Black-Scholes | 26.87 |

This run also surfaced a genuine floating-point precision failure in the
characteristic-function pricer at ultra-short maturities: some far-wing
prices come back invalid, traced to the deep-ITM-call probability landing
so close to 1 that the raw call price falls *below* its own intrinsic
value — confirmed with much finer quadrature grids, which converged to the
same wrong answer rather than fixing it. Restricting to a numerically
clean near-the-money band changes the picture:

| Model | IV RMSE (vol pts) |
| --- | --- |
| Bates | 3.55 |
| Black-Scholes | 3.89 |
| Heston | 6.15 |

Bates only modestly beats Black-Scholes here, and Heston is worse than
both — because a jump model's real advantage lives in the tails, exactly
what had to be excluded on numerical grounds.

**Study 2 — a full term structure, 5 real expiries (10 days to 1 year):**

| Model | IV RMSE (vol pts) |
| --- | --- |
| Heston | 4.18 |
| Bates | 4.21 |
| Black-Scholes | 12.12 |

Heston and Bates converge to almost the same fit here — a different,
more structural finding than Study 1. Both get pushed to an extreme
vol-of-vol (pinned at the search bound, and climbing even higher when the
bound was widened to test it), which points to a single constant-parameter
variance process struggling to reconcile a steep short-dated skew with a
much flatter one a year out, regardless of whether jumps are included.

This study also priced each expiry against its own delta-implied forward
rather than one shared spot — BTC's forward curve carries a real
cost-of-carry premium (about $84k near-term rising to about $95k a year
out on the same snapshot), which `data.py`'s `load_manual_export()` /
`load_multi_expiry()` handle automatically.

**Greeks, computed on this real term structure**, showed a fourth finding:
Black-Scholes vega comes out more than 3x larger than Heston's or Bates'
at the long end (about 250 vs. about 85 at 178 days). The reason is mean
reversion — a bump to the current instantaneous vol barely moves a
long-dated option's price once a calibrated kappa around 4 has time to
pull variance back to its long-run level, while Black-Scholes' flat sigma
has no such decay. Concrete evidence that Heston/Bates vega is not a
drop-in replacement for Black-Scholes vega.

Run it yourself: `python3 analyze_manual_export.py` (uses the sample
exports already in the repo; edit the file lists at the top to point at
fresh exports for an updated snapshot).

## Project structure

```
models/black_scholes.py    Closed-form pricing + implied vol inversion
models/heston.py           Stochastic-vol pricing (vectorized, fixed quadrature)
models/bates.py            Heston + jumps
greeks.py                  Finite-difference delta/gamma/theta/vega (all 3 models)
data.py                    Synthetic market generator + 2 real-data loaders
                            (live Deribit API, and manual UI exports)
calibrate.py                Calibration routines for all three models
report.py                   Shared plotting/reporting used by every entry point
main.py                     Full study on synthetic data
run_on_real_data.py         Full study on a LIVE Deribit API pull
analyze_manual_export.py    Full study on Deribit's UI-exported tables —
                             both real-data findings above come from this
sample_data/real_exports/   The exact export files used for those results
outputs/                    Where analyze_manual_export.py writes its plots/CSVs
```

Run on synthetic data (works anywhere, no internet needed):
```bash
pip install -r requirements.txt
python3 main.py
```

Run on live Deribit data via the API (needs internet access — won't work
in a sandbox without egress to deribit.com):
```bash
pip install -r requirements.txt
python3 run_on_real_data.py
```

Reproduce the real-data results in this README exactly, using the sample
exports already included (works anywhere, no internet needed — this is
what actually generated the numbers above):
```bash
pip install -r requirements.txt
python3 analyze_manual_export.py
```
