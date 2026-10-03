"""uv-managed, explicit operating commands. The default trading command is paper."""

import asyncio
import json
import platform
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich import print

from .config import Settings
from .domain import MarketSnapshot, utc_now
from .jev import api_key
from .storage import Ledger, MarketStore

app = typer.Typer(
    no_args_is_help=True, help="BTC USDC credit spreads: paper first; explicit exchange arming."
)
Config = Annotated[Path | None, typer.Option("--config", "-c", help="Typed TOML configuration")]


def settings_for(config=None, mode=None):
    settings = Settings.load(config)
    if mode:
        settings.mode = mode
    return settings


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


@app.command()
def doctor(config: Config = None, network: bool = False):
    """Check policy and environment; --network probes live Deribit and verifies the Jev key."""
    cfg = settings_for(config)
    print(
        {
            "python": platform.python_version(),
            "mode": cfg.mode,
            "paper_balance_usdc": cfg.paper_balance,
            "jev_model": cfg.jev_model,
            "jev_key_found": bool(api_key()),
            "runtime": str(cfg.runtime_dir.resolve()),
        }
    )
    if network:
        from .jev import verify_key
        from .market import PublicDeribit

        async def check():
            client = PublicDeribit()
            try:
                snapshot = await client.snapshot(cfg.min_expiry_days, cfg.max_expiry_days)
                ages = [
                    (snapshot.asof - q.exchange_time).total_seconds()
                    for q in snapshot.quotes.values()
                ]
                index = next(iter(snapshot.quotes.values())).index if snapshot.quotes else None
                print(
                    {
                        "deribit": snapshot.source,
                        "btc_index": index,
                        "quoted_options": f"{len(snapshot.quotes)} of {len(snapshot.instruments)}",
                        "oldest_quote_seconds": round(max(ages), 1) if ages else None,
                    }
                )
            finally:
                await client.close()
            if api_key():
                try:
                    await verify_key(api_key())
                    print({"jev": "key verified with " + cfg.jev_model})
                except (ValueError, httpx.HTTPError) as exc:
                    print({"jev": f"verification failed: {exc}"})

        asyncio.run(check())


@app.command()
def collect(config: Config = None, seconds: int = 3600, snapshots_only: bool = False):
    """Record production market data to Parquet without credentials."""
    from .market import collect as record

    if seconds <= 0:
        raise typer.BadParameter("seconds must be positive")
    asyncio.run(record(settings_for(config), seconds, not snapshots_only))


@app.command("import-history")
def import_history(
    path: Path, kind: Annotated[str, typer.Option()] = "snapshots", config: Config = None
):
    """Validate and import normalized JSONL snapshots or timestamp,price intraday CSV."""
    cfg = settings_for(config)
    if kind == "prices":
        from .service import parse_prices

        prices = parse_prices(path)
        target = cfg.runtime_dir / "underlying.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        prices.rename("price").rename_axis("timestamp").to_csv(target)
        print({"observations": len(prices), "path": str(target.resolve())})
    elif kind == "snapshots":
        from .replay import read_snapshots

        snapshots = read_snapshots(path)
        store = MarketStore(cfg.runtime_dir / "market")
        for snapshot in snapshots:
            store.snapshot(snapshot)
        print({"snapshots": len(snapshots), "performance_claim": "none"})
    else:
        raise typer.BadParameter("kind must be snapshots or prices")


