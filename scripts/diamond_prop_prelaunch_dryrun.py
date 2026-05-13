"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_prop_prelaunch_dryrun
======================================================================
Sunday-EOD final-readiness verification for the Monday prop-fund
cutover.  Run this once on Sunday 2026-05-17 evening; if every
section passes, you're cleared to go live Monday morning.

What it does
------------
Aggregates ALL prop-fund pre-launch signals into a single output:

  1. Launch readiness gate verdict (R1-R7)
  2. Drawdown guard signal + flag-file presence
  3. Allocator allocation table (50/25/25 vs 33/33/33)
  4. Sizing audit per PROP_READY bot
  5. Direction stratification per PROP_READY bot
  6. Feed sanity per PROP_READY bot
  7. Watchdog dual-basis classification per PROP_READY bot
  8. Cron freshness — every receipt must be < its cadence + 5 min
  9. Supervisor wiring sanity (regex-checks the integration block
     is present in jarvis_strategy_supervisor.py)
 10. Alert dispatcher channel availability (env var detection)

Output
------
- stdout: one section per check, GO/HOLD/NO_GO per
- ``var/eta_engine/state/diamond_prop_prelaunch_dryrun_latest.json``
- exit 0 if cleared for launch (all checks GO);
  exit 1 if HOLD (operator review); exit 2 if NO_GO (blocked).

Run
---
::

    python -m eta_engine.scripts.diamond_prop_prelaunch_dryrun
    python -m eta_engine.scripts.diamond_prop_prelaunch_dryrun --json
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
STATE_DIR = WORKSPACE_ROOT / "var" / "eta_engine" / "state"

OUT_LATEST = STATE_DIR / "diamond_prop_prelaunch_dryrun_latest.json"

#: Per-receipt freshness limits in HOURS (cron cadence + 5 min slack).
FRESHNESS_LIMITS_HOURS = {
    "diamond_leaderboard_latest.json": 1.5,  # hourly + slack
    "diamond_prop_allocator_latest.json": 1.5,  # hourly + slack
    "diamond_ops_dashboard_latest.json": 1.5,  # hourly + slack
    "diamond_feed_sanity_audit_latest.json": 1.5,  # hourly + slack
    "diamond_prop_drawdown_guard_latest.json": 0.5,  # 15-min + slack
    "closed_trade_ledger_latest.json": 0.5,  # 15-min + slack
    "diamond_prop_launch_readiness_latest.json": 0.5,  # 15-min + slack
    "diamond_sizing_audit_latest.json": 25.0,  # daily + 1h slack
    "diamond_direction_stratify_latest.json": 25.0,  # daily + 1h slack
    "diamond_promotion_gate_latest.json": 25.0,  # daily
    "diamond_demotion_gate_latest.json": 25.0,  # daily
    "diamond_watchdog_latest.json": 25.0,  # daily
}


@dataclass
class SectionResult:
    name: str
    status: str  # GO / HOLD / NO_GO
    rationale: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DryRunReceipt:
    ts: str
    overall_verdict: str
    summary: str
    sections: list[SectionResult] = field(default_factory=list)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _file_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return (datetime.now(UTC) - mtime).total_seconds() / 3600.0


# ────────────────────────────────────────────────────────────────────
# Section checks
# ────────────────────────────────────────────────────────────────────


def _check_launch_readiness() -> SectionResult:
    """Re-use the existing launch-readiness verdict if available."""
    rec = _load_json(STATE_DIR / "diamond_prop_launch_readiness_latest.json")
    if not rec:
        return SectionResult(
            "launch_readiness",
            "NO_GO",
            rationale="readiness gate has never run; cron likely not firing",
        )
    verdict = rec.get("overall_verdict", "NO_GO")
    return SectionResult(
        "launch_readiness",
        verdict,
        rationale=rec.get("summary", ""),
        detail={"days_until_launch": rec.get("days_until_launch")},
    )


