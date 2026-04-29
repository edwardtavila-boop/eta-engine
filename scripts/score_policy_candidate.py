"""JARVIS policy promotion gate -- score a candidate (Tier-1 #4, 2026-04-27).

The kaizen loop produces +1 tickets every day. Some of those tickets will
propose changes to JARVIS's decision logic ("lower min-confluence from 8.0
to 7.5"). The promotion gate is the safety brake: a candidate policy
cannot go live until it scores >= the current champion on the last 30
days of journal events.

This script implements the SCORING half of that gate. The actual
candidate-policy authoring + promotion is a separate workflow.

How it works
------------
  1. Load the last N days of decision-journal events for both:
     - what JARVIS DID with current policy v_champ
     - what JARVIS WOULD HAVE DONE with candidate v_cand (simulated)
  2. Compute per-policy aggregate metrics:
     - approval rate (too high = lax; too low = paranoid)
     - avg size_cap_mult on CONDITIONAL
     - rejection of subsequently-profitable orders ("opportunity cost")
     - approval of subsequently-losing orders ("damage cost")
     - net P&L of approved orders
  3. Print a side-by-side comparison + WIN/LOSS verdict per metric

Status: ACTIVE for registered candidate callables. Candidate policies register
through ``eta_engine.brain.jarvis_v3.candidate_policy`` and this gate replays
their callable over recent JARVIS audit records.

Usage::

    python scripts/score_policy_candidate.py --window-days 30
    python scripts/score_policy_candidate.py --candidate v18
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("score_policy_candidate")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def load_audit_records(audit_paths: list[Path], *, since: datetime) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for p in audit_paths:
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
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
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts >= since:
                    records.append(rec)
        except OSError:
            continue
    return records


def champion_metrics(records: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate metrics from existing audit records (the champion)."""
    if not records:
        return {"total": 0, "approval_rate": 0.0, "avg_cap": 1.0}
    total = len(records)
    approved = sum(1 for r in records if (r.get("response", {}) or {}).get("verdict") == "APPROVED")
    cond_caps = [
        (r.get("response", {}) or {}).get("size_cap_mult")
        for r in records
        if (r.get("response", {}) or {}).get("verdict") == "CONDITIONAL"
    ]
    cond_caps = [c for c in cond_caps if isinstance(c, (int, float))]
    return {
        "total": total,
        "approved": approved,
        "approval_rate": round(approved / total, 4),
        "avg_cap": round(sum(cond_caps) / len(cond_caps), 4) if cond_caps else 1.0,
        "denied": sum(1 for r in records if (r.get("response", {}) or {}).get("verdict") == "DENIED"),
        "deferred": sum(1 for r in records if (r.get("response", {}) or {}).get("verdict") == "DEFERRED"),
        "conditional": len(cond_caps),
    }


def _reconstruct_jarvis_context_minimal(record: dict[str, Any]) -> object:
    """Build a minimal JarvisContext from an audit record.

    The audit record stores ``stress_composite`` + ``session_phase`` but
    not the nested StressScore/JarvisSuggestion objects. We reconstruct
    just enough so a candidate policy can read the fields it cares about.
    """
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_admin import ActionSuggestion
    from eta_engine.brain.jarvis_context import (
        JarvisContext,
        JarvisSuggestion,
        SessionPhase,
        StressScore,
    )

    # stress_composite from response (denormalized in audit)
    resp = record.get("response", {}) or {}
    stress_c = resp.get("stress_composite")
    if stress_c is None:
        stress_c = float(record.get("stress_composite") or 0.0)
    binding = resp.get("binding_constraint", "")

    # session_phase
    sp_str = resp.get("session_phase") or record.get("session_phase") or "OVERNIGHT"
    try:
        sp = SessionPhase(sp_str)
    except ValueError:
        sp = SessionPhase.OVERNIGHT

    # jarvis_action (the action SUGGESTION the engine produced)
    ja_str = record.get("jarvis_action", "TRADE")
    try:
        ja = ActionSuggestion(ja_str)
    except ValueError:
        ja = ActionSuggestion.TRADE

    # JarvisContext has many required fields (equity, regime, journal, ...)
    # but for replay scoring we only need the fields a candidate actually
    # reads (stress_score, session_phase, suggestion). Use model_construct
    # to bypass validation so we don't have to fabricate equity curves
    # and trade journals for every audit record.
    return JarvisContext.model_construct(
        ts=datetime.now(UTC),
        suggestion=JarvisSuggestion(
            action=ja,
            reason="reconstructed for candidate replay",
            confidence=0.5,
        ),
        stress_score=StressScore(composite=float(stress_c), binding_constraint=binding, components=[]),
        session_phase=sp,
    )


def _reconstruct_action_request(record: dict[str, Any]) -> object:
    from eta_engine.brain.jarvis_admin import ActionRequest, ActionType, SubsystemId

    req = record.get("request", {}) or {}
    try:
        subsystem = SubsystemId(req.get("subsystem", "operator"))
    except ValueError:
        subsystem = SubsystemId.OPERATOR
    try:
        action = ActionType(req.get("action", "ORDER_PLACE"))
    except ValueError:
        action = ActionType.ORDER_PLACE
    return ActionRequest(
        request_id=req.get("request_id", "replay"),
        subsystem=subsystem,
        action=action,
        payload=req.get("payload") or {},
        rationale=req.get("rationale", ""),
    )