@app.command("export-history")
def export_history(output: Path, config: Config = None):
    """Export recorded snapshots for causal replay and observed index prices for forecasts."""
    store = MarketStore(settings_for(config).runtime_dir / "market")
    frame = store.query(
        "SELECT received_at,channel,payload FROM market WHERE channel='snapshot' OR channel LIKE 'deribit_price_index.%' ORDER BY received_at"
    )
    if isinstance(frame, list):
        raise typer.BadParameter("no recorded market data")
    output.mkdir(parents=True, exist_ok=True)
    # Demonstrations and testnet traffic must never contaminate performance replay.
    snapshot_rows = frame[frame["channel"] == "snapshot"].copy()
    parsed = [MarketSnapshot.model_validate_json(value) for value in snapshot_rows["payload"]]
    snapshot_rows = snapshot_rows.loc[[s.source in ("production", "historical") for s in parsed]]
    snapshot_rows = snapshot_rows.drop_duplicates("payload")
    (output / "snapshots.jsonl").write_text(
        "\n".join(snapshot_rows["payload"]) + "\n", encoding="utf-8"
    )
    indices = []
    for row in frame[frame["channel"] == "deribit_price_index.btc_usdc"].itertuples():
        data = json.loads(row.payload)
        if "price" in data:
            indices.append({"timestamp": row.received_at, "price": data["price"]})
    import pandas as pd

    pd.DataFrame(indices, columns=["timestamp", "price"]).drop_duplicates("timestamp").to_csv(
        output / "underlying.csv", index=False
    )
    print({"snapshots": len(snapshot_rows), "index_observations": len(indices)})


@app.command()
def research(snapshot: Path, model: str = "heston", output: Path = Path("runtime/research.json")):
    """Calibrate Heston/Bates; holdout fit does not authorize promotion."""
    from .calibration import calibrate

    data = MarketSnapshot.model_validate_json(snapshot.read_text(encoding="utf-8"))
    result = calibrate(data, model)
    write_json(
        output,
        {
            "parameters": result.parameters,
            "result": result.result.model_dump(mode="json"),
            "promotion": "research only: temporal walk-forward validation still required",
        },
    )
    print(result.result.model_dump(mode="json"))


@app.command()
def benchmark(
    paths: int = 10000,
    steps: int = 64,
    seed: int = 7,
    output: Path = Path("runtime/rough-benchmark.json"),
):
    """Reproducible rough Bergomi comparison and grid-refinement uncertainty."""
    from .pricing import black_price
    from .rough import rough_bergomi

    a = rough_bergomi(100, 100, 0.1, paths=paths, steps=steps, seed=seed)
    b = rough_bergomi(100, 100, 0.1, paths=paths, steps=steps * 2, seed=seed)
    report = {
        "coarse": asdict(a),
        "fine": asdict(b),
        "flat_black_reference": black_price(100, 100, 0.1, 0.7),
        "grid_difference": b.price - a.price,
        "status": "research challenger; sampling error excludes grid bias",
    }
    write_json(output, report)
    print(report)


@app.command("model-walk-forward")
def model_walk_forward(
    snapshots: Path,
    model: str = "heston",
    stride: int = 1,
    output: Path = Path("runtime/model-walk-forward.json"),
):
    """Fit only earlier snapshots and score subsequent quotes; never auto-promote."""
    from .replay import read_snapshots
    from .walkforward import compare

    result = compare(read_snapshots(snapshots), model, stride)
    write_json(output, result)
    print(result)


@app.command()
def backtest(
    snapshots: Path,
    prices: Path,
    output: Path = Path("runtime/backtest"),
    decisions: Path | None = None,
    config: Config = None,
):
    """Compare quantitative, contextual and recorded-Jev portfolios on executable quotes."""
    from .replay import backtest as replay

    result = asyncio.run(replay(settings_for(config), snapshots, prices, output, decisions))
    print(result)


def trading(mode, config, seconds, prices, demo, resume, arm=False, release=None, record=True):
    from .service import run

    if seconds <= 0:
        raise typer.BadParameter("seconds must be positive")
    cfg = settings_for(config, mode)
    try:
        sid = asyncio.run(run(cfg, seconds, prices, demo, resume, arm, release, record=record))
        print(
            {
                "session": sid,
                "mode": mode,
                "ledger": str((cfg.runtime_dir / "ledger.sqlite").resolve()),
            }
        )
    except (ValueError, OSError) as exc:
        print(f"[red]Stopped: {exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def paper(
    config: Config = None,
    seconds: int = 300,
    prices: Path | None = None,
    demo: bool = False,
    resume: str | None = None,
    record: Annotated[
        bool, typer.Option(help="Run the live WebSocket recorder in this process")
    ] = True,
):
    """Simulated execution against live Deribit production quotes. --demo is synthetic."""
    trading("paper", config, seconds, prices, demo, resume, record=record)


