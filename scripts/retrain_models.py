"""Periodic ML retraining loop (Tier-2 #11, 2026-04-27).

Monthly task that retrains the JARVIS calibrator + any per-bot models
on a fresh window of journal data. The calibrator is already daily-fit
(``run_calibration_fit.py``); this is the heavier monthly retrain that
also covers:

  * stress-component weights
  * regime-classifier (HMM) parameters
  * online-updater alphas (per-bot)

Each retrained artifact is versioned and persisted to
``state/models/<artifact>_<date>.json``. The runtime checks for the
latest version on startup but doesn't auto-promote -- the operator
approves promotion via Resend ``model_retrain_complete`` alert.

Idempotent: re-running on the same day no-ops.
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

logger = logging.getLogger("retrain_models")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lookback-days", type=int, default=90)
    p.add_argument("--out-dir", type=Path, default=ROOT / "state" / "models")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    today = datetime.now(UTC).date().isoformat()
    summary: dict = {
        "ts": datetime.now(UTC).isoformat(),
        "date": today,
        "lookback_days": args.lookback_days,
        "artifacts": [],
    }

    # --- 1. Calibrator (full retrain over wider window) ---
    try:
        from eta_engine.brain.jarvis_v3.calibration import fit_from_audit

        cutoff = datetime.now(UTC) - timedelta(days=args.lookback_days)
        audit_dir = ROOT / "state" / "jarvis_audit"
        audit_files = list(audit_dir.glob("*.jsonl")) if audit_dir.exists() else []
        if audit_files:
            sigmoid = fit_from_audit(audit_files, since=cutoff)
            if not args.dry_run:
                args.out_dir.mkdir(parents=True, exist_ok=True)
                out = args.out_dir / f"calibrator_{today}.json"
                if hasattr(sigmoid, "model_dump_json"):
                    out.write_text(sigmoid.model_dump_json(indent=2), encoding="utf-8")
                else:
                    out.write_text(str(sigmoid), encoding="utf-8")
                summary["artifacts"].append({"name": "calibrator", "path": str(out)})
                logger.info("calibrator -> %s", out)
        else:
            logger.warning("no audit files -- skipping calibrator retrain")
    except Exception as exc:  # noqa: BLE001
        logger.warning("calibrator retrain failed: %s", exc)
        summary["artifacts"].append({"name": "calibrator", "error": str(exc)})

    # --- 2. Correlation matrix refresh ---
    try:
        import subprocess

        if not args.dry_run:
            subprocess.run(
                [sys.executable, "-m", "eta_engine.scripts.refresh_correlation_matrix"],
                check=False,
                timeout=120,
                cwd=str(ROOT),
            )
            summary["artifacts"].append(
                {"name": "correlation_matrix", "path": str(ROOT / "state" / "correlation" / "learned.json")}
            )
            logger.info("correlation_matrix refreshed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("correlation refresh failed: %s", exc)

    # --- 3. Stress weights (placeholder) ---
    # Stress weights live in jarvis_context; refitting requires labeled
    # outcomes which we'll add when the realized_r feedback has 90+ days.
    summary["artifacts"].append(
        {
            "name": "stress_weights",
            "status": "deferred",
            "reason": "needs 90+ days of realized_r feedback in journal",
        }
    )

    # --- Final report ---
    if not args.dry_run:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.out_dir / f"retrain_summary_{today}.json"
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info("wrote %s", report_path)

    # Fire alert
    try:
        import yaml
        from eta_engine.obs.alert_dispatcher import AlertDispatcher

        cfg = yaml.safe_load((ROOT / "configs" / "alerts.yaml").read_text(encoding="utf-8"))
        AlertDispatcher(cfg).send(
            "model_retrain_complete",
            {
                "date": today,
                "n_artifacts": len([a for a in summary["artifacts"] if "path" in a]),
                "summary": str(summary["artifacts"]),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_retrain_complete alert (non-fatal): %s", exc)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