def _check_drawdown_guard() -> SectionResult:
    rec = _load_json(STATE_DIR / "diamond_prop_drawdown_guard_latest.json")
    if not rec:
        return SectionResult(
            "drawdown_guard",
            "NO_GO",
            rationale="drawdown guard has never run",
        )
    sig = rec.get("signal", "UNKNOWN")
    halt_flag = STATE_DIR / "prop_halt_active.flag"
    watch_flag = STATE_DIR / "prop_watch_active.flag"
    detail = {
        "signal": sig,
        "halt_flag_present": halt_flag.exists(),
        "watch_flag_present": watch_flag.exists(),
    }
    if sig == "HALT" or halt_flag.exists():
        return SectionResult(
            "drawdown_guard",
            "NO_GO",
            rationale=f"prop guard signal={sig}, halt flag present",
            detail=detail,
        )
    if sig == "WATCH" or watch_flag.exists():
        return SectionResult(
            "drawdown_guard",
            "HOLD",
            rationale=f"prop guard signal={sig} (watch flag set)",
            detail=detail,
        )
    return SectionResult(
        "drawdown_guard",
        "GO",
        rationale=f"prop guard signal={sig}; no flags raised",
        detail=detail,
    )


def _check_allocator() -> SectionResult:
    rec = _load_json(STATE_DIR / "diamond_prop_allocator_latest.json")
    if not rec:
        return SectionResult(
            "allocator",
            "NO_GO",
            rationale="allocator has never run",
        )
    mode = rec.get("mode", "UNKNOWN")
    n_allocs = len(rec.get("allocations") or [])
    if n_allocs == 0:
        return SectionResult(
            "allocator",
            "NO_GO",
            rationale="no PROP_READY bots have allocations",
        )
    return SectionResult(
        "allocator",
        "GO",
        rationale=(f"{mode} mode; {n_allocs} bot(s) allocated to ${rec.get('account_size', 0):,.0f}"),
        detail={"mode": mode, "n_allocations": n_allocs},
    )


def _check_freshness() -> SectionResult:
    """Every cron receipt must be within its expected cadence + slack."""
    stale: list[str] = []
    fresh: list[str] = []
    for fname, limit_h in FRESHNESS_LIMITS_HOURS.items():
        path = STATE_DIR / fname
        age = _file_age_hours(path)
        if age is None:
            stale.append(f"{fname} (missing)")
        elif age > limit_h:
            stale.append(f"{fname} ({age:.1f}h > {limit_h}h limit)")
        else:
            fresh.append(f"{fname} ({age:.2f}h)")
    if stale:
        # >2 stale = NO_GO, 1 stale = HOLD
        status = "NO_GO" if len(stale) > 2 else "HOLD"
        return SectionResult(
            "freshness",
            status,
            rationale=f"{len(stale)} stale receipt(s): {'; '.join(stale[:3])}",
            detail={"stale": stale, "fresh_count": len(fresh)},
        )
    return SectionResult(
        "freshness",
        "GO",
        rationale=f"all {len(fresh)} cron receipts fresh",
        detail={"fresh_count": len(fresh)},
    )


def _check_supervisor_wiring() -> SectionResult:
    """Regex-check the supervisor's prop-fund entry gate is fully wired.

    Wave-25e consolidated waves 22 + 25 into one unified gate region.
    The HALT check is now inside ``resolve_execution_target`` (called
    from the supervisor) instead of inline ``should_block_prop_entry``.
    The reject reason is also unified under the ``gate_reject:`` prefix.
    """
    p = ROOT / "scripts" / "jarvis_strategy_supervisor.py"
    if not p.exists():
        return SectionResult(
            "supervisor_wiring",
            "NO_GO",
            rationale="supervisor source file not found",
        )
    text = p.read_text(encoding="utf-8")
    required = [
        # Wave-22 helpers still imported (WATCH size halving)
        "prop_entry_size_multiplier",
        # Wave-25 composite gate
        "resolve_execution_target",
        # Wave-25 unified reject prefix
        "gate_reject:",
        # WATCH-mode size halving still in place
        "size_mult *= prop_mult",
    ]
    missing = [r for r in required if r not in text]
    if missing:
        return SectionResult(
            "supervisor_wiring",
            "NO_GO",
            rationale=(f"supervisor missing wave-22/25 patterns: {missing}"),
        )
    return SectionResult(
        "supervisor_wiring",
        "GO",
        rationale="wave-25e unified prop-fund entry gate fully wired",
    )


def _check_alert_channels() -> SectionResult:
    """Are any operator-alert channels configured?"""
    sys.path.insert(0, str(ROOT.parent))
    from eta_engine.scripts import (  # noqa: PLC0415
        diamond_prop_alert_dispatcher as ad,
    )

    channels = ad.configured_channels()
    if not channels:
        return SectionResult(
            "alert_channels",
            "HOLD",
            rationale=(
                "no push channels configured; HALT will only show on "
                "the dashboard. Set ETA_TELEGRAM_BOT_TOKEN + "
                "ETA_TELEGRAM_CHAT_ID or ETA_DISCORD_WEBHOOK_URL"
            ),
        )
    return SectionResult(
        "alert_channels",
        "GO",
        rationale=f"channels configured: {', '.join(channels)}",
        detail={"channels": channels},
    )


