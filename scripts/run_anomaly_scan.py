"""Anomaly scan over JARVIS audit (Tier-2 #5 wiring, 2026-04-27).

Wires ``brain/jarvis_v3/anomaly.py::DriftDetector`` (and
``MultiFieldDetector``) to a scheduled task. Detects when the
distribution of recent JARVIS verdicts has shifted from baseline
(2-sample KS), fires a Resend alert when the anomaly score breaches.

Run every 15 minutes via ``Eta-Anomaly-Scan-15m``. State persists to
``state/anomaly/last_alert.json`` to enforce a cooldown so a sustained
regime shift doesn't spam.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("run_anomaly_scan")


def _load_recent_verdict_stress(audit_dir: Path, *, since: datetime) -> list[float]:
    """Pull stress_composite values from records since cutoff."""
    out: list[float] = []
    if not audit_dir.exists():
        return out
    for f in audit_dir.glob("*.jsonl"):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
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
                if ts < since:
                    continue
                resp = rec.get("response", {}) or {}
                sc = resp.get("stress_composite")
                if isinstance(sc, (int, float)):
                    out.append(float(sc))
        except OSError:
            continue
    return out


def _in_cooldown(state_path: Path, cooldown_min: float) -> bool:
    if not state_path.exists():
        return False
    try:
        last = json.loads(state_path.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(last["last_fired_at"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return (datetime.now(UTC) - ts) < timedelta(minutes=cooldown_min)
    except (KeyError, ValueError, json.JSONDecodeError):
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-dir", type=Path,
                   default=ROOT / "state" / "jarvis_audit")
    p.add_argument("--recent-hours", type=float, default=2.0,
                   help="Compare LAST N hours against the baseline window")
    p.add_argument("--baseline-hours", type=float, default=24.0,
                   help="Baseline window for comparison")
    p.add_argument("--threshold", type=float, default=0.20,
                   help="Anomaly score threshold (KS statistic) to fire alert")
    p.add_argument("--cooldown-min", type=float, default=60.0)
    p.add_argument("--state-file", type=Path,
                   default=ROOT / "state" / "anomaly" / "last_alert.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from eta_engine.brain.jarvis_v3.anomaly import _two_sample_ks

    now = datetime.now(UTC)
    recent = _load_recent_verdict_stress(args.audit_dir, since=now - timedelta(hours=args.recent_hours))
    baseline = _load_recent_verdict_stress(args.audit_dir, since=now - timedelta(hours=args.baseline_hours))
    # Baseline is the wider window (includes recent); strip recent to compare clean
    # We can't easily de-dupe by value; use the longer window minus the count of recent for proxy
    baseline = baseline[:-len(recent)] if len(baseline) > len(recent) else baseline

    if len(recent) < 5 or len(baseline) < 20:
        logger.info("not enough data: recent=%d baseline=%d -- skipping",
                    len(recent), len(baseline))
        return 0

    # 2-sample KS via the existing helper
    ks_stat = _two_sample_ks(recent + baseline)
    logger.info("KS stat = %.4f (recent=%d baseline=%d, threshold=%.2f)",
                ks_stat, len(recent), len(baseline), args.threshold)

    if ks_stat < args.threshold:
        return 0

    if _in_cooldown(args.state_file, args.cooldown_min):
        logger.info("anomaly detected but in cooldown -- skipping alert")
        return 0

    print(f"\n  !! ANOMALY: KS={ks_stat:.4f} >= threshold={args.threshold}")
    if args.dry_run:
        return 0

    try:
        import yaml
        from eta_engine.obs.alert_dispatcher import AlertDispatcher
        cfg = yaml.safe_load((ROOT / "configs" / "alerts.yaml").read_text(encoding="utf-8"))
        dispatcher = AlertDispatcher(cfg)
        result = dispatcher.send("jarvis_anomaly_detected", {
            "ks_stat": ks_stat,
            "threshold": args.threshold,
            "recent_n": len(recent),
            "baseline_n": len(baseline),
            "summary": (
                f"JARVIS verdict-stress distribution shifted (KS={ks_stat:.3f}). "
                f"Possible regime change."
            ),
        })
        logger.info("alert dispatched: delivered=%s", result.delivered)
        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        args.state_file.write_text(json.dumps({
            "last_fired_at": datetime.now(UTC).isoformat(),
            "ks_stat": ks_stat,
        }), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert dispatch failed (non-fatal): %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
