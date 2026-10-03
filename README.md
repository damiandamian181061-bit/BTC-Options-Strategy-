# BTC Options Platform

Python 3.12 managed by **uv**, a TypeScript dashboard managed by **Bun**, and **paper trading by default**. The platform records production Deribit BTC USDC option markets, evaluates protected credit verticals, reserves risk before ordering, and keeps an auditable session ledger. Real execution uses a separate, explicitly armed adapter.

This is research and evaluation software. A working pricer or backtest does not establish profitable trading. Heston, Bates and rough Bergomi are research challengers pending temporal validation. Jev starts advisory; switch on *Require Jev approval* to make it a gate.

## Host on GitHub + first run

1. Create a new GitHub repository and push this project:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

2. In the cloned project folder, copy `.env.example` to `.env` and set `TYPESAFE_API_KEY` if you want Jev-enabled paper mode.
3. Start the stack with Docker (`.\start.ps1` on Windows, `sh start.sh` on Linux).

## Live hosting on Render (GitHub auto-deploy)

This repository includes a Render Blueprint service config at `/render.yaml` and a deploy workflow at `/.github/workflows/deploy-render.yml`.

1. Push this repository to GitHub (branch `main`).
2. In Render, create a **Web Service** from this repo using the Blueprint (`render.yaml`).
3. In Render service settings, set `TYPESAFE_API_KEY` (optional; leave empty for rules-only paper mode).
4. In Render service settings, create a **Deploy Hook** and copy its URL.
5. In GitHub repository settings, add secret `RENDER_DEPLOY_HOOK_URL` with that URL.
6. Push to `main` (or run the **Deploy to Render** workflow manually) to trigger deployment.

After deploy, open your Render URL and check `/api/health`.
The hosted container runs collector + paper worker + dashboard in one service and persists runtime state on the `/data` disk.

## Container quick start

Put your TypeSafe key in `.env` (copy `.env.example`), then, with Docker Desktop running, use one command from the project folder:

```powershell
.\start.ps1
```

The launcher builds Python with **uv** and the frontend with **Bun**, starts the recorder, paper trader and dashboard, waits for health checks, and opens **http://127.0.0.1:8765**. No host Python/Bun installation or Deribit credentials are needed. Only `TYPESAFE_API_KEY` is passed from `.env` to the paper worker; after editing it, run `docker compose up -d` to apply. If another app uses that port, run `.\start.ps1 -Port 8787`.

On Linux: `sh start.sh`. The portable command is `docker compose up --build -d`.

The dashboard is a mobile-first, brokerage-style app with three tabs:

- **Portfolio**: balance with a scrubbable equity chart (1D/1W/1M/All), portfolio Greeks (IV, delta, gamma, theta, vega) and open positions. Tap a position for its legs, live Greeks and P&L, then *slide to close* (short leg first).
- **Trade**: *Auto-trade* and *Require Jev approval* switches, and the latest ideas with the credit you receive and Jev's verdict. Tap an idea to review max loss, costs, edge, exit rules and Greeks, then *slide to execute*. The worker re-prices it on live quotes, asks Jev when approval is required, runs the independent risk check and buys protection first; a banner reports the outcome.
- **Settings**: live connection status (Deribit, worker, Jev key from `.env`, forecast history), session limits, *slide to halt & flatten*, and an optional history CSV import.

The status pill is green only for fresh **production** Deribit quotes; stale or synthetic data is labeled in amber or red.

Container restarts resume the same simulated account, pending orders and persistent halts. `docker compose stop` stops services; `docker compose up -d` resumes them. The container workspace uses persistent named volumes, separate from the local `runtime/` directory. The default stack is paper-only. Live remains an explicitly armed CLI option.

Optional port and initial balance settings are also in `.env`. See [container operation](docs/CONTAINERS.md) for logs, backups, fresh sessions and key handling.

## Local development quick start

From the project root:

```powershell
cd frontend
bun install --frozen-lockfile
bun test
bun run build
cd ..
uv sync --locked
uv run btc-options doctor
uv run btc-options validate
uv run btc-options paper --demo --seconds 30
uv run btc-options dashboard
```

Open **http://127.0.0.1:8765**. The CLI dashboard is read-only by default; add `--controls` to allow paper trade, close, halt and Jev-gate requests from the browser (the running `paper` worker consumes them). Exchange sessions never accept dashboard requests. For UI development, run the Python dashboard and `bun run dev` in `frontend`.

