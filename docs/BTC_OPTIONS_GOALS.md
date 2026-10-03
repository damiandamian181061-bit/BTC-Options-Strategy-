# BTC options trading system: agreed brief and validation goals

**Current implementation:** paper first, BTC USDC linear options, uv and Bun; 0.25% per trade and 1% aggregate risk, one spread, 1% daily halt, 10% drawdown halt, and 10,000 simulated USDC. Earlier small-live delivery and sizing figures below are planning history. See [implemented operations](OPERATIONS.md).

Prepared 2 October 2026 from the user's Goals Plan. Venue: Deribit. Initial strategy: fully protected bull put and bear call credit verticals. Quantitative models: Black-Scholes/forward Black benchmark, constrained market surface, Heston, Bates, and physical volatility/tail forecasts. Final optional model layer: Jev from TypeSafe AI. Delivery target: small live positions after explicit release gates.

This document preserves the user's objectives while tightening contract, probability, and testing definitions. The companion architecture plan provides the implementation details. Targets are proposed evaluation criteria; they do not guarantee future results.

## Mission

Pursue repeatable positive expectancy after costs while prioritizing capital preservation and controlled account exposure. Favor modest, liquid, fully protected credit spreads whose loss distributions and native payoff conventions are understood. Avoid scaling on the strength of win rate alone. Repeated small gains must compensate for infrequent larger losses, execution costs, and account risks.

## Goals and their acceptance evidence

| Goal | Requirement | Evidence required before promotion |
|---|---|---|
| G1: Protect capital | Every structure has a proven loss bound in a specified currency, including completed and permitted partial-fill states; deterministic portfolio limits apply | Contract payoff tests, native/reporting-currency ledger, stress/margin checks, and tested protected-leg execution |
| G2: Repeatable returns | Positive net expectancy across multiple time-separated regimes, with uncertainty reported | Walk-forward portfolio replay and production-quote forward paper results, including all costs and risk-matched controls |
| G3: Real edge | Implied/forecast variance gap supports the hypothesis, but the actual vertical must have conservative positive net expectancy and acceptable tails | Horizon-matched variance/tail forecasts and candidate-specific physical P&L distribution |
| G4: Useful quantitative models | Black reference and market surface work reliably; Heston/Bates enter the decision path only if they improve their assigned role out of sample | Independent numerical checks, held-out/lagged surface results, parameter health, and hedging/scenario evidence |
| G5: Measurable AI value | Jev makes a final bounded contextual judgment; retain it in the trading path only if it beats a rules-only contextual baseline at comparable risk | Frozen questions/model versions, semantic labels, matched portfolio experiments, and forward observations |
| G6: Transparent operation | Every order and decision is reconstructible; failures block new entries; independent recovery and risk reduction remain available | Immutable records, reconciliation, failure drills, alerting, and a tested kill switch |

## Required corrections to the original brief

### 1. Defined risk must name the currency

For an intact linear USDC credit vertical, maximum terminal option loss is quantity times (strike width minus actual credit), plus costs. This formula does not describe every inverse BTC spread or account-level drawdown.

For an inverse bull put spread, if settlement BTC/USD is below both strikes, the BTC liability is quantity times strike width divided by settlement BTC/USD. The native BTC liability grows as BTC/USD falls. The USD intrinsic payout is bounded, but a universal BTC loss cap cannot be claimed. BTC collateral also changes value. This follows mathematically from Deribit's inverse payoff convention. [Inverse option specifications](https://support.deribit.com/hc/en-us/articles/31424939096093-Inverse-Options)

Before funding, choose the reporting/risk currency, account margin mode, and permitted instrument family. If the requirement is a strict BTC-denominated loss cap for every trade, exclude inverse bull put spreads or select a different eligible product/structure. Implement inverse handling even if the first live universe uses linear contracts.

For every selected structure, include partial-leg exposure, fees, collateral translation, and venue margin treatment. Closing the protective long leg before its short leg can create an unprotected short position; paired closing or short-first sequential closing is required.

### 2. Positive variance premium and high win rate are hypotheses

ATM IV squared minus forecast RV is a proxy, not necessarily a model-free variance risk premium. Align horizon and units, and evaluate each vertical's protective-wing cost and loss distribution. A low-delta short strike does not by itself establish a 75–85% physical probability of profit.

Black-model delta and risk-neutral exercise probability are distinct quantities; neither is a validated physical win rate. Heston/Bates option-calibrated probabilities also contain risk premiums. Assess physical probabilities using historical/forecast scenarios and calibration against realized outcomes.

