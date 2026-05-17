"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_prop_launch_readiness
======================================================================
Pre-launch GO/NO-GO gate for the 2026-07-08 prop-fund cutover.

Operator goal (updated 2026-05-14)
--------------------------
2026-07-08: earliest operator-approved live-capital date for the $50K
prop firm. Until then, this gate runs every cron cycle and tells the
operator exactly what's blocking a clean launch while the fleet remains
paper-live only.

NO single signal is enough — a clean launch requires every gate to
say GO simultaneously.  This script aggregates all of them.

Pre-launch checklist (each must be GO):
---------------------------------------

  R1_PROP_READY_DESIGNATED   At least 2 of 3 PROP_READY bots designated
                              by the leaderboard
  R2_DRAWDOWN_OK              diamond_prop_drawdown_guard signal != HALT
  R3_FEED_SANITY_CLEAN        No PROP_READY bot in feed_sanity FLAGGED
                              (STUCK_PRICE / ZERO_PNL / MISSING fields)
  R4_SIZING_NOT_BREACHED      No PROP_READY bot in sizing_audit BREACHED
  R5_WATCHDOG_NOT_CRITICAL    No PROP_READY bot in watchdog CRITICAL
  R6_ALLOCATOR_RECEIPT_FRESH  Allocator receipt < AGE_LIMIT_HOURS old
                              (proves the cron is firing)
  R7_LEDGER_FRESH             closed_trade_ledger < AGE_LIMIT_HOURS old
                              (proves the data feed is alive)
  R8_BROKER_TRUTH_CONFIRMED   broker-truth retune focus is not negative-edge
                              and does not still need broker-proof closes

Verdict bands:
  GO            All 7 gates pass — safe to cut over Monday
  HOLD          1+ soft warnings; review before commit
  NO_GO         Any hard gate fails — Monday launch blocked

Output
------
- stdout: per-gate status table with rationale + countdown to Monday
- ``var/eta_engine/state/diamond_prop_launch_readiness_latest.json``
- exit code: 0=GO, 1=HOLD, 2=NO_GO

Run
---
::

    python -m eta_engine.scripts.diamond_prop_launch_readiness
    python -m eta_engine.scripts.diamond_prop_launch_readiness --json
    python -m eta_engine.scripts.diamond_prop_launch_readiness --launch-date 2026-07-08