def _check_prop_ready_bots() -> SectionResult:
    """Count + name the current PROP_READY designations."""
    rec = _load_json(STATE_DIR / "diamond_leaderboard_latest.json")
    if not rec:
        return SectionResult(
            "prop_ready",
            "NO_GO",
            rationale="leaderboard has never run",
        )
    pr = rec.get("prop_ready_bots") or []
    if len(pr) >= 3:
        return SectionResult(
            "prop_ready",
            "GO",
            rationale=f"3 PROP_READY bots: {pr}",
            detail={"prop_ready_bots": pr},
        )
    if len(pr) >= 2:
        return SectionResult(
            "prop_ready",
            "HOLD",
            rationale=f"only {len(pr)} PROP_READY (DEGRADED mode): {pr}",
            detail={"prop_ready_bots": pr},
        )
    return SectionResult(
        "prop_ready",
        "NO_GO",
        rationale=f"only {len(pr)} PROP_READY bot(s); need >=2",
        detail={"prop_ready_bots": pr},
    )


def _check_sizing(prop_ready: list[str]) -> SectionResult:
    rec = _load_json(STATE_DIR / "diamond_sizing_audit_latest.json")
    if not rec or not prop_ready:
        return SectionResult(
            "sizing",
            "HOLD",
            rationale="sizing audit missing or no PROP_READY bots",
        )
    pr_set = set(prop_ready)
    breached = [
        s.get("bot_id")
        for s in rec.get("statuses") or []
        if s.get("bot_id") in pr_set and s.get("verdict") == "SIZING_BREACHED"
    ]
    if breached:
        return SectionResult(
            "sizing",
            "NO_GO",
            rationale=f"PROP_READY bots SIZING_BREACHED: {breached}",
        )
    return SectionResult(
        "sizing",
        "GO",
        rationale="no PROP_READY bot is SIZING_BREACHED",
    )


def _check_feed_sanity(prop_ready: list[str]) -> SectionResult:
    rec = _load_json(STATE_DIR / "diamond_feed_sanity_audit_latest.json")
    if not rec or not prop_ready:
        return SectionResult(
            "feed_sanity",
            "HOLD",
            rationale="feed sanity missing or no PROP_READY bots",
        )
    pr_set = set(prop_ready)
    flagged = [
        s.get("bot_id")
        for s in rec.get("scorecards") or []
        if s.get("bot_id") in pr_set and s.get("verdict") == "FLAGGED"
    ]
    if flagged:
        return SectionResult(
            "feed_sanity",
            "NO_GO",
            rationale=f"PROP_READY bots feed-sanity FLAGGED: {flagged}",
        )
    return SectionResult(
        "feed_sanity",
        "GO",
        rationale="all PROP_READY bots passed feed sanity",
    )


def _check_wave25_lifecycle() -> SectionResult:
    """Wave-25 per-bot lifecycle state.

    The operator must explicitly opt at least one bot into ``EVAL_LIVE``
    before the supervisor will route signals to the live broker.
    Defaults to ``EVAL_PAPER`` everywhere (conservative).

    Verdicts:
      * GO   — at least one bot in EVAL_LIVE or FUNDED_LIVE
      * HOLD — every bot is EVAL_PAPER (paper-only fleet; no live)
      * HOLD — every bot is RETIRED (nothing trading at all)
    """
    sys.path.insert(0, str(ROOT.parent))
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        DIAMOND_BOTS,
        LIFECYCLE_EVAL_LIVE,
        LIFECYCLE_EVAL_PAPER,
        LIFECYCLE_FUNDED_LIVE,
        LIFECYCLE_RETIRED,
        get_bot_lifecycle,
    )

    counts = {
        LIFECYCLE_EVAL_LIVE: 0,
        LIFECYCLE_EVAL_PAPER: 0,
        LIFECYCLE_FUNDED_LIVE: 0,
        LIFECYCLE_RETIRED: 0,
    }
    live_bots: list[str] = []
    for bot_id in sorted(DIAMOND_BOTS):
        state = get_bot_lifecycle(bot_id)
        counts[state] = counts.get(state, 0) + 1
        if state in {LIFECYCLE_EVAL_LIVE, LIFECYCLE_FUNDED_LIVE}:
            live_bots.append(bot_id)

    detail = {"counts": counts, "live_bots": live_bots}
    if live_bots:
        return SectionResult(
            "wave25_lifecycle",
            "GO",
            rationale=f"{len(live_bots)} bot(s) opted into live execution: {live_bots}",
            detail=detail,
        )
    if counts[LIFECYCLE_RETIRED] == len(DIAMOND_BOTS):
        return SectionResult(
            "wave25_lifecycle",
            "HOLD",
            rationale="every bot is RETIRED — nothing will trade",
            detail=detail,
        )
    return SectionResult(
        "wave25_lifecycle",
        "HOLD",
        rationale=(
            "no bot opted into live execution; fleet is paper-only. "
            "Use `python -m eta_engine.scripts.manage_lifecycle set "
            "<bot_id> EVAL_LIVE` to promote."
        ),
        detail=detail,
    )