For example, an 85% win rate with 100-unit wins and 900-unit losses has negative expected P&L of 50 units per trade before costs. Rank on net expectancy and tail risk, with win rate as a diagnostic.

### 3. Black-Scholes comparisons must be fair

Keep the flat-volatility baseline for reproducible research. Compare production surface models against a competitive Black pricing/surface benchmark using the same quote coverage, forwards, units, bid/ask uncertainty, and held-out procedure. Beating one flat sigma across the whole chain does not prove better trading decisions.

Calibrating Heston or Bates to a current option snapshot is not physical forecasting. Price-fit improvements, hedge performance, and stress usefulness are separate tests. Reproduce exchange IV/Greek conventions explicitly; surface Greeks and Heston/Bates parameter sensitivities need not match.

### 4. Jev's confidence is a judgment statistic

Use atomic contextual questions, then deterministic code maps answers to TRADE or SKIP for entries. CLOSE is evaluated separately for existing positions. Low confidence, missing evidence, or a failed response means SKIP for new entries. A validated rules-only fallback must be separately configured before an outage; do not silently activate it.

Choice/Score confidence represents answer-distribution concentration, rather than trade-profit probability; Noul has no separate confidence field. [TypeSafe confidence documentation](https://docs.typesafe.ai/confidence)

Jev can suggest a close, but required risk exits, order cancellation, and reconciliation do not wait for AI. Explanation uses recorded input facts and deterministic reason codes, since Jev returns typed judgments rather than explanatory prose. Pin the provider model where available and freeze questions, criteria, and application policies.

### 5. Failure policy and drawdown limits need precise meaning

Failures mean no new discretionary risk. They do not mean freezing all operations while open positions or unknown orders remain. Continue reconciliation, cancellation, and predefined protected exposure reduction when venue access and liquidity permit.

A drawdown threshold triggers a deterministic halt and exit/reduction policy. Gaps, collateral moves, venue failures, and fill constraints can take realized drawdown beyond the trigger. Portfolio stress budgets and reserved risk must therefore support the drawdown objective; per-trade caps alone do not protect against correlated losses.

### 6. Testnet tests mechanics; production quotes test economics

Run a production-market-data paper simulator for the proposed 4–8-week forward period, extended when too few independent opportunities or relevant regimes occur. Use Deribit testnet in parallel to test authenticated order lifecycle, expiry/settlement, disconnects, restarts, and reconciliation.

Deribit explicitly says testnet liquidity and market activity do not accurately reflect production. Testnet P&L or fills cannot establish live profitability. [Deribit Testnet documentation](https://support.deribit.com/hc/en-us/articles/28685393662365-Deribit-Testnet)

## Component deliverables

- **Data:** clean timestamped option bid/ask/depth, index, expiry futures, funding, DVOL, instrument metadata, and private order/fill/account events. Preserve raw history from day one and distinguish source time from information availability.
- **Market state:** per-expiry forward curve and constrained arbitrage-consistent surface, with fresh-input and valid-domain checks. Normalize index conversion and native contract units centrally.
- **Pricing:** independently verified forward Black reference; validated Heston/Bates integrations; quote-uncertainty-aware calibration and explicit health diagnostics. Failed model health blocks any strategy relying on that model.
- **Forecasts:** persistence/EWMA benchmarks, HAR-RV and GARCH-t candidates, physical tail scenarios, and a separate optional trend/return challenger. Match forecast horizons to trade/exit policy.
- **Strategy:** bull put and bear call credit spreads ranked by net expectancy, conditional losses, liquidity, credit/width, and uncertainty. Both protective and short legs share expiry and documented quantities.
- **Risk:** currency-specific payoff bounds; temporary-leg budgets; per-trade, portfolio, delta, vega, concentration, margin, daily-loss and drawdown controls. Event windows and operational/model failures block entries deterministically.
- **Jev:** compact packet, narrow semantic questions, full responses/probabilities/confidence, pinned/logged versions, bounded latency, state-expiry checks, predefined abstention/fallback, and a contextual rules-only comparison.
- **Execution:** limit orders with explicit maximum crossing/price/depth policy; combo execution where feasible; long protection filled first when entering sequentially, short closed first when exiting sequentially. No market-order dependence, duplicate submissions, or unprotected rescue legs.
- **Operations:** immutable ledger, continuous account reconciliation, recoverable order state machine, monitoring, alerts, restart/failure playbooks, and withdrawal-disabled trading credentials.

## Targets to freeze before final testing and live deployment

| Metric | Proposed criterion | Qualification |
|---|---|---|
| Per-trade risk | User ceiling 1–2% of strategy equity; select one exact ceiling before deployment | Start materially smaller if minimum size permits; cap completed and partial-fill states in an explicit currency |
| Aggregate risk | Configure joint stress-loss, gross exposure, expiry concentration, delta/vega, margin and cash-buffer limits | Correlation and collateral moves can dominate individual spread limits |
| Drawdown | User proposal 10–15%; choose one exact trigger and portfolio response | Target/trigger, not guaranteed realized maximum; start with a smaller daily risk budget |
| Win rate | Explore the user's 75–85% range | Observed physical statistic with uncertainty; not inferred from 10–20 delta or used alone for promotion |
| Profit factor | User target above 1.3 after all costs in historical and forward paper results | Include uncertainty and regime splits; a small sample can make this unstable |
| Expectancy | Positive after option fees, spread/depth costs, funding where relevant, delivery and exit costs | Require robustness to conservative cost, tail and fill assumptions |
| Fill quality | Set bounds for implementation shortfall versus decision-time executable quotes | Also log mid deviation, but mid is not an available fill; isolate spread, delay and adverse selection |
| Model quality | Useful out-of-sample improvement over the competitive baseline for its assigned role | Current-chain fit alone cannot pass a forecast or trading gate |
| Jev value | Improved specified risk-adjusted result over rules-only contextual filters at matched exposure | Costs, skipped opportunity, version drift and historical contamination included |
| Operational safety | Zero unresolved order/position/accounting mismatches at release; recovery drills pass | Incidents may occur and must be handled, logged and reconciled; zero unhandled incidents is the operational objective |

## Release gates

| Gate | Evidence needed |
|---|---|
| Contract/data foundation | Currency-specific payoff bounds proven, metadata and times normalized, raw recording reliable, native/reporting ledger reconciles |
| Numerical/model release | Black reference validated; surface arbitrage/domain checks pass; Heston/Bates fail safely and justify their assigned role |
| Rules-only strategy | Profitable net expectancy over multiple chronological regimes with stress/tail/cost robustness and no leakage |
| Execution release | Protected-leg/combo behavior, partial fills, rejected orders, ambiguous timeouts, disconnects, expiry and restart recovery verified |
| Jev promotion | Frozen semantic policy improves the predefined risk-adjusted objective versus contextual rules-only filters; otherwise remove it from trading decisions |
| Forward paper release | Initially 4–8 weeks on production quotes, sufficient opportunities, realistic fills/costs, no unresolved failures; testnet mechanics validated separately |
| Small live release | Account/risk budgets configured, minimum trade fits all budgets, one validated strategy/universe, supervised monitoring and tested kill switch |
| Scaling | Actual execution and economic behavior remain within predefined tolerances over sufficient evidence; change one dimension at a time |

Failed gates require a documented diagnosis, repair, and fresh validation appropriate to the change. Repeated retuning on the same final test period invalidates its untouched status. Jev failure does not prevent an independently validated rules-only live release; failure of the quantitative/risk/execution gates does.

## Milestones

1. Foundation: contract/risk currency decision, recorder, validation, ledger, Black pricing/Greeks.
2. Surface and structural models: constrained surface, forwards, numerical/Greek checks, Heston then Bates.
3. Forecasts, credit strategy and risk: physical variance/tail scenarios, spread generator, sizing and exits.
4. Rigorous replay: walk-forward, bid/ask/depth execution, fees, margin, settlement and stress cases.
5. Jev overlay: packet/questions, deterministic action mapping, fallbacks, semantic labels and matched comparisons.
6. Forward paper and testnet: production-quote economics plus exchange-mechanics/recovery checks.
7. Small live deployment: tight budgets, protected execution, continuous reconciliation, scaling only after evidence.

## Non-goals and unresolved launch inputs

Non-goals: high-frequency trading, naked/unprotected shorts, AI execution privileges or limit overrides, and a promised profit figure. The architecture also avoids optimizing win rate at the expense of net expectancy.

Resolve before live configuration: strategy capital, risk/reporting currency, settlement family, Deribit account/margin mode, exact per-trade and aggregate budgets, selected drawdown trigger, eligible expiry/strike widths, data-source coverage/cost, and operating/monitoring responsibility. These inputs are not needed to finish this plan, but are required for an executable live policy.