"""

from __future__ import annotations

# ruff: noqa: N802, PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

LEADERBOARD_PATH = workspace_roots.ETA_DIAMOND_LEADERBOARD_PATH
PROP_ALLOCATOR_PATH = workspace_roots.ETA_DIAMOND_PROP_ALLOCATOR_PATH
DRAWDOWN_GUARD_PATH = workspace_roots.ETA_DIAMOND_PROP_DRAWDOWN_GUARD_PATH
SIZING_AUDIT_PATH = workspace_roots.ETA_DIAMOND_SIZING_AUDIT_PATH
WATCHDOG_PATH = workspace_roots.ETA_DIAMOND_WATCHDOG_PATH
FEED_SANITY_PATH = workspace_roots.ETA_DIAMOND_FEED_SANITY_AUDIT_PATH
LEDGER_PATH = workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH
RETUNE_STATUS_PATH = workspace_roots.ETA_DIAMOND_RETUNE_STATUS_PATH
OUT_LATEST = workspace_roots.ETA_DIAMOND_PROP_LAUNCH_READINESS_PATH

#: Default operator-set launch target.
#: Operator directive (2026-05-14): no live capital before 2026-07-08.
#: Paper-live readiness and prop-firm dry-runs can proceed before then, but
#: EVAL_LIVE/FUNDED_LIVE routing remains calendar-held until this date floor.
DEFAULT_LAUNCH_DATE = "2026-07-08"

#: Receipts older than this are stale (cron isn't firing).
AGE_LIMIT_HOURS = 2.0

#: Minimum number of PROP_READY bots for the launch to be viable.
MIN_PROP_READY_BOTS = 2


@dataclass
class GateResult:
    name: str
    status: str  # "GO" / "HOLD" / "NO_GO"
    rationale: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class LaunchReadinessReceipt:
    ts: str
    launch_date: str
    days_until_launch: int
    overall_verdict: str  # "GO" / "HOLD" / "NO_GO"
    summary: str
    gates: list[GateResult] = field(default_factory=list)


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


def _parse_launch_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).date()


def _check_R0_live_capital_calendar(launch: date, today: date | None = None) -> GateResult:
    """Calendar gate: before launch day, stay paper-live only."""
    observed = today or datetime.now(UTC).date()
    days_until = (launch - observed).days
    if days_until > 0:
        return GateResult(
            "R0_LIVE_CAPITAL_CALENDAR",
            "HOLD",
            rationale=(
                f"live capital is calendar-held until {launch.isoformat()} "
                f"({days_until}d); keep paper_live/paper_sim execution only"
            ),
            detail={
                "today": observed.isoformat(),
                "not_before": launch.isoformat(),
                "days_until": days_until,
                "paper_live_required": True,
            },
        )
    return GateResult(
        "R0_LIVE_CAPITAL_CALENDAR",
        "GO",
        rationale=f"calendar date reached for live-capital consideration: {launch.isoformat()}",
        detail={
            "today": observed.isoformat(),
            "not_before": launch.isoformat(),
            "days_until": max(days_until, 0),
            "paper_live_required": False,
        },
    )


# ────────────────────────────────────────────────────────────────────
# Individual gate checks
# ────────────────────────────────────────────────────────────────────


def _check_R1_prop_ready_designated(leaderboard: dict | None) -> GateResult:
    if not leaderboard:
        return GateResult(
            "R1_PROP_READY_DESIGNATED",
            "NO_GO",
            rationale="leaderboard receipt missing — cron not firing or empty",
        )
    pr = leaderboard.get("prop_ready_bots") or []
    n = len(pr)
    if n >= 3:
        return GateResult(
            "R1_PROP_READY_DESIGNATED",
            "GO",
            rationale=f"{n} PROP_READY bots designated: {pr}",
            detail={"prop_ready_bots": pr, "n": n},
        )
    if n >= MIN_PROP_READY_BOTS:
        return GateResult(
            "R1_PROP_READY_DESIGNATED",
            "HOLD",
            rationale=(
                f"only {n} PROP_READY bots ({pr}); below the 3-bot "
                "DOMINANT/BALANCED design but launch still possible "
                "in DEGRADED mode"
            ),
            detail={"prop_ready_bots": pr, "n": n},
        )
    return GateResult(
        "R1_PROP_READY_DESIGNATED",
        "NO_GO",
        rationale=(f"only {n} PROP_READY bot(s); need >= {MIN_PROP_READY_BOTS} before going live"),
        detail={"prop_ready_bots": pr, "n": n},
    )


def _check_R2_drawdown(guard: dict | None) -> GateResult:
    if not guard:
        return GateResult(
            "R2_DRAWDOWN_OK",
            "NO_GO",
            rationale="drawdown guard receipt missing — cron not firing",
        )
    sig = guard.get("signal", "UNKNOWN")
    detail = _drawdown_guard_detail(guard)
    if sig == "OK":
        return GateResult(
            "R2_DRAWDOWN_OK",
            "GO",
            rationale="prop drawdown guard: OK across all rules",
            detail=detail,
        )
    if sig == "WATCH":
        return GateResult(
            "R2_DRAWDOWN_OK",
            "HOLD",
            rationale=f"prop drawdown guard: WATCH — {guard.get('rationale', '')}",
            detail=detail,
        )
    return GateResult(
        "R2_DRAWDOWN_OK",
        "NO_GO",
        rationale=f"prop drawdown guard: HALT — {guard.get('rationale', '')}",
        detail=detail,
    )


def _drawdown_check_detail(check: object) -> dict[str, Any] | None:
    """Return operator-useful fields from a drawdown guard sub-check."""
    if not isinstance(check, dict):
        return None
    keys = (
        "name",
        "status",
        "rationale",
        "limit_usd",
        "used_usd",
        "buffer_usd",
        "buffer_pct_of_limit",
    )
    return {k: check.get(k) for k in keys if k in check}


def _drawdown_guard_detail(guard: dict[str, Any]) -> dict[str, Any]:
    """Preserve enough drawdown context for dashboards and eta_status."""
    detail: dict[str, Any] = {
        "signal": guard.get("signal", "UNKNOWN"),
        "guard_rationale": guard.get("rationale", ""),
        "receipt_ts": guard.get("ts"),
        "prop_ready_bots": guard.get("prop_ready_bots") or [],
        "daily_pnl_usd": guard.get("daily_pnl_usd"),
        "total_pnl_usd": guard.get("total_pnl_usd"),
        "consistency_ratio": guard.get("consistency_ratio"),
    }
    for key in ("daily_dd_check", "static_dd_check", "consistency_check"):
        check_detail = _drawdown_check_detail(guard.get(key))
        if check_detail is not None:
            detail[key] = check_detail
    return detail


def _check_R3_feed_sanity(feed: dict | None, prop_ready: set[str]) -> GateResult:
    if not feed:
        return GateResult(
            "R3_FEED_SANITY_CLEAN",
            "NO_GO",
            rationale="feed sanity audit receipt missing",
        )
    flagged = []
    for sc in feed.get("scorecards") or []:
        bid = sc.get("bot_id")
        if bid in prop_ready and sc.get("verdict") == "FLAGGED":
            flagged.append({"bot_id": bid, "flags": sc.get("flags", [])})
    if not flagged:
        return GateResult(
            "R3_FEED_SANITY_CLEAN",
            "GO",
            rationale=(f"all {len(prop_ready)} PROP_READY bots passed feed sanity"),
            detail={"prop_ready_count": len(prop_ready)},
        )
    return GateResult(
        "R3_FEED_SANITY_CLEAN",
        "NO_GO",
        rationale=(
            f"{len(flagged)} PROP_READY bot(s) flagged on feed sanity: "
            f"{[f['bot_id'] for f in flagged]} — data quality must be "
            "fixed before going live"
        ),
        detail={"flagged": flagged},
    )


def _check_R4_sizing(sizing: dict | None, prop_ready: set[str]) -> GateResult:
    if not sizing:
        return GateResult(
            "R4_SIZING_NOT_BREACHED",
            "NO_GO",
            rationale="sizing audit receipt missing",
        )
    breached = []
    for sc in sizing.get("statuses") or []:
        bid = sc.get("bot_id")
        if bid in prop_ready and sc.get("verdict") == "SIZING_BREACHED":
            breached.append(bid)
    if not breached:
        return GateResult(
            "R4_SIZING_NOT_BREACHED",
            "GO",
            rationale="no PROP_READY bot has sizing BREACHED",
        )
    return GateResult(
        "R4_SIZING_NOT_BREACHED",
        "NO_GO",
        rationale=(
            f"sizing BREACHED on PROP_READY bots: {breached} — single "
            "stopout breaches the floor; cut risk_per_trade_pct first"
        ),
        detail={"breached_bots": breached},
    )


def _check_R5_watchdog(watchdog: dict | None, prop_ready: set[str]) -> GateResult:
    if not watchdog:
        return GateResult(
            "R5_WATCHDOG_NOT_CRITICAL",
            "NO_GO",
            rationale="watchdog receipt missing",
        )
    critical = []
    warn = []
    for st in watchdog.get("statuses") or []:
        bid = st.get("bot_id")
        if bid not in prop_ready:
            continue
        cls = st.get("classification", "INCONCLUSIVE")
        if cls == "CRITICAL":
            critical.append(bid)
        elif cls == "WARN":
            warn.append(bid)
    if critical:
        return GateResult(
            "R5_WATCHDOG_NOT_CRITICAL",
            "NO_GO",
            rationale=(
                f"watchdog CRITICAL on PROP_READY bots: {critical} — either USD floor breached or R-edge decayed"
            ),
            detail={"critical": critical, "warn": warn},
        )
    if warn:
        return GateResult(
            "R5_WATCHDOG_NOT_CRITICAL",
            "HOLD",
            rationale=(f"watchdog WARN on PROP_READY bots: {warn} — within 20% of floor; review before going live"),
            detail={"warn": warn},
        )
    return GateResult(
        "R5_WATCHDOG_NOT_CRITICAL",
        "GO",
        rationale="no PROP_READY bot in watchdog CRITICAL/WARN bands",
    )


def _check_R6_allocator_fresh(allocator_path: Path) -> GateResult:
    age = _file_age_hours(allocator_path)
    if age is None:
        return GateResult(
            "R6_ALLOCATOR_RECEIPT_FRESH",
            "NO_GO",
            rationale="allocator receipt missing — hourly cron never fired",
        )
    if age > AGE_LIMIT_HOURS:
        return GateResult(
            "R6_ALLOCATOR_RECEIPT_FRESH",
            "NO_GO",
            rationale=(f"allocator receipt {age:.1f}h old > {AGE_LIMIT_HOURS}h limit — cron has stalled; investigate"),
            detail={"age_hours": round(age, 2)},
        )
    return GateResult(
        "R6_ALLOCATOR_RECEIPT_FRESH",
        "GO",
        rationale=f"allocator receipt fresh ({age:.2f}h old)",
        detail={"age_hours": round(age, 2)},
    )


def _check_R7_ledger_fresh(ledger_path: Path) -> GateResult:
    age = _file_age_hours(ledger_path)
    if age is None:
        return GateResult(
            "R7_LEDGER_FRESH",
            "NO_GO",
            rationale=("closed_trade_ledger receipt missing — 15-min cron never fired; supervisor has no data"),
        )
    # Ledger should be very fresh — 15-min cron means < 0.5h is normal
    if age > 0.5:
        return GateResult(
            "R7_LEDGER_FRESH",
            "HOLD",
            rationale=(f"ledger {age:.1f}h old; 15-min cron should keep < 0.5h"),
            detail={"age_hours": round(age, 2)},
        )
    return GateResult(
        "R7_LEDGER_FRESH",
        "GO",
        rationale=f"ledger fresh ({age:.2f}h old)",
        detail={"age_hours": round(age, 2)},
    )


def _check_R8_broker_truth(retune_status: dict[str, Any] | None) -> GateResult:
    """Require broker-truth focus to be clean before live cutover."""
    if not retune_status:
        return GateResult(
            "R8_BROKER_TRUTH_CONFIRMED",
            "NO_GO",
            rationale="diamond retune status missing — broker-truth launch surface unavailable",
        )

    summary = retune_status.get("summary") if isinstance(retune_status.get("summary"), dict) else {}
    focus_bot = str(summary.get("broker_truth_focus_bot_id") or retune_status.get("focus_bot") or "").strip()
    edge_status = str(summary.get("broker_truth_focus_edge_status") or "").strip()
    closes = int(summary.get("broker_truth_focus_closed_trade_count") or retune_status.get("focus_closed_trade_count") or 0)
    required = int(summary.get("broker_truth_focus_required_closed_trade_count") or 100)
    remaining = int(
        summary.get("broker_truth_focus_remaining_closed_trade_count")
        or max(required - closes, 0)
    )
    total_realized_pnl = float(
        summary.get("broker_truth_focus_total_realized_pnl") or retune_status.get("focus_total_realized_pnl") or 0.0
    )
    profit_factor = float(
        summary.get("broker_truth_focus_profit_factor") or retune_status.get("focus_profit_factor") or 0.0
    )
    active_experiment = summary.get("broker_truth_focus_active_experiment")
    if not isinstance(active_experiment, dict):
        active_experiment = retune_status.get("focus_active_experiment")
    active_experiment = active_experiment if isinstance(active_experiment, dict) else {}
    detail = {
        "focus_bot": focus_bot,
        "edge_status": edge_status or "unknown",
        "closed_trade_count": closes,
        "required_closed_trade_count": required,
        "remaining_closed_trade_count": remaining,
        "total_realized_pnl": round(total_realized_pnl, 2),
        "profit_factor": round(profit_factor, 4),
        "next_action": str(summary.get("broker_truth_focus_next_action") or retune_status.get("focus_next_action") or ""),
    }
    if active_experiment:
        detail["active_experiment"] = active_experiment

    if not focus_bot or not edge_status:
        return GateResult(
            "R8_BROKER_TRUTH_CONFIRMED",
            "HOLD",
            rationale="broker-truth focus is missing key fields — review retune status before launch",
            detail=detail,
        )
    if edge_status == "sample_met_negative_edge":
        return GateResult(
            "R8_BROKER_TRUTH_CONFIRMED",
            "NO_GO",
            rationale=(
                f"broker truth negative on {focus_bot}: sample met ({closes}/{required}) "
                f"but PnL=${total_realized_pnl:,.2f} PF={profit_factor:.2f}"
            ),
            detail=detail,
        )
    if edge_status in {"needs_more_broker_closes", "missing_closed_trade_ledger"}:
        return GateResult(
            "R8_BROKER_TRUTH_CONFIRMED",
            "HOLD",
            rationale=(
                f"broker proof incomplete for {focus_bot}: {remaining} more closes needed "
                f"({closes}/{required}) before launch confidence"
            ),
            detail=detail,
        )
    if edge_status == "broker_edge_ready":
        if active_experiment.get("partial_profit_enabled") is True:
            return GateResult(
                "R8_BROKER_TRUTH_CONFIRMED",
                "HOLD",
                rationale=(
                    f"broker sample is positive for {focus_bot}, but the active experiment still has "
                    "partial_profit_enabled=true; verify unsliced USD truth before launch"
                ),
                detail=detail,
            )
        return GateResult(
            "R8_BROKER_TRUTH_CONFIRMED",
            "GO",
            rationale=(
                f"broker truth clean for {focus_bot}: positive sample ({closes}/{required}) "
                f"PnL=${total_realized_pnl:,.2f} PF={profit_factor:.2f}"
            ),
            detail=detail,
        )
    return GateResult(
        "R8_BROKER_TRUTH_CONFIRMED",
        "HOLD",
        rationale=f"broker truth for {focus_bot or 'focus bot'} is inconclusive ({edge_status}); review before launch",
        detail=detail,
    )


# ────────────────────────────────────────────────────────────────────
# Aggregator
# ────────────────────────────────────────────────────────────────────


def _aggregate_verdict(gates: list[GateResult]) -> tuple[str, str]:
    """Worst-of: any NO_GO -> NO_GO; else any HOLD -> HOLD; else GO."""
    if any(g.status == "NO_GO" for g in gates):
        no_go = [g.name for g in gates if g.status == "NO_GO"]
        return "NO_GO", (f"NO_GO — {len(no_go)} hard gate(s) failing: {', '.join(no_go)}")
    if any(g.status == "HOLD" for g in gates):
        hold = [g.name for g in gates if g.status == "HOLD"]
        return "HOLD", (f"HOLD — {len(hold)} soft warning(s): {', '.join(hold)} — review before commit")
    return "GO", "ALL GATES GO — safe to cut over to live prop fund"


def run(launch_date_str: str = DEFAULT_LAUNCH_DATE) -> dict[str, Any]:
    leaderboard = _load_json(LEADERBOARD_PATH) or {}
    drawdown = _load_json(DRAWDOWN_GUARD_PATH)
    sizing = _load_json(SIZING_AUDIT_PATH)
    watchdog = _load_json(WATCHDOG_PATH)
    feed = _load_json(FEED_SANITY_PATH)
    retune_status = _load_json(RETUNE_STATUS_PATH)

    prop_ready = set(leaderboard.get("prop_ready_bots") or [])
    today = datetime.now(UTC).date()
    try:
        launch = _parse_launch_date(launch_date_str)
    except ValueError:
        launch = today + timedelta(days=7)
        launch_date_str = launch.isoformat()

    gates = [
        _check_R0_live_capital_calendar(launch, today=today),
        _check_R1_prop_ready_designated(leaderboard),
        _check_R2_drawdown(drawdown),
        _check_R3_feed_sanity(feed, prop_ready),
        _check_R4_sizing(sizing, prop_ready),
        _check_R5_watchdog(watchdog, prop_ready),
        _check_R6_allocator_fresh(PROP_ALLOCATOR_PATH),
        _check_R7_ledger_fresh(LEDGER_PATH),
        _check_R8_broker_truth(retune_status),
    ]

    verdict, summary = _aggregate_verdict(gates)
    days_until = (launch - today).days

    receipt = LaunchReadinessReceipt(
        ts=datetime.now(UTC).isoformat(),
        launch_date=launch_date_str,
        days_until_launch=days_until,
        overall_verdict=verdict,
        summary=summary,
        gates=gates,
    )
    summary_dict = asdict(receipt)
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary_dict, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary_dict


def _print(s: dict[str, Any]) -> None:
    print("=" * 100)
    print(
        f" PROP-FUND LAUNCH READINESS  ({s['ts']})  "
        f"verdict={s['overall_verdict']}  "
        f"days_until_{s['launch_date']}={s['days_until_launch']}",
    )
    print("=" * 100)
    print(f" {s['summary']}")
    print()
    print(f" {'gate':30s}  {'status':6s}  rationale")
    print("-" * 100)
    for g in s["gates"]:
        print(f" {g['name']:30s}  {g['status']:6s}  {g['rationale'][:55]}")
        if len(g["rationale"]) > 55:
            print(f" {'':30s}  {'':6s}  {g['rationale'][55:]}")
    print()
    if s["overall_verdict"] == "GO":
        print(
            f" >>> READY FOR LIVE CUTOVER on {s['launch_date']} <<<",
        )
    elif s["overall_verdict"] == "HOLD":
        print(
            " >>> Soft warnings — operator review needed before live cutover",
        )
    else:
        print(
            " >>> LAUNCH BLOCKED — fix the NO_GO gates before going live",
        )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--launch-date",
        type=str,
        default=DEFAULT_LAUNCH_DATE,
        help=f"Target launch date YYYY-MM-DD (default {DEFAULT_LAUNCH_DATE})",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run(launch_date_str=args.launch_date)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return {"GO": 0, "HOLD": 1, "NO_GO": 2}.get(summary["overall_verdict"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