@app.command()
def testnet(
    config: Config = None,
    arm: bool = False,
    seconds: int = 300,
    prices: Path | None = None,
    resume: str | None = None,
):
    """Exchange mechanics only: separate testnet credentials and explicit --arm."""
    trading("testnet", config, seconds, prices, False, resume, arm)


@app.command()
def live(
    config: Config = None,
    arm: bool = False,
    release: Path | None = None,
    seconds: int = 300,
    prices: Path | None = None,
    resume: str | None = None,
):
    """Real execution: requires --arm, capital, credentials and passing release evidence."""
    trading("live", config, seconds, prices, False, resume, arm, release)


@app.command()
def halt(session: str, config: Config = None, reason: str = "operator kill switch"):
    """Persist an entry halt; the active service cancels entries and reduces exposure."""
    ledger = Ledger(settings_for(config).runtime_dir / "ledger.sqlite")
    try:
        ledger.session(session)
        ledger.halt(session, reason)
        print("Halt persisted. Keep the service running for cancellation and reduction.")
    finally:
        ledger.close()


@app.command()
def report(session: str, config: Config = None, output: Path = Path("runtime/paper-report.json")):
    """Cost-adjusted accounting and coverage, without fabricating performance evidence."""
    from .replay import session_report

    ledger = Ledger(settings_for(config).runtime_dir / "ledger.sqlite")
    try:
        result = session_report(ledger, session)
        write_json(output, result)
        print(result)
    finally:
        ledger.close()


@app.command()
def recover(
    session: str,
    action: str = "reconcile",
    config: Config = None,
    arm: bool = False,
    seconds: int = 30,
):
    """Independent reconcile/cancel/reduce controls; recovery forbids new exposure."""
    from .execution import PaperAdapter, SpreadExecutor
    from .live import LiveAdapter
    from .market import PublicDeribit
    from .domain import OrderState

    cfg = settings_for(config)
    ledger = Ledger(cfg.runtime_dir / "ledger.sqlite")
    stored = ledger.session(session)
    cfg = Settings.model_validate_json(stored["config"])
    if action not in ("reconcile", "cancel", "reduce"):
        raise typer.BadParameter("action must be reconcile, cancel, or reduce")

    async def recovery():
        adapter = (
            PaperAdapter(ledger, session, cfg)
            if cfg.mode == "paper"
            else LiveAdapter(ledger, session, cfg, armed=arm, recovery_only=True)
        )
        client = PublicDeribit(testnet=cfg.mode == "testnet")
        executor = SpreadExecutor(adapter, ledger, session)
        try:
            if action in ("cancel", "reduce"):
                ledger.halt(session, "operator recovery: " + action)
                for order in ledger.orders(session):
                    if order.state not in (
                        OrderState.FILLED,
                        OrderState.CANCELLED,
                        OrderState.REJECTED,
                    ):
                        await adapter.cancel(order)
            deadline = asyncio.get_running_loop().time() + seconds
            while True:
                snapshot = await client.snapshot(0, cfg.max_expiry_days + 1)
                if isinstance(adapter, LiveAdapter):
                    adapter.ready = (
                        True  # Recovery submissions still require reduce-only holdings checks.
                    )
                await adapter.advance(snapshot)
                await adapter.reconcile(snapshot)
                if action != "reduce":
                    break
                await executor.reduce()
                await executor.manage(snapshot, cfg)
                if not ledger.positions(session) or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(1)
            print({"positions": ledger.positions(session), "halt": ledger.session(session)["halt"]})
        finally:
            await client.close()
            if isinstance(adapter, LiveAdapter):
                await adapter.close()

    try:
        asyncio.run(recovery())
    finally:
        ledger.close()


