"""Safe alert-log heartbeat writer for DR/readiness checks.

This script proves the canonical alerts-log channel is writable without
starting bots, contacting brokers, or dispatching external notifications.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import ETA_RUNTIME_ALERTS_LOG_PATH  # noqa: E402


def _display_path(path: Path) -> str:
    """Return a workspace-relative path when possible."""
    try:
        return path.relative_to(ROOT.parent).as_posix()
    except ValueError:
        return str(path)


def build_smoke_record(*, source: str = "alerts_log_smoke") -> dict[str, Any]:
    """Build the minimal heartbeat row for the alerts JSONL stream."""
    return {
        "ts": datetime.now(UTC).isoformat(),
        "kind": "alerts_smoke",
        "source": source,
        "severity": "INFO",
        "status": "green",
        "dry_run": True,
        "broker_network": False,
        "transport": "none",
    }


def append_alerts_smoke(
    log_path: Path = ETA_RUNTIME_ALERTS_LOG_PATH, *, source: str = "alerts_log_smoke"
) -> dict[str, Any]:
    """Append one smoke row to ``log_path`` and return operator evidence."""
    record = build_smoke_record(source=source)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return {
        "path": _display_path(log_path),
        "bytes": log_path.stat().st_size,
        "record": record,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alerts_log_smoke")
    parser.add_argument(
        "--log-path",
        type=Path,
        default=ETA_RUNTIME_ALERTS_LOG_PATH,
        help=f"alerts JSONL path (default: {ETA_RUNTIME_ALERTS_LOG_PATH})",
    )
    parser.add_argument("--source", default="alerts_log_smoke")
    parser.add_argument("--json", action="store_true", help="emit machine-readable evidence")
    args = parser.parse_args(argv)

    evidence = append_alerts_smoke(args.log_path, source=args.source)
    if args.json:
        print(json.dumps(evidence, indent=2, sort_keys=True))
    else:
        print(f"[alerts_log_smoke] appended alerts_smoke to {evidence['path']} ({evidence['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
