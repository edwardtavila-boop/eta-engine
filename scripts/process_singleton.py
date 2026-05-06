"""Small cross-platform process singleton guard for scheduled ETA jobs."""

from __future__ import annotations

import atexit
import json
import os
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class ProcessSingletonLock:
    """Atomic lock-file guard that tolerates stale locks after crashes."""

    path: Path
    name: str

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": self.name,
            "pid": os.getpid(),
            "started_at_utc": _utc_now(),
        }

        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                current = _read_json(self.path)
                pid = int(current.get("pid") or 0)
                if _pid_is_running(pid):
                    return False
                with suppress(FileNotFoundError):
                    self.path.unlink()
                continue

            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            self.acquired = True
            atexit.register(self.release)
            return True

    def release(self) -> None:
        if not self.acquired:
            return
        current = _read_json(self.path)
        if int(current.get("pid") or 0) == os.getpid():
            with suppress(FileNotFoundError):
                self.path.unlink()
        self.acquired = False


def write_singleton_skip_report(*, state_root: Path, lock_path: Path, name: str) -> Path:
    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    report_path = state_root / f"{name}_singleton_skip.json"
    report = {
        "ts": _utc_now(),
        "status": "skipped",
        "reason": "singleton_lock_active",
        "name": name,
        "lock_path": str(lock_path),
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report_path