@app.command("settle-paper")
def settle_paper(session: str, snapshot: Path, config: Config = None):
    """Settle expired simulated options from confirmed public delivery prices."""
    from .domain import Settlement
    from .execution import PaperAdapter
    from .market import PublicDeribit

    cfg = settings_for(config)
    ledger = Ledger(cfg.runtime_dir / "ledger.sqlite")
    cfg = Settings.model_validate_json(ledger.session(session)["config"])
    if cfg.mode != "paper":
        raise typer.BadParameter("paper settlement cannot change exchange holdings")
    historical = MarketSnapshot.model_validate_json(snapshot.read_text(encoding="utf-8"))

    async def settle():
        client = PublicDeribit()
        try:
            data = await client.call(
                "public/get_delivery_prices", index_name="btc_usdc", count=1000
            )
            prices = {d["date"]: d["delivery_price"] for d in data["data"]}
            confirmed = utc_now()
            current = historical.model_copy(update={"asof": confirmed, "source": "production"})
            adapter = PaperAdapter(ledger, session, cfg)
            for name in ledger.positions(session):
                i = current.instruments[name]
                if i.expiry <= confirmed and i.expiry.date().isoformat() in prices:
                    record = Settlement(
                        instrument=name,
                        expiry=i.expiry,
                        confirmed_at=confirmed,
                        delivery_price=prices[i.expiry.date().isoformat()],
                        source="production",
                    )
                    adapter.settle(record, current)
            for row in ledger.spreads(session):
                c = json.loads(row["candidate"])
                positions = ledger.positions(session)
                if not positions.get(c["long"]) and not positions.get(c["short"]):
                    ledger.spread_status(row["id"], "CLOSED")
            print({"remaining_positions": ledger.positions(session)})
        finally:
            await client.close()

    try:
        asyncio.run(settle())
    finally:
        ledger.close()


@app.command("release-check")
def release_check(evidence: Path, config: Config, output: Path = Path("runtime/release.json")):
    """Evidence JSON maps numerical/backtest/forward_paper/testnet to report paths."""
    from .validation import release

    if config is None:
        raise typer.BadParameter("explicit live configuration required")
    cfg = settings_for(config, "live")
    files = {k: Path(v) for k, v in json.loads(evidence.read_text(encoding="utf-8")).items()}
    try:
        release(cfg, files, output)
    except ValueError as exc:
        print(f"[red]NO-GO: {exc}[/red]")
        raise typer.Exit(1) from exc
    print(f"Release manifest saved: {output.resolve()}")


@app.command()
def dashboard(
    config: Config = None,
    port: int = 8765,
    controls: Annotated[
        bool, typer.Option(help="Allow paper trade/close/halt requests from the dashboard")
    ] = False,
):
    """Serve the built frontend and API on 127.0.0.1 (read-only unless --controls)."""
    import uvicorn
    from .api import create_app

    cfg = settings_for(config)
    front = Path(__file__).resolve().parent / "frontend_dist"
    # Bun assets ship in wheels; source checkouts use their own build output.
    if not front.exists():
        front = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    print(f"Dashboard: http://127.0.0.1:{port} | build the UI with bun run build in frontend")
    app_ = create_app(cfg.runtime_dir, front, controls=controls)
    uvicorn.run(app_, host="127.0.0.1", port=port)


@app.command()
def validate(output: Path = Path("runtime/numerical-validation.json")):
    """Run the project's numeric and operational tests; record JUnit results."""
    import xml.etree.ElementTree as ET

    output.parent.mkdir(parents=True, exist_ok=True)
    junit = output.with_suffix(".xml")
    result = subprocess.run([sys.executable, "-m", "pytest", "--junitxml", str(junit)], check=False)
    root = ET.parse(junit).getroot()
    suites = list(root.iter("testsuite"))
    report = {
        "tests": sum(int(s.get("tests", "0")) for s in suites),
        "failures": sum(int(s.get("failures", "0")) + int(s.get("errors", "0")) for s in suites),
        "created": utc_now().isoformat(),
        "exit_code": result.returncode,
    }
    write_json(output, report)
    raise typer.Exit(result.returncode)


if __name__ == "__main__":
    app()
