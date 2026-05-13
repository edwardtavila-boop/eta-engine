"""Shared services for ETA dashboard API routes."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def ensure_dir_writable(path: Path) -> bool:
    """Return True when a directory is writable (best effort)."""
    try:
        probe = path / ".dashboard_health_probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def read_jsonl_tail(path: Path, limit: int) -> list[str]:
    """Read up to ``limit`` non-empty JSONL lines from end of file."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        out.append(raw)
        if len(out) >= limit:
            break
    return out


def run_background_task(
    task: str,
    state_dir: Path,
    log_dir: Path,
    timeout_s: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run deploy.scripts.run_task in a subprocess."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.scripts.run_task",
            task,
            "--state-dir",
            str(state_dir),
            "--log-dir",
            str(log_dir),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
