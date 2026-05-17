"""JARVIS denial-rate alerter (Lever 7, 2026-04-26).

Watches the JARVIS audit JSONL for sudden spikes in DENIED / DEFERRED
verdicts. A sustained spike is a regime-shift early warning -- something
in the live tape just changed enough that JARVIS is rejecting orders it
would normally approve.

Behavior
--------
  * scans audit files matching ``--audit-glob`` (default:
    var/eta_engine/state/jarvis_audit/*.jsonl plus legacy state/jarvis_audit/*.jsonl)
  * looks at the last ``--window-min`` minutes of records (default: 5)
  * computes denial-rate = (DENIED + DEFERRED) / total
  * if the rate exceeds ``--threshold`` (default: 0.50) AND there are
    at least ``--min-events`` records (default: 10), fires a Resend
    ``jarvis_denial_rate_high`` alert
  * applies a cooldown so we don't re-fire every minute -- default
    cooldown is ``--cooldown-min`` (default: 30) minutes between alerts

Usage
-----
  python -m eta_engine.obs.jarvis_denial_rate_alerter
  python -m eta_engine.obs.jarvis_denial_rate_alerter --window-min 5 --threshold 0.5
  python -m eta_engine.obs.jarvis_denial_rate_alerter --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots

logger = logging.getLogger("jarvis_denial_rate_alerter")

# Verdicts that count as "rejection" for denial-rate purposes.
REJECTION_VERDICTS = {"DENIED", "DEFERRED"}


def parse_audit_lines(paths: list[Path], window_min: float) -> list[dict[str, Any]]:
    """Load audit records from one or more JSONL files within the time window."""
    cutoff = datetime.now(UTC) - timedelta(minutes=window_min)
    records: list[dict[str, Any]] = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = rec.get("ts")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    if ts < cutoff:
                        continue
                    records.append(rec)
        except OSError as exc:
            logger.warning("failed to read %s: %s", p, exc)
    return records


def compute_denial_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the denial-rate stats from a list of audit records."""
    counts: Counter[str] = Counter()
    for rec in records:
        verdict = (rec.get("response", {}) or {}).get("verdict")
        if verdict:
            counts[verdict] += 1
    total = sum(counts.values())
    rejections = sum(counts[v] for v in REJECTION_VERDICTS)
    rate = rejections / total if total else 0.0
    return {
        "total": total,
        "rejections": rejections,
        "denial_rate": rate,
        "verdict_counts": dict(counts),
    }


def in_cooldown(state_path: Path, cooldown_min: float) -> bool:
    """Return True if the last alert was within the cooldown window."""
    if not state_path.exists():
        return False
    try:
        last = json.loads(state_path.read_text(encoding="utf-8"))
        last_ts = datetime.fromisoformat(last["last_fired_at"].replace("Z", "+00:00"))
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)
        elapsed = datetime.now(UTC) - last_ts
        return elapsed < timedelta(minutes=cooldown_min)
    except (KeyError, ValueError, json.JSONDecodeError):
        return False


def update_cooldown_state(state_path: Path, payload: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_fired_at": datetime.now(UTC).isoformat(),
                **payload,
            }
        ),
        encoding="utf-8",
    )


def fire_alert(stats: dict[str, Any], *, alerts_yaml: Path) -> bool:
    """Best-effort Resend dispatch. Returns True if delivered to >=1 channel."""
    try:
        import yaml

        from eta_engine.obs.alert_dispatcher import AlertDispatcher

        if not alerts_yaml.exists():
            logger.warning("alerts config not found at %s; skipping", alerts_yaml)
            return False
        cfg = yaml.safe_load(alerts_yaml.read_text(encoding="utf-8"))
        dispatcher = AlertDispatcher(cfg)
        result = dispatcher.send(
            "jarvis_denial_rate_high",
            {
                "denial_rate": stats["denial_rate"],
                "rejections": stats["rejections"],
                "total": stats["total"],
                "verdict_counts": stats["verdict_counts"],
                "summary": (
                    f"JARVIS rejected {stats['rejections']}/{stats['total']} orders "
                    f"({stats['denial_rate']:.0%}) in the recent window. "
                    f"Possible regime shift -- check the tape."
                ),
            },
        )
        logger.info("alert dispatched: delivered=%s blocked=%s", result.delivered, result.blocked)
        return bool(result.delivered)
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert dispatch failed (non-fatal): %s", exc)
        return False