Demo observations are synthetic and labeled. The same conservative strategy may reject all demo spreads. Tests exercise fills, partial protection, exits and recovery separately. Synthetic observations never qualify as performance evidence.

## Production-data paper evaluation

One command records live Deribit data over WebSocket and runs the simulated portfolio (add `--no-record` if a separate `collect` process already records into the same runtime folder). Check connectivity and your Jev key first:

```powershell
uv run btc-options doctor --network
uv run btc-options paper --config configs/paper.toml --seconds 86400
uv run btc-options dashboard --controls
```

No Deribit trading credentials are needed. Default simulated capital is **10,000 USDC**. Save the printed session ID. Resume with `--resume SESSION_ID` and identical configuration after a restart. A new invocation starts an isolated portfolio. Keep one service writer per session. Simulated sessions cannot become real sessions.

Physical forecasts require at least **60 completed days** of sufficiently covered intraday history. The worker backfills 90 days of real 5-minute BTC-PERPETUAL closes from Deribit's public chart API on start and extends them every evaluation (the perpetual tracks the index within basis points), so entries are not blocked for two months. Its own recorded BTC USDC index takes precedence where available, and imported history joins automatically. A degenerate GARCH fit (e.g. near-infinite-variance tails on a short sample) is flagged unhealthy and excluded from the forecast ensemble. An explicit updating `timestamp,price` CSV can also be supplied with `--prices history.csv`. An old static CSV alone is insufficient for forward trading: the latest price must be within 15 minutes.

```powershell
uv run btc-options import-history intraday.csv --kind prices
uv run btc-options export-history runtime/export
uv run btc-options backtest runtime/export/snapshots.jsonl runtime/export/underlying.csv
uv run btc-options report SESSION_ID --output runtime/paper-report.json
```

Performance replay accepts only chronological, normalized production/historical executable quotes. Manual exports lacking reliable timestamps or depth remain valuation research. Empty trade histories and missing losses produce undefined metrics, not invented win rates or profit factors.

## Quantitative stack

| Component | Role |
|---|---|
| Forward Black | Explicit forwards, discounts and premium units; prices, IV inversion, analytical Greeks; independent QuantLib checks |
| Greeks | Delta (BTC), gamma, theta (USDC/day), vega (USDC/vol point) and IV for every leg, spread, candidate and the portfolio; risk delta/vega budgets use the same engine |
| Extended SSVI (eSSVI) | Per-expiry skew with Hendriks–Martini calendar and Gatheral–Jacquier butterfly conditions enforced by construction; quotes weighted by bid/ask width in vol; sub-day expiries excluded; per-slice residual errors |
| Corrected Heston / Bates | Compensated, measure-specific jump transform; bid/ask-scaled calibration, adaptive verification, holdouts and health diagnostics |
| Physical forecasts | Equal-weight ensemble of direct multi-horizon log-HAR, HAR-X (DVOL implied variance + downside semivariance), EWMA and GJR-GARCH-t on a one-year backfill; last-day persistence is a benchmark only |
| Rough Bergomi | Seeded hybrid Volterra simulation, antithetics and disjoint-pilot control variate; sampling and grid-refinement uncertainty |
| Spread strategy | 14–45 day bull put/bear call verticals; .10–.20 short absolute delta; daily filtered-historical-simulation paths with a DVOL-estimated mean-reverting IV level, valued to the actual exit (take-profit, mid-value stop, time or pre-expiry exit) including exit spread and fees |

Implied valuation is risk-neutral. Physical scenarios are separate; option delta is not a real-world win probability. Newer models do not automatically become trading defaults.

```powershell
uv run btc-options research runtime/latest.json --model heston
uv run btc-options research runtime/latest.json --model bates
uv run btc-options model-walk-forward runtime/export/snapshots.jsonl --model bates
uv run btc-options benchmark --paths 10000 --steps 64
```

## Risk, execution and recovery

Defaults: **0.25% per trade**, **1% aggregate reserved loss**, **one spread**, **1% daily loss halt**, **10% drawdown halt**. Reservations include pending/partial positions and funded protection. Hold at most 24 hours (and exit 48 hours before expiry), take profit after capturing 50% of credit, and begin reduction when the spread's mid value reaches 2x initial credit. The candidate evaluator simulates these same exits, so a policy that cannot cover its round-trip costs produces no candidates. Triggers cannot guarantee fill prices or realized drawdown.

