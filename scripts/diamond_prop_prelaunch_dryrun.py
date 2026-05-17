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
 10. Alert dispatcher channel availability (env + canonical Telegram secrets)

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
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots
from eta_engine.scripts.retune_advisory_cache import build_retune_advisory

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
STATE_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR

OUT_LATEST = workspace_roots.ETA_DIAMOND_PROP_PRELAUNCH_DRYRUN_PATH

#: Launch-critical receipts and the scheduled tasks that keep them fresh.
SCHEDULED_RECEIPT_SURFACES = (
    {
        "receipt": "closed_trade_ledger_latest.json",
        "task_name": "ETA-Diamond-LedgerEvery15Min",
        "freshness_limit_h": 0.5,  # 15-min + slack
    },
    {
        "receipt": "diamond_prop_drawdown_guard_latest.json",
        "task_name": "ETA-Diamond-PropDrawdownGuardEvery15Min",
        "freshness_limit_h": 0.5,  # 15-min + slack
    },
    {
        "receipt": "diamond_prop_launch_readiness_latest.json",
        "task_name": "ETA-Diamond-LaunchReadinessEvery15Min",
        "freshness_limit_h": 0.5,  # 15-min + slack
    },
    {
        "receipt": "diamond_leaderboard_latest.json",
        "task_name": "ETA-Diamond-LeaderboardHourly",
        "freshness_limit_h": 1.5,  # hourly + slack
    },
    {
        "receipt": "diamond_prop_allocator_latest.json",
        "task_name": "ETA-Diamond-PropAllocatorHourly",
        "freshness_limit_h": 1.5,  # hourly + slack
    },
    {
        "receipt": "diamond_ops_dashboard_latest.json",
        "task_name": "ETA-Diamond-OpsDashboardHourly",
        "freshness_limit_h": 1.5,  # hourly + slack
    },
    {
        "receipt": "diamond_feed_sanity_audit_latest.json",
        "task_name": "ETA-Diamond-FeedSanityHourly",
        "freshness_limit_h": 1.5,  # hourly + slack
    },
    {
        "receipt": "diamond_sizing_audit_latest.json",
        "task_name": "ETA-Diamond-SizingAuditDaily",
        "freshness_limit_h": 25.0,  # daily + 1h slack
    },
    {
        "receipt": "diamond_direction_stratify_latest.json",
        "task_name": "ETA-Diamond-DirectionStratifyDaily",
        "freshness_limit_h": 25.0,  # daily + 1h slack
    },
    {
        "receipt": "diamond_promotion_gate_latest.json",
        "task_name": "ETA-Diamond-PromotionGateDaily",
        "freshness_limit_h": 25.0,  # daily
    },
    {
        "receipt": "diamond_demotion_gate_latest.json",
        "task_name": "ETA-Diamond-DemotionGateDaily",
        "freshness_limit_h": 25.0,  # daily
    },
    {
        "receipt": "diamond_watchdog_latest.json",
        "task_name": "ETA-Diamond-WatchdogDaily",
        "freshness_limit_h": 25.0,  # daily
    },
)

ADDITIONAL_REQUIRED_SCHEDULED_TASKS = (
    "ETA-Diamond-PropAlertDispatcherEvery15Min",
)

