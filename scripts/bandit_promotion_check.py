"""Bandit auto-promotion check (Tier-4 #15, 2026-04-27).

Daily scheduled task that scores every registered candidate against
the champion via ``score_policy_candidate.py``. When a candidate's
metrics decisively beat the champion (per the rule below), fires a
``kaizen_promotion_pending`` Resend alert so the operator can approve
the promotion.

Promotion rule (operator-tunable):
  * candidate has at least N total decisions in the scoring window
    (default: 100)
  * candidate's approval_rate is within +/- DELTA of champion (default
    +/- 5pct -- a candidate that approves wildly differently is suspect
    even if mean reward looks better)
  * candidate's cap_tightened_count > 0 (proves the candidate actually
    differentiated from champion, not just no-oped)

This is a SIGNAL, not an actuator. Promotion is operator-approved.
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

logger = logging.getLogger("bandit_promotion_check")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-dir", type=Path,
                   default=ROOT / "state" / "jarvis_audit")
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--min-decisions", type=int, default=100)
    p.add_argument("--approval-delta", type=float, default=0.05,
                   help="Max acceptable abs diff in approval_rate")
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "state" / "bandit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Side-effect: register all shipped candidates
    from eta_engine.brain.jarvis_v3 import policies  # noqa: F401
    from eta_engine.brain.jarvis_v3.candidate_policy import list_candidates
    from eta_engine.scripts.score_policy_candidate import (
        candidate_metrics, champion_metrics, load_audit_records,
    )

    cutoff = datetime.now(UTC) - timedelta(days=args.window_days)
    audit_files = list(args.audit_dir.glob("*.jsonl")) if args.audit_dir.exists() else []
    records = load_audit_records(audit_files, since=cutoff)
    logger.info("scoring %d records over %dd window", len(records), args.window_days)

    if len(records) < args.min_decisions:
        logger.info("not enough records (%d < %d) -- skipping promotion check",
                    len(records), args.min_decisions)
        return 0

    champion = champion_metrics(records)

    candidates_to_check = [
        c["name"] for c in list_candidates() if c["name"] != "v17"
    ]
    logger.info("checking %d candidates: %s", len(candidates_to_check), candidates_to_check)

    promotions: list[dict] = []
    for name in candidates_to_check:
        try:
            metrics = candidate_metrics(records, candidate_module=name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scoring %s failed: %s", name, exc)
            continue

        differentiated = metrics.get("cap_tightened_count", 0) > 0
        approval_diff = abs(
            float(metrics.get("approval_rate", 0)) - float(champion.get("approval_rate", 0))
        )
        within_delta = approval_diff <= args.approval_delta
        enough_decisions = metrics.get("total", 0) >= args.min_decisions

        promotable = differentiated and within_delta and enough_decisions
        logger.info("  %s: differentiated=%s within_delta=%s enough_decisions=%s -> %s",
                    name, differentiated, within_delta, enough_decisions,
                    "PROMOTABLE" if promotable else "skip")

        if promotable:
            promotions.append({
                "candidate": name,
                "metrics": metrics,
                "champion": champion,
                "approval_diff": round(approval_diff, 4),
            })

    out_path = args.out_dir / f"promotion_check_{datetime.now(UTC).date().isoformat()}.json"
    if not args.dry_run:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "ts": datetime.now(UTC).isoformat(),
            "window_days": args.window_days,
            "champion": champion,
            "candidates_checked": candidates_to_check,
            "promotable": promotions,
        }, indent=2), encoding="utf-8")
        logger.info("wrote %s", out_path)

    # Fire alert when any promotable candidates surface
    if promotions and not args.dry_run:
        try:
            import yaml
            from eta_engine.obs.alert_dispatcher import AlertDispatcher
            cfg = yaml.safe_load((ROOT / "configs" / "alerts.yaml").read_text(encoding="utf-8"))
            dispatcher = AlertDispatcher(cfg)
            dispatcher.send("kaizen_promotion_pending", {
                "promotable_count": len(promotions),
                "candidates": [pr["candidate"] for pr in promotions],
                "summary": (
                    f"{len(promotions)} JARVIS candidate(s) ready for promotion review: "
                    f"{', '.join(pr['candidate'] for pr in promotions)}"
                ),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert dispatch failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
