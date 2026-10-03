"""Local service state and atomic setup files; no order authorization here."""

import json
import os
from datetime import datetime
from pathlib import Path

from .domain import identifier, utc_now


def atomic_text(path: Path, value: str, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{identifier()}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def heartbeat(runtime, role, state="running", **details):
    atomic_text(
        Path(runtime) / "services" / f"{role}.json",
        json.dumps(
            {
                "role": role,
                "state": state,
                "timestamp": utc_now().isoformat(),
                **details,
            }
        ),
    )


def service_status(runtime, role):
    record = read_json(Path(runtime) / "services" / f"{role}.json")
    if not isinstance(record, dict):
        return {"role": role, "state": "not_started", "responsive": False}
    try:
        age = (utc_now() - datetime.fromisoformat(record["timestamp"])).total_seconds()
        return {
            **record,
            "age_seconds": age,
            "responsive": 0 <= age <= 30 and record["state"] in ("running", "starting"),
        }
    except (ValueError, TypeError, KeyError):
        return {"role": role, "state": "invalid_status", "responsive": False}


class PaperLease:
    """OS-held lock, released on crashes, prevents a second managed paper writer."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.file.tell() == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ValueError("another managed paper service already owns this volume") from exc
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt

            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


# Dashboard controls and commands are files the single paper writer consumes;
# the dashboard never writes the ledger or talks to an execution adapter.


def read_controls(runtime, jev_mode="shadow"):
    """Without saved controls, keep the configured behavior: auto-trade, gate only in filter."""
    saved = read_json(Path(runtime) / "services" / "controls.json") or {}
    return {
        "auto_trade": bool(saved.get("auto_trade", True)),
        "jev_gate": bool(saved.get("jev_gate", jev_mode == "filter")),
    }


def write_controls(runtime, auto_trade, jev_gate):
    value = {"auto_trade": bool(auto_trade), "jev_gate": bool(jev_gate)}
    atomic_text(Path(runtime) / "services" / "controls.json", json.dumps(value))
    return value


def queue_command(runtime, payload):
    command = {**payload, "id": identifier(), "queued_at": utc_now().isoformat()}
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%f")
    atomic_text(Path(runtime) / "commands" / f"{stamp}-{command['id']}.json", json.dumps(command))
    return command


def take_commands(runtime):
    """Oldest first; each command is removed once read so it executes at most once."""
    commands = []
    for path in sorted((Path(runtime) / "commands").glob("*.json")):
        command = read_json(path)
        path.unlink(missing_ok=True)
        if isinstance(command, dict):
            commands.append(command)
    return commands
