"""Daily calibration fit (Tier-2 #7 wiring, 2026-04-27).

Wires ``brain/jarvis_v3/calibration.py::fit_from_audit`` to a scheduled
task. Once a day, fits a Platt sigmoid that maps JARVIS verdict
features (stress, session_phase, binding_constraint) to realized
P&L outcomes, and persists the calibrator so subsequent verdicts can
be confidence-weighted.

The persisted calibrator is a small JSON blob -- consumers load it
via ``PlattSigmoid.parse_file(path)``.

Run via ``Eta-Calibration-Daily`` at 23:00 ET (after kaizen close + critique).
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

logger = logging.getLogger("run_calibration_fit")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-dir", type=Path,
                   default=ROOT / "state" / "jarvis_audit")
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--out-path", type=Path,
                   default=ROOT / "state" / "calibration" / "platt_sigmoid.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from eta_engine.brain.jarvis_v3.calibration import fit_from_audit

    cutoff = datetime.now(UTC) - timedelta(days=args.window_days)
    audit_files = list(args.audit_dir.glob("*.jsonl")) if args.audit_dir.exists() else []
    logger.info("fitting calibrator over %d audit files (window=%dd, cutoff=%s)",
                len(audit_files), args.window_days, cutoff.date())

    if not audit_files:
        logger.warning("no audit files -- skipping fit")
        return 0

    try:
        sigmoid = fit_from_audit(audit_files, since=cutoff)
    except Exception as exc:  # noqa: BLE001
        logger.error("fit_from_audit failed: %s", exc)
        return 1

    logger.info("fit succeeded: sigmoid=%s",
                sigmoid.model_dump() if hasattr(sigmoid, "model_dump") else sigmoid)

    if not args.dry_run:
        args.out_path.parent.mkdir(parents=True, exist_ok=True)
        args.out_path.write_text(
            sigmoid.model_dump_json(indent=2)
            if hasattr(sigmoid, "model_dump_json") else str(sigmoid),
            encoding="utf-8",
        )
        logger.info("wrote calibrator -> %s", args.out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
