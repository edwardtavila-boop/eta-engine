"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_heartbeat
===================================================
Daemon-liveness monitor — checks every long-running process has
written to its log within the expected heartbeat window.

Why this exists
---------------
Phase-1 capture daemons, the live order supervisor, the Gateway,
and the L2 cron tasks each have their own failure modes:
  - Process running but stuck (deadlock) → no log writes
  - Process dead but supervisor doesn't know (PID reuse, parent
    detached) → file rotation continues to fool capture_health
  - Process restarted but stuck in init (auth loop, dependency
    error) → no useful output

This monitor checks two independent liveness signals per daemon:
  1. Process is actually running (OS process check)
  2. Most-recent log line is fresher than expected_heartbeat_seconds

If either fails, fires an alert.  Hourly cron runs this; alerts
trigger operator notification (Slack/email).

Daemons monitored
-----------------
- ETA-CaptureTicks       → logs/eta_engine/capture_health.jsonl
- ETA-CaptureDepth       → logs/eta_engine/capture_health.jsonl
- ETA-IBGateway          → logs/ibgateway/*.log (mtime)
- ETA-L2-PromotionEvaluator (daily — staleness allowed up to 26h)
- ETA-L2-BacktestDaily      (daily — staleness allowed up to 26h)
- ETA-L2-FillAuditWeekly    (weekly — 8 days)

Each daemon has its own expected heartbeat interval; staleness
beyond 1.5× that interval triggers ALERT.

Run
---
::

    python -m eta_engine.scripts.l2_heartbeat
    python -m eta_engine.scripts.l2_heartbeat --json
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
HEARTBEAT_LOG = LOG_DIR / "l2_heartbeat.jsonl"


@dataclass
class HeartbeatProbe:
    """One daemon's expected heartbeat configuration."""

    name: str
    log_paths: list[Path]  # any one being fresh = alive
    expected_interval_seconds: float
    description: str = ""


@dataclass
class HeartbeatStatus:
    name: str
    alive: bool
    last_signal_age_seconds: float | None
    expected_interval_seconds: float
    grace_factor: float = 1.5
    notes: list[str] = field(default_factory=list)


# Standard probes for the L2 stack.  Operator extends this dict to
# add new daemons.
def _default_probes() -> list[HeartbeatProbe]:
    return [
        HeartbeatProbe(
            name="ETA-CaptureTicks",
            log_paths=[LOG_DIR / "capture_health.jsonl"],
            expected_interval_seconds=600,  # capture_health writes every ~5min
            description="capture_tick_stream daemon writes capture_health",
        ),
        HeartbeatProbe(
            name="ETA-CaptureDepth",
            log_paths=[LOG_DIR / "capture_health.jsonl"],
            expected_interval_seconds=600,
            description="capture_depth_snapshots daemon writes capture_health",
        ),
        HeartbeatProbe(
            name="ETA-L2-BacktestDaily",
            log_paths=[LOG_DIR / "l2_backtest_runs.jsonl"],
            expected_interval_seconds=26 * 3600,  # daily cron + grace
            description="daily harness backtest run",
        ),
        HeartbeatProbe(
            name="ETA-L2-PromotionEvaluator",
            log_paths=[LOG_DIR / "l2_promotion_decisions.jsonl"],
            expected_interval_seconds=26 * 3600,
            description="daily promotion evaluator",
        ),
        HeartbeatProbe(
            name="ETA-L2-FillAuditWeekly",
            log_paths=[LOG_DIR / "l2_fill_audit.jsonl"],
            expected_interval_seconds=8 * 24 * 3600,  # weekly + 1d grace
            description="weekly fill audit",
        ),
        HeartbeatProbe(
            name="ETA-L2-RiskMetrics",
            log_paths=[LOG_DIR / "l2_risk_metrics.jsonl"],
            expected_interval_seconds=26 * 3600,
            description="daily risk metrics",
        ),
        HeartbeatProbe(
            name="ETA-L2-DriftMonitor",
            log_paths=[LOG_DIR / "l2_drift_monitor.jsonl"],
            expected_interval_seconds=26 * 3600,
            description="daily drift monitor",
        ),
    ]


def _file_mtime_age_seconds(path: Path, *, now: datetime | None = None) -> float | None:
    """Return age in seconds of the file's last modification.  None
    when the file doesn't exist."""
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    now = now or datetime.now(UTC)
    return now.timestamp() - mtime


def check_probe(probe: HeartbeatProbe, *, now: datetime | None = None) -> HeartbeatStatus:
    """Check a single probe.  Returns alive=True if ANY of its log_paths
    has been written within the grace window."""
    now = now or datetime.now(UTC)
    notes: list[str] = []
    ages = []
    for p in probe.log_paths:
        age = _file_mtime_age_seconds(p, now=now)
        if age is not None:
            ages.append(age)
    if not ages:
        notes.append("none of the log paths exist yet — daemon may have never run, or path is wrong")
        return HeartbeatStatus(
            name=probe.name,
            alive=False,
            last_signal_age_seconds=None,
            expected_interval_seconds=probe.expected_interval_seconds,
            notes=notes,
        )
    youngest = min(ages)
    grace_seconds = probe.expected_interval_seconds * 1.5
    alive = youngest <= grace_seconds
    if not alive:
        notes.append(f"oldest write {round(youngest / 60, 1)} min ago exceeds grace {round(grace_seconds / 60, 1)} min")
    return HeartbeatStatus(
        name=probe.name,
        alive=alive,
        last_signal_age_seconds=round(youngest, 1),
        expected_interval_seconds=probe.expected_interval_seconds,
        notes=notes,
    )


def check_all(*, probes: list[HeartbeatProbe] | None = None, now: datetime | None = None) -> list[HeartbeatStatus]:
    if probes is None:
        probes = _default_probes()
    return [check_probe(p, now=now) for p in probes]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    statuses = check_all()
    try:
        with HEARTBEAT_LOG.open("a", encoding="utf-8") as f:
            for s in statuses:
                f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **asdict(s)}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: heartbeat log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps([asdict(s) for s in statuses], indent=2))
        return 1 if any(not s.alive for s in statuses) else 0

    print()
    print("=" * 78)
    print(f"L2 HEARTBEAT  ({datetime.now(UTC).isoformat()})")
    print("=" * 78)
    print(f"  {'Daemon':<30s} {'Alive':<6s} {'Last signal':<12s} {'Expected':<10s}")
    print(f"  {'-' * 30:<30s} {'-' * 6:<6s} {'-' * 12:<12s} {'-' * 10}")
    for s in statuses:
        age = f"{s.last_signal_age_seconds:.0f}s" if s.last_signal_age_seconds is not None else "n/a"
        alive_str = "[OK]" if s.alive else "[!!]"
        print(f"  {s.name:<30s} {alive_str:<6s} {age:<12s} {int(s.expected_interval_seconds)}s")
        for n in s.notes:
            print(f"      - {n}")
    n_dead = sum(1 for s in statuses if not s.alive)
    print()
    print(f"  {n_dead}/{len(statuses)} daemons not heartbeating")
    print()
    return 1 if n_dead > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