def _cooldown_probe_path(state_path: Path) -> Path:
    if (
        state_path == workspace_roots.ETA_JARVIS_DENIAL_RATE_ALERT_STATE_PATH
        and not state_path.exists()
        and workspace_roots.ETA_LEGACY_JARVIS_DENIAL_RATE_ALERT_STATE_PATH.exists()
    ):
        logger.info(
            "using legacy denial-rate cooldown fallback: %s",
            workspace_roots.ETA_LEGACY_JARVIS_DENIAL_RATE_ALERT_STATE_PATH,
        )
        return workspace_roots.ETA_LEGACY_JARVIS_DENIAL_RATE_ALERT_STATE_PATH
    return state_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--audit-glob",
        action="append",
        default=None,
        help="Glob(s) for JARVIS audit JSONL files. Repeatable. "
        "Default: var/eta_engine/state/jarvis_audit/*.jsonl plus legacy state/jarvis_audit/*.jsonl",
    )
    p.add_argument("--window-min", type=float, default=5.0, help="Look back this many minutes (default: 5)")
    p.add_argument("--threshold", type=float, default=0.50, help="Fire when denial_rate >= this (default: 0.50)")
    p.add_argument(
        "--min-events", type=int, default=10, help="Need at least this many records to evaluate (default: 10)"
    )
    p.add_argument(
        "--cooldown-min",
        type=float,
        default=30.0,
        help="Don't re-fire within this many minutes of last alert (default: 30)",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=workspace_roots.ETA_JARVIS_DENIAL_RATE_ALERT_STATE_PATH,
        help="State file for cooldown tracking. Default: var/eta_engine/state/jarvis_denial_rate_state.json",
    )
    p.add_argument("--alerts-yaml", type=Path, default=ROOT / "configs" / "alerts.yaml", help="Path to alerts.yaml")
    p.add_argument("--dry-run", action="store_true", help="Compute + print stats but do not fire alert or update state")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Resolve audit globs
    if args.audit_glob:
        globs = args.audit_glob
    else:
        globs = [
            str(workspace_roots.ETA_JARVIS_AUDIT_DIR / "*.jsonl"),
            str(workspace_roots.ETA_LEGACY_JARVIS_AUDIT_DIR / "*.jsonl"),
        ]
    paths: list[Path] = []
    for g in globs:
        gp = Path(g)
        if gp.is_file():
            paths.append(gp)
            continue
        # Glob expansion
        for match in Path(gp.parent).glob(gp.name):
            paths.append(match)

    logger.info("scanning %d audit files in window=%.1f min", len(paths), args.window_min)

    records = parse_audit_lines(paths, args.window_min)
    stats = compute_denial_stats(records)
    logger.info(
        "stats: total=%d rejections=%d rate=%.1f%%",
        stats["total"],
        stats["rejections"],
        stats["denial_rate"] * 100,
    )

    if stats["total"] < args.min_events:
        logger.info(
            "only %d events (< min %d) -- not enough to evaluate, exiting clean", stats["total"], args.min_events
        )
        return 0

    if stats["denial_rate"] < args.threshold:
        logger.info(
            "rate %.1f%% < threshold %.1f%% -- nominal, exiting clean", stats["denial_rate"] * 100, args.threshold * 100
        )
        return 0

    # Threshold breached
    if in_cooldown(_cooldown_probe_path(args.state_file), args.cooldown_min):
        logger.info("threshold breached BUT in cooldown (%.1f min) -- skipping alert", args.cooldown_min)
        return 0

    print("\n  !! JARVIS DENIAL-RATE THRESHOLD BREACHED")
    print(f"     rate:        {stats['denial_rate']:.1%}")
    print(f"     rejections:  {stats['rejections']} / {stats['total']}")
    print(f"     verdicts:    {stats['verdict_counts']}")
    print()

    if args.dry_run:
        print("  (dry-run) alert NOT fired; cooldown state NOT updated")
        return 0

    delivered = fire_alert(stats, alerts_yaml=args.alerts_yaml)
    if delivered:
        update_cooldown_state(
            args.state_file,
            {
                "denial_rate": stats["denial_rate"],
                "rejections": stats["rejections"],
                "total": stats["total"],
            },
        )
        logger.info("alert delivered + cooldown state updated -> %s", args.state_file)
    else:
        logger.warning("alert dispatch returned no deliveries; not entering cooldown")

    return 0


if __name__ == "__main__":
    sys.exit(main())
