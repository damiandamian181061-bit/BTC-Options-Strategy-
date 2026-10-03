# Operations and validation

The implemented policy is paper first, BTC USDC linear options, 10,000 simulated USDC, uv for Python and Bun for the frontend. It supersedes earlier small-live delivery targets in the design drafts.

## Session procedure

1. Install locked environments and build the frontend as described in README.
2. Run `uv run btc-options validate` to record JUnit and numerical/operational results.
3. Start collection and import trustworthy intraday index history if available. Otherwise build it. RV needs 90% coverage of 5-minute returns per completed day and continuous daily training observations. Future observations are excluded; stale underlying inputs block entries.
4. Start paper trading and save the session ID. Keep one writer per session and resume with identical configuration after restart. The observer runs independently.
5. Review exclusions, calibration health, depth, fees and reconciliations. Maintain a verified event calendar using `event_calendar` in TOML. The example is illustrative. A missing calendar leaves unknown events as an explicit risk.
6. Persist halts when needed. The active service cancels entries and manages reduction before Jev. Independent recovery commands work without forecasts or Jev. Resume only after reconciling records and exchange state.
7. Export snapshots/index data, replay portfolios, and compare earlier calibrations against later quotes. Evaluate 4–8 weeks of forward paper operation and extend for insufficient trades or regime coverage.

## Quantitative assumptions

SSVI shares rho/eta with `phi(theta)=eta/sqrt(theta)`, positive monotone ATM total variances and sufficient butterfly bounds. It refuses extrapolation beyond observed strike and expiry domains. Fit-residual bands are diagnostic, not calibrated statistical confidence intervals. Surface delta/gamma bump the full smile; parallel market-IV vega is per volatility point.

Position and candidate Greeks (`greeks.py`) use forward Black with the exchange mark IV (candidates use the SSVI volatility), zero-rate carry and USDC-per-BTC units: delta in BTC, gamma per USDC of forward move (the UI shows it per $1,000), theta in USDC per calendar day, vega in USDC per volatility point. Aggregated IV is |vega|-weighted. Mid IV is inverted independently from the executable bid/ask mid. Risk delta/vega budgets use the same engine.

The physical ensemble averages HAR, Student-t GARCH, EWMA and persistence variance. Historical standardized residual blocks retain empirical tails. Zero arithmetic drift is a benchmark assumption. Spread horizon valuation uses constant strike IV with parallel stress shifts of at least five volatility points; no IV collapse is assumed. Conservative expectation deducts sampling error and takes the weakest stress mean. This may reject every candidate. Joint IV/spot dynamics, forecast uncertainty and event tails need empirical validation before live release.

Heston/Bates fit forward-normalized call values (puts via parity), scale residuals by bid/ask spreads and regularize parameter changes. Fixed quadrature is the fast calibration engine; adaptive inversion verifies holdouts independently. Numerical violations fail rather than being hidden by clipping. Strike holdouts are not temporal out-of-sample evidence. `model-walk-forward` freezes earlier parameters and per-expiry flat-Black baselines before scoring later quotes. No result automatically changes the trading pricer.

Rough Bergomi is a seeded hybrid simulation challenger with antithetic paths and disjoint-pilot control variates. Sampling uncertainty excludes grid bias, which the benchmark reports through refinement. It has no live authorization role. New structural or Jev versions require fresh validation.

## Execution and accounting

IOC fills use subsequent quotes after latency, executable prices and displayed depth. Resting fills, hidden liquidity and touched-quote fills are not inferred. Depth is consumed once per observation. Commission defaults approximate a standard tier and premium cap; configure the actual fee tier. Fully funded maximum spread liability is a conservative paper margin proxy; testnet validates venue mechanics.

Contractual payoff is bounded by width. Fee/slippage reserves are estimates; extreme fees, USDC depeg, outages and execution losses are not guaranteed to fit them. Exit triggers are instructions to reduce exposure, not guaranteed loss caps. Confirmed delivery history is required for paper settlement; current marks are never substituted. Live expiry transitions, transfers or unmanaged positions require exchange audit, and discrepancies halt execution.

SQLite atomically books each idempotent fill and changes cash, position size and order state. Exchange intents are saved before transport. Ambiguous timeouts enter RECONCILING and are never resubmitted. Empty recent label lookup does not prove rejection: older orders require a historical trade/order audit. Recovery cannot increase exposure. Use a dedicated USDC subaccount with no unrelated holdings or orders.

Feeds preserve exchange/receive timestamps, metadata and unbroken book sequences. Gaps invalidate books and trigger resubscription. Periodic public snapshots reconcile the stream. Parquet writes are atomic batches; errors/gaps are recorded. A successful short smoke test does not establish long-running uptime.

## Release evidence

Pass `release-check` a JSON mapping actual report filenames:

```json
{
  "numerical": "runtime/numerical-validation.json",
  "backtest": "runtime/backtest/comparison.json",
  "forward_paper": "runtime/paper-report.json",
  "testnet": "runtime/testnet-mechanics.json"
}
```

The numerical report requires no failures and at least 20 tests. Backtests require executable quotes and three reviewed regime labels. Context and forward-paper reports each require at least 30 closed spreads, positive net realized PnL, profit factor >=1.3, acceptable drawdown and zero management failures. Forward paper must be production-only, in paper mode and at least 28 days. Undefined or unknown metrics are no-go.

The backtest `regimes` field starts empty deliberately; annotate only verified coverage with retained artifacts and rationale. The testnet report must independently establish `lifecycle_validated` from protection, partial fills, exits, cancellations, ambiguous transport, disconnect/restart recovery, margin and reconciliation evidence. Do not mark success merely to arm live. Reports are local review artifacts, not tamper-proof attestations.

For Jev filtering, add `jev_comparison` evidence establishing reviewed `matched_exposure`, `out_of_sample` and `objective_improved` fields. Predefine the objective, tail constraints and holdout events. Compare with deterministic context at matched configured risk and realized exposure. Recorded judgments can be supplied to replay with `--decisions`; today's model is not called retrospectively on historical events. Confidence concerns the judgment, not trading profit.

Release manifests bind policy, implementation and evidence hashes and expire after seven days. No passing release ships with the project. Actual testnet account mechanics and forward profitability remain unproven until the corresponding evaluation has run.

## Record contracts

Typed records carry schema_version=1. Option amounts are BTC underlying units; premiums are USDC per BTC. Execution requires USDC settlement and contract_size=1. Snapshot asof is aware and no earlier than any quote timestamp. Model records carry cutoff/version/measure/health. Candidate records bind snapshot, protected legs, costs, scenario assumptions and reserved risk. Risk assessments hold final authority. Jev records preserve packet hash, model, typed response, status and latency. Intent states include submitting, acknowledged, partial, filled, cancelled, rejected and reconciling. Session mode never changes.

Dashboard routes use a read-only SQLite connection on loopback. Managed containers (or `dashboard --controls`) add paper-only trade, close, halt and control requests that are queued for the paper worker, plus a history import; there are no key or live-arming routes. The TypeSafe key is read from `.env` (only that variable) or the environment and never reaches the browser. Refreshes do not start workers or submit orders. Build with Bun; Python serves built assets. Keep runtime data and secret values outside source control. See [container operation](CONTAINERS.md).