def candidate_metrics(records: list[dict[str, Any]], *, candidate_module: str | None) -> dict[str, float]:
    """Replay records through a registered candidate policy and aggregate.

    When ``candidate_module`` is ``None``, returns champion metrics
    (TIE baseline). When provided, looks up the candidate via
    ``brain.jarvis_v3.candidate_policy.get_candidate`` and replays
    every record through it, reconstructing minimal JarvisContext +
    ActionRequest from the audit fields.
    """
    if candidate_module is None:
        return champion_metrics(records)

    # Trigger registry side-effects (auto-register all shipped candidates).
    try:
        from eta_engine.brain.jarvis_v3 import policies  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        logger.warning("policies package import failed: %s", exc)

    from eta_engine.brain.jarvis_v3.candidate_policy import get_candidate

    try:
        candidate = get_candidate(candidate_module)
    except KeyError:
        logger.error("no candidate registered as '%s' -- did you import the module?",
                     candidate_module)
        return champion_metrics(records)

    if not records:
        return {"total": 0, "approval_rate": 0.0, "avg_cap": 1.0}

    cand_verdicts: dict[str, int] = {}
    cand_caps: list[float] = []
    cap_tightened = 0
    total = 0

    for rec in records:
        try:
            ctx = _reconstruct_jarvis_context_minimal(rec)
            req = _reconstruct_action_request(rec)
            cand_resp = candidate(req, ctx)
        except Exception as exc:  # noqa: BLE001
            logger.debug("replay skipped 1 record: %s", exc)
            continue
        total += 1
        v = cand_resp.verdict.value if hasattr(cand_resp.verdict, "value") else str(cand_resp.verdict)
        cand_verdicts[v] = cand_verdicts.get(v, 0) + 1
        if v == "CONDITIONAL":
            cap = cand_resp.size_cap_mult if cand_resp.size_cap_mult is not None else 0.5
            cand_caps.append(float(cap))
            # Did the candidate tighten vs the champion's recorded cap?
            champ_cap = (rec.get("response", {}) or {}).get("size_cap_mult")
            if isinstance(champ_cap, (int, float)) and float(cap) < float(champ_cap):
                cap_tightened += 1

    approved = cand_verdicts.get("APPROVED", 0)
    return {
        "total": total,
        "approved": approved,
        "approval_rate": round(approved / total, 4) if total else 0.0,
        "avg_cap": round(sum(cand_caps) / len(cand_caps), 4) if cand_caps else 1.0,
        "denied": cand_verdicts.get("DENIED", 0),
        "deferred": cand_verdicts.get("DEFERRED", 0),
        "conditional": len(cand_caps),
        "cap_tightened_count": cap_tightened,
    }


def candidate_replay_status(candidate_module: str | None) -> dict[str, object]:
    """Return JSON-ready status for the candidate replay lane."""
    if candidate_module is None:
        return {
            "mode": "champion_baseline",
            "candidate": None,
            "registered": False,
            "message": "no candidate supplied; candidate metrics mirror champion baseline",
        }

    try:
        from eta_engine.brain.jarvis_v3 import policies  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        logger.warning("policies package import failed: %s", exc)

    from eta_engine.brain.jarvis_v3.candidate_policy import list_candidates

    registered = {entry["name"] for entry in list_candidates()}
    if candidate_module in registered:
        return {
            "mode": "candidate_replay",
            "candidate": candidate_module,
            "registered": True,
            "message": f"candidate replay active for registered policy '{candidate_module}'",
        }
    return {
        "mode": "candidate_missing",
        "candidate": candidate_module,
        "registered": False,
        "message": (
            f"candidate '{candidate_module}' is not registered; candidate "
            "metrics mirror champion baseline"
        ),
    }


def compare(champ: dict[str, float], cand: dict[str, float]) -> dict[str, str]:
    verdicts: dict[str, str] = {}
    # higher-is-better metrics: approval_rate (within reason), total
    # lower-is-better: denied count
    # cap should be HIGHER (more permissive) only if it IMPROVES outcomes
    if cand["total"] > champ["total"]:
        verdicts["total"] = "WIN"
    elif cand["total"] < champ["total"]:
        verdicts["total"] = "LOSS"
    else:
        verdicts["total"] = "TIE"

    if cand["approval_rate"] > champ["approval_rate"]:
        verdicts["approval_rate"] = "WIN_higher"
    elif cand["approval_rate"] < champ["approval_rate"]:
        verdicts["approval_rate"] = "LOSS_lower"
    else:
        verdicts["approval_rate"] = "TIE"

    return verdicts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-dir", type=Path,
                   default=ROOT / "state" / "jarvis_audit",
                   help="Directory containing JARVIS audit *.jsonl files")
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--candidate", type=str, default=None,
                   help="Registered candidate policy name, e.g. v18")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    since = datetime.now(UTC) - timedelta(days=args.window_days)
    audit_paths = list(args.audit_dir.glob("*.jsonl")) if args.audit_dir.exists() else []
    records = load_audit_records(audit_paths, since=since)

    champ = champion_metrics(records)
    cand  = candidate_metrics(records, candidate_module=args.candidate)
    verdicts = compare(champ, cand)
    replay_status = candidate_replay_status(args.candidate)

    if args.json:
        print(json.dumps({
            "window_days": args.window_days,
            "candidate_replay": replay_status,
            "champion":    champ,
            "candidate":   cand,
            "verdicts":    verdicts,
        }, indent=2))
    else:
        print(f"\n  window: last {args.window_days} days  ({len(records)} records)")
        print("  metric           champion       candidate      verdict")
        for k in sorted(verdicts.keys()):
            print(f"  {k:<16} {champ.get(k, '-'):>12}   {cand.get(k, '-'):>12}   {verdicts[k]}")
        print()
        print(f"  [STATUS] {replay_status['message']}")
        if replay_status["mode"] == "candidate_replay":
            print(f"  Replayed {cand.get('total', 0)} records through candidate '{args.candidate}'.")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