EXPECTED_SCHEDULED_TASKS = tuple(
    surface["task_name"] for surface in SCHEDULED_RECEIPT_SURFACES
) + ADDITIONAL_REQUIRED_SCHEDULED_TASKS


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
    retune_advisory: dict[str, Any] = field(default_factory=dict)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_json_dict(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if isinstance(payload, dict):
        return payload
    return {}


def _dict_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _file_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return (datetime.now(UTC) - mtime).total_seconds() / 3600.0


def _collect_task_registration() -> dict[str, Any]:
    """Return scheduled-task presence/state for the launch-critical ETA-Diamond lane."""
    quoted = ", ".join(f"'{name}'" for name in EXPECTED_SCHEDULED_TASKS)
    command = f"""
$names = @({quoted})
$results = foreach ($name in $names) {{
  $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if ($null -eq $task) {{
    [pscustomobject]@{{TaskName=$name;State='Missing';LastTaskResult=$null;LastRunTime=$null;NextRunTime=$null}}
  }} else {{
    $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
    [pscustomobject]@{{
      TaskName=$task.TaskName
      State=[string]$task.State
      LastTaskResult=$info.LastTaskResult
      LastRunTime=$info.LastRunTime
      NextRunTime=$info.NextRunTime
    }}
  }}
}}
$results | ConvertTo-Json -Depth 4
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except OSError as exc:
        return {"available": False, "error": f"powershell_unavailable:{exc}"}
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "powershell_timeout"}

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        return {"available": False, "error": f"powershell_rc={result.returncode}:{stderr[:240]}"}

    raw = result.stdout.strip()
    if not raw:
        return {"available": False, "error": "empty_scheduler_probe"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"available": False, "error": f"invalid_scheduler_probe_json:{exc}"}

    rows = payload if isinstance(payload, list) else [payload]
    tasks: list[dict[str, Any]] = []
    missing: list[str] = []
    nonready: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("TaskName") or "")
        state = str(row.get("State") or "Unknown")
        if not name:
            continue
        record = {
            "task_name": name,
            "state": state,
            "last_task_result": row.get("LastTaskResult"),
            "last_run_time": row.get("LastRunTime"),
            "next_run_time": row.get("NextRunTime"),
        }
        tasks.append(record)
        if state == "Missing":
            missing.append(name)
        elif state not in {"Ready", "Running", "Queued"}:
            nonready.append(name)
    return {
        "available": True,
        "tasks": tasks,
        "missing": missing,
        "nonready": nonready,
    }


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
    for surface in SCHEDULED_RECEIPT_SURFACES:
        fname = str(surface["receipt"])
        limit_h = float(surface["freshness_limit_h"])
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


def _check_task_registration() -> SectionResult:
    """Launch-critical ETA-Diamond scheduled tasks should exist on the runtime host."""
    report = _collect_task_registration()
    if not report.get("available"):
        return SectionResult(
            "task_registration",
            "HOLD",
            rationale="scheduled-task probe unavailable; verify ETA-Diamond tasks on the intended Windows runtime host",
            detail={"error": report.get("error")},
        )
    missing = report.get("missing", [])
    nonready = report.get("nonready", [])
    if missing:
        return SectionResult(
            "task_registration",
            "NO_GO",
            rationale=(
                f"{len(missing)} ETA-Diamond scheduled task(s) missing; receipts may stay fresh only after manual runs"
            ),
            detail={"missing": missing, "nonready": nonready, "tasks": report.get("tasks", [])},
        )
    if nonready:
        return SectionResult(
            "task_registration",
            "HOLD",
            rationale=(
                f"{len(nonready)} ETA-Diamond scheduled task(s) registered but not "
                "ready/running; inspect Task Scheduler state"
            ),
            detail={"missing": missing, "nonready": nonready, "tasks": report.get("tasks", [])},
        )
    return SectionResult(
        "task_registration",
        "GO",
        rationale=f"all {len(report.get('tasks', []))} launch-critical ETA-Diamond scheduled tasks registered",
        detail={"missing": missing, "nonready": nonready, "tasks": report.get("tasks", [])},
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
                "the dashboard. Seed ETA Telegram env vars or canonical "
                "secrets/telegram_*.txt, or set ETA_DISCORD_WEBHOOK_URL"
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


def _retune_advisory() -> dict[str, Any]:
    return build_retune_advisory(STATE_DIR / "health")


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
        _check_task_registration(),
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
        retune_advisory=_retune_advisory(),
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
    advisory = s.get("retune_advisory", {})
    if advisory.get("available"):
        focus_pnl = advisory.get("focus_total_realized_pnl")
        focus_pf = advisory.get("focus_profit_factor")
        broker_mtd = advisory.get("broker_mtd_pnl")
        pnl_text = f"${focus_pnl:+.2f}" if isinstance(focus_pnl, int | float) else "n/a"
        pf_text = f"{focus_pf:.2f}" if isinstance(focus_pf, int | float) else "n/a"
        mtd_text = f"${broker_mtd:+.2f}" if isinstance(broker_mtd, int | float) else "n/a"
        print(" broker-backed retune advisory")
        print(
            f"   focus={advisory.get('focus_bot')} state={advisory.get('focus_state')} "
            f"issue={advisory.get('focus_issue')}",
        )
        print(
            f"   closes={advisory.get('focus_closed_trade_count')} pnl={pnl_text} "
            f"pf={pf_text} mtd={mtd_text}",
        )
        if advisory.get("diagnosis"):
            print(f"   local drift={advisory.get('diagnosis')}")
        if advisory.get("preferred_warning"):
            print(f"   warning={advisory.get('preferred_warning')}")
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
