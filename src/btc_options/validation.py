"""Release gates inspect fixed evidence files; missing evidence is a no-go."""

import hashlib
import json
from pathlib import Path

from .domain import utc_now


def evidence_file(path):
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}


def verify_evidence(evidence, settings):
    required = {"numerical", "backtest", "forward_paper", "testnet"}
    if settings.jev_mode == "filter":
        required.add("jev_comparison")
    if not required.issubset(evidence):
        raise ValueError(f"missing release evidence: {sorted(required - set(evidence))}")
    reports = {}
    for kind in required:
        item = evidence[kind]
        raw = Path(item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("evidence changed after review")
        reports[kind] = json.loads(raw)
    n = reports["numerical"]
    if n.get("failures") != 0 or n.get("tests", 0) < 20:
        raise ValueError("numerical and operational tests are incomplete")
    back = reports["backtest"]
    if not back.get("historical_executable_quotes") or len(back.get("regimes", [])) < 3:
        raise ValueError("executable backtest and three validated regimes required")
    b = back.get("portfolios", {}).get("deterministic_context", {})
    p = reports["forward_paper"]
    for report in (b, p):
        if (
            report.get("closed_spreads", 0) < 30
            or report.get("net_realized", 0) <= 0
            or (report.get("profit_factor") or 0) < 1.3
        ):
            raise ValueError("insufficient positive, cost-adjusted trading evidence")
        if (
            report.get("max_drawdown", 1) >= settings.risk.drawdown
            or report.get("management_failures", 1) != 0
        ):
            raise ValueError("risk or reconciliation evidence failed")
    if not p.get("production_only") or p.get("days", 0) < 28 or p.get("mode") != "paper":
        raise ValueError("at least 28 days of production-quote forward paper evidence required")
    if not reports["testnet"].get("lifecycle_validated"):
        raise ValueError(
            "testnet partial fills, timeout, cancellation, recovery and reconciliation not validated"
        )
    if "jev_comparison" in reports:
        j = reports["jev_comparison"]
        if not (
            j.get("matched_exposure") and j.get("out_of_sample") and j.get("objective_improved")
        ):
            raise ValueError("Jev filter has not earned promotion")
    return reports


def release(settings, files, target):
    from .live import policy_hash, implementation_hash

    evidence = {kind: evidence_file(path) for kind, path in files.items()}
    verify_evidence(evidence, settings)
    report = dict(
        schema_version=1,
        created_ms=int(utc_now().timestamp() * 1000),
        policy_hash=policy_hash(settings),
        implementation_hash=implementation_hash(),
        evidence=evidence,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