Paper IOC limits consume subsequent executable depth after latency, including fees and partial cancellations. A touched quote does not guarantee a fill. Entry buys protection first; exit closes shorts first. Combo execution remains disabled pending independent validation; sequential protected execution is implemented.

```powershell
uv run btc-options halt SESSION_ID
uv run btc-options recover SESSION_ID --action reconcile
uv run btc-options recover SESSION_ID --action cancel
uv run btc-options recover SESSION_ID --action reduce --seconds 60
uv run btc-options settle-paper SESSION_ID runtime/last-pre-expiry-snapshot.json
```

Halts survive restarts. Keep the service running for reduction or use recovery. Exchange recovery needs `--arm` and matching credentials but forbids new exposure and does not depend on a release manifest. Settlement needs confirmed delivery history and retained expiry metadata. Exchange account discrepancies halt for audit.

## Jev, testnet and live

The TypeSafe adapter pins **jev-1.13.0** and rejects mutable aliases. Set `TYPESAFE_API_KEY` in `.env`; it is the only entry read from that file (re-read every loop, so edits apply live locally). An exported environment variable takes precedence. Without a key, rules-only paper remains usable. With approval off, judgments are recorded but do not change the portfolio; with approval on (or `jev_mode = "filter"`), missing, invalid, uncertain or obsolete responses map to SKIP. CLOSE is a separate advisory for existing positions. Mandatory risk management runs first. Jev cannot size, submit or authorize orders.

Testnet is for exchange mechanics; its liquidity does not support performance claims. Copy the policy to separate exchange configuration, set the mode, and explicitly configure `strategy_capital`. Use a dedicated subaccount with withdrawal permissions disabled. `.env.example` documents variable names; Deribit credentials in `.env` are never auto-loaded and must be exported explicitly in the service shell.

```powershell
uv run btc-options testnet --config configs/testnet.toml --arm
uv run btc-options release-check evidence-paths.json --config configs/live.toml
uv run btc-options live --config configs/live.toml --arm --release runtime/release.json
```

Live requires passing evidence, explicit arming and production credentials. Release manifests bind configuration, implementation and evidence hashes, and expire after seven days. No passing live release is included. Begin with 4–8 weeks of forward paper evaluation, extending for low trade counts or insufficient regimes. See [the operations runbook](docs/OPERATIONS.md) for assumptions and gate formats.

## Storage, tests and preserved studies

Parquet holds versioned timestamped market envelopes and normalized snapshots; DuckDB analyzes them. Validated WebSocket books supply paper snapshots when available; public REST is a fallback and partial or stale chains cannot qualify absent sufficient valid data. SQLite WAL holds mode-isolated sessions, cash, positions, reservations, orders, idempotent fills, settlements and decision events. Runtime data and secrets are ignored by Git. The dashboard serves from loopback, opens the ledger read-only and only queues paper requests for the worker. Build the Bun assets before packaging Python; wheels bundle the dashboard.

The original standalone Black-Scholes/Heston/Bates study scripts were retired; their fast Heston/Bates engine lives on as `btc_options.structural` and the research commands above. They remain in git history before this cleanup. `sample_data/real_exports` keeps the raw Deribit UI exports.

Windows/Linux Python tests and wheel builds, plus Bun tests and builds, are configured in `.github/workflows/check.yml`. Local validation does not imply remote CI has run.

## Primary references checked 2 October 2026

- [Deribit USDC options](https://support.deribit.com/hc/en-us/articles/31424932728093-Linear-USDC-Options), [data collection](https://docs.deribit.com/articles/options-data-collection-best-practices), [testnet](https://support.deribit.com/hc/en-us/articles/28685393662365-Deribit-Testnet), [fees](https://support.deribit.com/hc/en-us/articles/25944746248989-Fees).
- [SSVI constraints](https://arxiv.org/abs/1204.0646), [QuantLib Bates reference](https://github.com/lballabio/QuantLib/blob/master/ql/pricingengines/vanilla/batesengine.cpp).
- [TypeSafe models](https://docs.typesafe.ai/models), [API](https://docs.typesafe.ai/api), [Jev limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

Model recency alone is not superiority. This project provides reproducible evaluation and makes no claim to a universally best BTC trading model or guaranteed returns.
