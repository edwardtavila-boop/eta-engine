"""
EVOLUTIONARY TRADING ALGO  //  scripts.go_trigger
======================================
Manual operator confirm-phrase gate for live-tiny flip, kill, and resume.

Decision #8 (go-live trigger): "edward types GO" — no auto-promote.
Decision #16 (kill-log SLA): pause + manual resume.

Phrases
-------
  GO APEX MNQ LIVE-TINY       flip MNQ from demo to live (after preflight GO)
  GO APEX NQ LIVE-TINY        flip NQ (only after 30 MNQ trades green)
  GO APEX BYBIT TESTNET       enable Tier-B testnet
  GO APEX BYBIT MAINNET       flip Tier-B to mainnet (after testnet gate)
  KILL APEX NOW               flatten everything, pause, no auto-resume
  RESUME APEX TIER-A          resume after manual review
  RESUME APEX TIER-B          resume after manual review

Usage
-----
    python -m eta_engine.scripts.go_trigger --phrase "GO APEX MNQ LIVE-TINY"
    python -m eta_engine.scripts.go_trigger --phrase "KILL APEX NOW" --reason "BTC flash crash"

Outputs
-------
- docs/go_trigger_log.jsonl   append-only event log (one JSON per line)
- roadmap_state.json patched: shared_artifacts.apex_go_state
- exit 0 on accepted phrase, 2 on rejected, 3 on preflight block

This script does NOT send orders. It sets flags that the running trader reads
and acts on. Orders live in venues/*. This is the human-in-the-loop gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Accepted phrase table — map to (action, required_preflight, target)
PHRASES: dict[str, dict[str, str]] = {
    "GO APEX MNQ LIVE-TINY": {"action": "flip_live", "target": "tier_a_mnq", "requires_preflight": "true"},
    "GO APEX NQ LIVE-TINY": {"action": "flip_live", "target": "tier_a_nq", "requires_preflight": "true"},
    "GO APEX BYBIT TESTNET": {"action": "enable", "target": "tier_b_testnet", "requires_preflight": "false"},
    "GO APEX BYBIT MAINNET": {"action": "flip_live", "target": "tier_b_mainnet", "requires_preflight": "true"},
    "KILL APEX NOW": {"action": "kill_all", "target": "all", "requires_preflight": "false"},
    "RESUME APEX TIER-A": {"action": "resume", "target": "tier_a", "requires_preflight": "true"},
    "RESUME APEX TIER-B": {"action": "resume", "target": "tier_b", "requires_preflight": "true"},
}


@dataclass
class TriggerEvent:
    timestamp_utc: str
    phrase: str
    action: str
    target: str
    accepted: bool
    reason: str
    preflight_verdict: str | None
    operator_note: str


def _preflight_verdict() -> str:
    """Read latest preflight dryrun report; return GO/ABORT/UNKNOWN."""
    p = ROOT / "docs" / "preflight_dryrun_report.json"
    if not p.exists():
        return "UNKNOWN"
    try:
        raw = json.loads(p.read_text())
        return str(raw.get("overall", "UNKNOWN")).upper()
    except Exception:
        return "UNKNOWN"


def _append_log(event: TriggerEvent) -> Path:
    log_p = ROOT / "docs" / "go_trigger_log.jsonl"
    log_p.parent.mkdir(parents=True, exist_ok=True)
    with log_p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event)) + "\n")
    return log_p


def _patch_roadmap_state(event: TriggerEvent) -> Path:
    rs_p = ROOT / "roadmap_state.json"
    if not rs_p.exists():
        return rs_p
    try:
        raw = json.loads(rs_p.read_text())
    except Exception:
        return rs_p
    sa = raw.setdefault("shared_artifacts", {})
    state = sa.setdefault(
        "apex_go_state",
        {
            "tier_a_mnq_live": False,
            "tier_a_nq_live": False,
            "tier_b_testnet": False,
            "tier_b_mainnet": False,
            "kill_switch_active": False,
            "last_trigger": None,
        },
    )
    if event.accepted:
        if event.action == "flip_live" and event.target == "tier_a_mnq":
            state["tier_a_mnq_live"] = True
        elif event.action == "flip_live" and event.target == "tier_a_nq":
            state["tier_a_nq_live"] = True
        elif event.action == "enable" and event.target == "tier_b_testnet":
            state["tier_b_testnet"] = True
        elif event.action == "flip_live" and event.target == "tier_b_mainnet":
            state["tier_b_mainnet"] = True
        elif event.action == "kill_all":
            state["kill_switch_active"] = True
            state["tier_a_mnq_live"] = False
            state["tier_a_nq_live"] = False
            state["tier_b_mainnet"] = False
        elif (
            event.action == "resume"
            and event.target == "tier_a"
            or event.action == "resume"
            and event.target == "tier_b"
        ):
            state["kill_switch_active"] = False
        state["last_trigger"] = asdict(event)
    rs_p.write_text(json.dumps(raw, indent=2))
    return rs_p


def handle(phrase: str, reason: str, skip_preflight: bool = False) -> TriggerEvent:
    now = datetime.now(UTC).isoformat()
    p = phrase.strip().upper()
    if p not in PHRASES:
        return TriggerEvent(
            timestamp_utc=now,
            phrase=phrase,
            action="reject",
            target="n/a",
            accepted=False,
            reason=f"unknown phrase; accepted phrases: {sorted(PHRASES)}",
            preflight_verdict=None,
            operator_note=reason,
        )

    meta = PHRASES[p]
    requires = meta["requires_preflight"] == "true" and not skip_preflight
    pf = _preflight_verdict() if requires else "SKIPPED"
    if requires and pf != "GO":
        return TriggerEvent(
            timestamp_utc=now,
            phrase=p,
            action=meta["action"],
            target=meta["target"],
            accepted=False,
            reason=f"preflight verdict is {pf}, not GO — blocked",
            preflight_verdict=pf,
            operator_note=reason,
        )

    return TriggerEvent(
        timestamp_utc=now,
        phrase=p,
        action=meta["action"],
        target=meta["target"],
        accepted=True,
        reason="accepted",
        preflight_verdict=pf,
        operator_note=reason,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="EVOLUTIONARY TRADING ALGO manual GO/KILL trigger")
    ap.add_argument("--phrase", required=True, help="Exact confirm phrase (case-insensitive)")
    ap.add_argument("--reason", default="", help="Operator note captured in log")
    ap.add_argument("--skip-preflight", action="store_true", help="Override preflight gate (debug only; logged)")
    ap.add_argument("--dry-run", action="store_true", help="Print decision but do not patch state or log")
    args = ap.parse_args()

    evt = handle(args.phrase, args.reason, skip_preflight=args.skip_preflight)

    print("EVOLUTIONARY TRADING ALGO  -- go_trigger")
    print("=" * 64)
    print(f"timestamp : {evt.timestamp_utc}")
    print(f"phrase    : {evt.phrase}")
    print(f"action    : {evt.action}")
    print(f"target    : {evt.target}")
    print(f"preflight : {evt.preflight_verdict}")
    print(f"accepted  : {evt.accepted}")
    print(f"reason    : {evt.reason}")
    if evt.operator_note:
        print(f"note      : {evt.operator_note}")
    print("=" * 64)

    if not args.dry_run:
        _append_log(evt)
        _patch_roadmap_state(evt)
        print("logged + roadmap_state patched")

    if not evt.accepted:
        return 3 if "preflight" in evt.reason.lower() else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
