"""Nightly critique runner (Tier-2 #6 wiring, 2026-04-27).

Wires the existing ``brain/jarvis_v3/critique.py`` module into the
nightly kaizen close-cycle as a 2nd reviewer. The kaizen synthesizer
already produces a +1 ticket from the day's events; the critique
module independently scores false-positive and false-negative rates,
flags drift, and classifies the day's review window.

Run via the ``Eta-Critique-Nightly`` scheduled task (22:45 ET, 15 min
after the kaizen close at 22:30 -- so the ticket exists before we
critique it).

Output: appends a structured review note to
``state/kaizen_critique/<YYYY-MM-DD>.json`` and fires a Resend alert
when severity is HIGH.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("run_critique_nightly")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-dir", type=Path,
                   default=ROOT / "state" / "jarvis_audit",
                   help="Directory of JARVIS audit *.jsonl files")
    p.add_argument("--window-hours", type=float, default=24.0)
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "state" / "kaizen_critique")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from eta_engine.brain.jarvis_v3.critique import critique_window, load_decisions

    # Load decisions from every audit file in the window
    cutoff = datetime.now(UTC) - timedelta(hours=args.window_hours)
    all_records = []
    for f in args.audit_dir.glob("*.jsonl") if args.audit_dir.exists() else []:
        try:
            recs = load_decisions(f)
        except Exception as exc:  # noqa: BLE001
            logger.debug("load_decisions failed for %s: %s", f, exc)
            continue
        # Filter to window
        for r in recs:
            ts = getattr(r, "ts", None)
            if ts is None or ts >= cutoff:
                all_records.append(r)

    logger.info("loaded %d decisions in last %.1fh", len(all_records), args.window_hours)

    if not all_records:
        logger.warning("no decisions in window -- nothing to critique")
        return 0

    report = critique_window(all_records)
    logger.info("critique: severity=%s fp=%.3f fn=%.3f drift=%.3f",
                getattr(report, "severity", "?"),
                getattr(report, "false_positive_rate", 0.0),
                getattr(report, "false_negative_rate", 0.0),
                getattr(report, "drift_score", 0.0))

    today = datetime.now(UTC).date().isoformat()
    out_path = args.out_dir / f"{today}.json"
    if not args.dry_run:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        logger.info("wrote %s", out_path)

    # Fire alert on HIGH severity
    severity = getattr(report, "severity", "")
    if severity in ("HIGH", "CRITICAL"):
        try:
            import yaml
            from eta_engine.obs.alert_dispatcher import AlertDispatcher
            cfg = yaml.safe_load((ROOT / "configs" / "alerts.yaml").read_text(encoding="utf-8"))
            dispatcher = AlertDispatcher(cfg)
            result = dispatcher.send("critique_high_severity", {
                "severity": severity,
                "false_positive_rate": getattr(report, "false_positive_rate", 0.0),
                "false_negative_rate": getattr(report, "false_negative_rate", 0.0),
                "drift_score": getattr(report, "drift_score", 0.0),
                "summary": getattr(report, "summary", ""),
            })
            logger.info("alert dispatched: delivered=%s", result.delivered)
        except Exception as exc:  # noqa: BLE001
            logger.warning("critique alert dispatch failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