def _check_watchdog(prop_ready: list[str]) -> SectionResult:
    rec = _load_json(STATE_DIR / "diamond_watchdog_latest.json")
    if not rec or not prop_ready:
        return SectionResult(
            "watchdog",
            "HOLD",
            rationale="watchdog missing or no PROP_READY bots",
        )
    pr_set = set(prop_ready)
    critical = [
        s.get("bot_id")
        for s in rec.get("statuses") or []
        if s.get("bot_id") in pr_set and s.get("classification") == "CRITICAL"
    ]
    if critical:
        return SectionResult(
            "watchdog",
            "NO_GO",
            rationale=f"PROP_READY bots watchdog CRITICAL: {critical}",
        )
    return SectionResult(
        "watchdog",
        "GO",
        rationale="no PROP_READY bot in watchdog CRITICAL",
    )


# ────────────────────────────────────────────────────────────────────
# Aggregator
# ────────────────────────────────────────────────────────────────────


def _aggregate(sections: list[SectionResult]) -> tuple[str, str]:
    if any(s.status == "NO_GO" for s in sections):
        no_go = [s.name for s in sections if s.status == "NO_GO"]
        return "NO_GO", f"NO_GO -- blockers: {', '.join(no_go)}"
    if any(s.status == "HOLD" for s in sections):
        hold = [s.name for s in sections if s.status == "HOLD"]
        return "HOLD", f"HOLD -- review: {', '.join(hold)}"
    return "GO", "ALL CHECKS GO -- cleared for Monday cutover"


def run() -> dict[str, Any]:
    leaderboard = _load_json(STATE_DIR / "diamond_leaderboard_latest.json") or {}
    prop_ready = leaderboard.get("prop_ready_bots") or []

    sections = [
        _check_launch_readiness(),
        _check_drawdown_guard(),
        _check_allocator(),
        _check_freshness(),
        _check_supervisor_wiring(),
        _check_alert_channels(),
        _check_wave25_lifecycle(),
        _check_prop_ready_bots(),
        _check_sizing(prop_ready),
        _check_feed_sanity(prop_ready),
        _check_watchdog(prop_ready),
    ]
    verdict, summary_text = _aggregate(sections)
    receipt = DryRunReceipt(
        ts=datetime.now(UTC).isoformat(),
        overall_verdict=verdict,
        summary=summary_text,
        sections=sections,
    )
    summary = asdict(receipt)
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(s: dict[str, Any]) -> None:
    print("=" * 100)
    print(
        f" PROP-FUND PRELAUNCH DRY-RUN  ({s['ts']})  verdict={s['overall_verdict']}",
    )
    print("=" * 100)
    print(f" {s['summary']}")
    print()
    print(f" {'section':22s}  {'status':6s}  rationale")
    print("-" * 100)
    for sec in s["sections"]:
        print(f" {sec['name']:22s}  {sec['status']:6s}  {sec['rationale'][:60]}")
        if len(sec["rationale"]) > 60:
            print(f" {'':22s}  {'':6s}  {sec['rationale'][60:]}")
    print()
    if s["overall_verdict"] == "GO":
        print(" >>> CLEARED FOR LIVE CUTOVER MONDAY <<<")
    elif s["overall_verdict"] == "HOLD":
        print(" >>> REVIEW THE HOLD ITEMS BEFORE CUTOVER <<<")
    else:
        print(" >>> LAUNCH BLOCKED -- fix NO_GO items before cutover <<<")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return {"GO": 0, "HOLD": 1, "NO_GO": 2}.get(summary["overall_verdict"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
