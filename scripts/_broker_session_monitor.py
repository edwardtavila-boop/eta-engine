"""Daily active-broker session monitor.

Probe an active futures broker (IBKR or Tastytrade) via
``BrokerConnectionManager.connect_name`` and surface:

  * credentials missing (STUBBED) -> YELLOW
  * degraded endpoint (DEGRADED) -> YELLOW
  * hard failure (FAILED / UNAVAILABLE) -> RED
  * healthy probe (READY) -> GREEN

Side effects when run as a script:

  * writes ``docs/{broker}_session_status.json`` with the probe result +
    a UTC timestamp so downstream dashboards have a single artifact
    location per broker.
  * appends one JSON line to ``docs/alerts_log.jsonl`` when the level is
    YELLOW or RED. Identical-or-lower-severity duplicates inside a
    ``--dedupe-h`` window are suppressed so the alert log doesn't flood.

Exit codes
----------
0  GREEN  -- healthy probe (READY)
1  YELLOW -- creds missing / degraded
2  RED    -- hard failure / adapter unavailable
3  ARG    -- unsupported broker name on the CLI

Design notes
------------
* Probes do not place orders. ``BrokerConnectionManager.connect_name``
  is contractually read-only for the IBKR + Tastytrade adapters (both
  return a VenueConnectionReport from cred + endpoint inspection).
* Network calls are best-effort -- adapter exceptions are caught by
  ``BrokerConnectionManager`` and surfaced as FAILED with an error
  string we write into the status file.
* Designed to run as a daily remote trigger (see
  ``scripts/schedule_active_broker_monitors.py`` for registration).
  Keeping this script single-file + stdlib-only ensures it runs on any
  clone of the repo without the full dev environment.
* This script deliberately excludes Tradovate -- that broker is DORMANT
  per operator mandate 2026-04-24 (funding-blocked). Flip back by
  emptying ``venues/router.py`` DORMANT_BROKERS and adding ``tradovate``
  to ``ACTIVE_BROKERS`` below.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from apex_predator.venues.base import ConnectionStatus, VenueConnectionReport
from apex_predator.venues.connection import BrokerConnectionManager

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS_DIR = ROOT / "docs"
DEFAULT_ALERTS_LOG = ROOT / "docs" / "alerts_log.jsonl"

ACTIVE_BROKERS = ("ibkr", "tastytrade")


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

_STATUS_TO_LEVEL: dict[ConnectionStatus, str] = {
    ConnectionStatus.READY: "GREEN",
    ConnectionStatus.DEGRADED: "YELLOW",
    ConnectionStatus.STUBBED: "YELLOW",
    ConnectionStatus.FAILED: "RED",
    ConnectionStatus.UNAVAILABLE: "RED",
}

_LEVEL_EXIT: dict[str, int] = {
    "GREEN": 0,
    "YELLOW": 1,
    "RED": 2,
}


def classify(report: VenueConnectionReport) -> tuple[str, str]:
    """Return (level, human-readable reason) for a probe report."""
    level = _STATUS_TO_LEVEL.get(report.status, "RED")
    if report.status is ConnectionStatus.READY:
        reason = "READY"
    elif report.status is ConnectionStatus.STUBBED:
        reason = report.error or "creds missing / adapter in STUB mode"
    elif report.status is ConnectionStatus.DEGRADED:
        reason = report.error or "endpoint reachable but degraded"
    elif report.status is ConnectionStatus.FAILED:
        reason = report.error or "adapter probe raised; see details.endpoint"
    elif report.status is ConnectionStatus.UNAVAILABLE:
        reason = report.error or "broker adapter not available in this repo"
    else:
        reason = f"unknown status {report.status!r}"
    return level, reason


# ---------------------------------------------------------------------------
# Artifact writers
# ---------------------------------------------------------------------------

def status_path(broker: str, *, status_dir: Path = DEFAULT_STATUS_DIR) -> Path:
    return status_dir / f"{broker}_session_status.json"


def write_status_file(
    broker: str,
    report: VenueConnectionReport,
    level: str,
    reason: str,
    *,
    status_dir: Path = DEFAULT_STATUS_DIR,
) -> Path:
    """Persist a single-broker status snapshot as JSON."""
    status_dir.mkdir(parents=True, exist_ok=True)
    out = status_path(broker, status_dir=status_dir)
    payload = {
        "broker": broker,
        "level": level,
        "reason": reason,
        "status": report.status.value,
        "creds_present": bool(report.creds_present),
        "error": report.error,
        "details": report.details,
        "generated_at_utc": datetime.now(UTC).isoformat(),
    }
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return out


def _recent_alert_within(
    alerts_path: Path,
    *,
    event: str,
    broker: str,
    level: str,
    now_ts: float,
    dedupe_h: float,
) -> bool:
    """Return True if a same-event, same-broker, >=same-severity alert
    was written inside the last ``dedupe_h`` hours."""
    if not alerts_path.exists() or dedupe_h <= 0:
        return False
    cutoff = now_ts - dedupe_h * 3600.0
    severity = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    want = severity.get(level, 0)
    try:
        lines = alerts_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        ts = float(row.get("ts") or 0.0)
        if ts < cutoff:
            break
        if row.get("event") != event:
            continue
        payload = row.get("payload") or {}
        if payload.get("broker") != broker:
            continue
        prior_level = (row.get("level") or "").upper()
        if severity.get(prior_level, 0) >= want:
            return True
    return False


def append_alert(
    broker: str,
    level: str,
    reason: str,
    *,
    alerts_path: Path = DEFAULT_ALERTS_LOG,
    now_ts: float | None = None,
    dedupe_h: float = 20.0,
    event: str = "broker_session_health",
) -> bool:
    """Append a single alert line if not duplicate-suppressed.

    Returns True if a new line was written, False if suppressed.
    """
    if now_ts is None:
        now_ts = time.time()
    if _recent_alert_within(
        alerts_path,
        event=event,
        broker=broker,
        level=level,
        now_ts=now_ts,
        dedupe_h=dedupe_h,
    ):
        return False
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": now_ts,
        "event": event,
        "level": level,
        "channels": [],
        "delivered": [],
        "blocked": [],
        "payload": {
            "broker": broker,
            "reason": reason,
        },
    }
    with alerts_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, default=str) + "\n")
    return True


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

async def probe(broker: str) -> VenueConnectionReport:
    """Run the read-only connect probe for ``broker``.

    Delegates to ``BrokerConnectionManager.connect_name`` so adapter
    construction, cred loading, and error handling stay in one place.
    """
    mgr = BrokerConnectionManager.from_env()
    return await mgr.connect_name(broker)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--broker",
        required=True,
        choices=list(ACTIVE_BROKERS),
        help="active broker to probe",
    )
    p.add_argument(
        "--status-dir",
        type=Path,
        default=DEFAULT_STATUS_DIR,
        help="where to write {broker}_session_status.json",
    )
    p.add_argument(
        "--alerts-log",
        type=Path,
        default=DEFAULT_ALERTS_LOG,
        help="append-only JSONL path for YELLOW/RED alerts",
    )
    p.add_argument(
        "--dedupe-h",
        type=float,
        default=20.0,
        help="suppress duplicate alerts within this many hours (default 20h)",
    )
    p.add_argument(
        "--no-alerts",
        action="store_true",
        help="write status file only; never append to alerts log",
    )
    args = p.parse_args(argv)

    try:
        report = asyncio.run(probe(args.broker))
    except Exception as exc:  # noqa: BLE001
        # BrokerConnectionManager catches adapter errors internally, so a
        # raise here means something structural broke -- worth surfacing
        # loud to the trigger.
        print(
            f"broker-session-monitor[{args.broker}]: RED -- probe crashed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    level, reason = classify(report)
    out = write_status_file(args.broker, report, level, reason, status_dir=args.status_dir)
    print(f"broker-session-monitor[{args.broker}]: {level} -- {reason}")
    print(f"  status file -> {out}")

    wrote_alert = False
    if not args.no_alerts and level in {"YELLOW", "RED"}:
        wrote_alert = append_alert(
            args.broker,
            level,
            reason,
            alerts_path=args.alerts_log,
            dedupe_h=args.dedupe_h,
        )
        if wrote_alert:
            print(f"  alert appended -> {args.alerts_log}")
        else:
            print(f"  alert dedupe-suppressed (<{args.dedupe_h:.0f}h since last)")

    return _LEVEL_EXIT[level]


if __name__ == "__main__":
    sys.exit(main())
