# Managed paper workspace

Run Docker Desktop with Linux containers. `start.ps1` starts all three services and opens the local UI; `sh start.sh` is the Linux equivalent. The application image builds locked Bun assets and installs locked Python dependencies with uv. Processes run as an unprivileged user. Only the dashboard port is published, bound to localhost. No Docker socket or production trading credentials are mounted.

## Setup and daily operation

1. Launch with `.\start.ps1`, or `docker compose up --build -d`.
2. Open http://127.0.0.1:8765; **Settings** shows each connection.
3. The recorder connects to public Deribit production feeds. The paper worker starts when the recorder is healthy.
4. Put `TYPESAFE_API_KEY=...` in `.env` before launching. Compose passes only that variable to the paper worker (never the Deribit credentials, never the dashboard). After changing it, run `docker compose up -d` so the worker is recreated with the new value. `docker compose exec paper btc-options doctor --network` verifies Deribit connectivity and the key against the pinned model; normal TypeSafe usage/billing applies. The key is never returned by any route or recorded in status/history.
5. History is backfilled from Deribit automatically. Optionally import a trusted UTF-8 `timestamp,price` CSV, at most 16 MiB. Use intraday BTC index prices with timezone-aware timestamps and enough coverage. Import validation rejects duplicate timestamps, invalid prices and future data. Uploaded versions are retained; the normalized active file is replaced atomically. Imports never submit orders.
6. The paper worker validates completed-day coverage, daily continuity, freshness and forecasting convergence. The progress indicator does not override these checks. Sixty variance and daily-return observations are required; obtaining sixty daily returns generally needs more than sixty daily closes.

The dashboard has three tabs: **Portfolio** (balance, equity chart, portfolio Greeks, positions with slide to close), **Trade** (auto-trade and Jev-approval switches, ideas with Greeks and Jev verdicts, slide to execute) and **Settings**. Trade, close and halt requests are queued as files for the single paper worker, which re-prices entries on fresh quotes, applies the Jev gate and the independent risk check, and records every outcome. The dashboard never opens the ledger for writing and has no live-arming or Docker-control endpoints; exchange sessions reject dashboard requests. All writes require a matching local Origin and an allowed Host. The CLI `dashboard` is read-only unless started with `--controls`, and its setup actions stay disabled.

The `paper-data` volume stores market history, imports, the ledger, service status and the active session pointer. The key lives only in your `.env` and the paper container's environment; protect that file and access to Docker Desktop.

Container data is separate from the host development `runtime/` folder. Existing local simulated portfolios are retained in that folder. 

## Stop, resume and inspect

```powershell
docker compose ps
docker compose logs --tail 100 paper collector
docker compose stop
docker compose up -d
```

Stopping does not flatten simulated holdings. Restart resumes and reconciles pending intents; required management runs before new entries. Halts remain persistent. An OS-held volume lease prevents a second managed paper writer. Changing the initial balance after a session exists fails rather than rewriting its account; use a fresh session deliberately.

To start another isolated simulated account, stop the paper service first, then launch it with an explicit new-session command:

```powershell
docker compose stop paper
docker compose run --rm --no-deps paper paper --new-session
```

That command runs in the foreground; stop it before returning to the normal background service with `docker compose up -d paper`. Prior sessions remain in the ledger for inspection. Live/testnet execution is not selectable from this entrypoint; use the original CLI with credentials, arming and release checks.

For a port override, edit `BTC_UI_PORT` in `.env`. The PowerShell launcher also accepts `-Port 8787`.

## Export and backup

Use the existing export commands inside the running container:

```powershell
docker compose exec paper btc-options export-history /data/export
docker compose cp paper:/data/export ./paper-export
```

For a ledger backup, stop the paper service so no transaction is in progress, then copy the data directory from the container. Preserve SQLite WAL/SHM files together when present. The Jev key is not in `/data`.

```powershell
docker compose stop paper
docker compose cp paper:/data ./paper-backup
docker compose up -d paper
```

Keep backups outside source control. Ordinary `docker compose down` retains named volumes. Removing volumes destroys the associated paper history; it is not required for upgrades or restarts.

## Validation scope

Unit tests cover request origins and host checks, `.env` key loading (only the Jev key), failed verification, CSV validation, upload bounds, paper-only command routing, the Jev gate, session continuity and the writer lease. The image is built and services are health-checked independently. No successful Jev request is claimed without a real key; mocked API cases validate failure handling. Containerization does not establish strategy profitability or replace the forward-paper and live release gates.

Implementation follows the official [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/), [Bun Docker guide](https://bun.com/guides/ecosystem/docker), and [Compose startup guidance](https://docs.docker.com/compose/how-tos/startup-order/).
