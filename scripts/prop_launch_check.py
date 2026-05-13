"""Operator CLI: one-command pre-launch verification for the prop-fund cutover.

Run this Sunday EOD (or anytime to see the current launch posture).
It aggregates every wave-22 + wave-24 + wave-25 surface into ONE
verdict + action list so the operator doesn't have to remember the
ten different scripts that each surface a slice of the truth.

Usage::

    python -m eta_engine.scripts.prop_launch_check
    python -m eta_engine.scripts.prop_launch_check --json
    python -m eta_engine.scripts.prop_launch_check --verbose

What it surfaces
----------------

  1. Overall verdict: ``GO`` / ``HOLD`` / ``NO_GO`` (from prelaunch dryrun)
  2. Wave-25 lifecycle table — which bots route to live vs paper
  3. Leaderboard top-5 with PROP_READY designation
  4. Drawdown guard signal + buffer state
  5. Alert channel configuration
  6. Cron task freshness (which receipts are stale)
  7. **Actionable next steps** — exactly what the operator needs to do
     to flip remaining HOLD/NO_GO items to GO.

Exit codes
----------
  0 = GO        (launch posture clear)
  1 = HOLD      (review needed; no hard blockers)
  2 = NO_GO     (hard blockers; do NOT launch)
"""
# ruff: noqa: T201
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def _print_section_header(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(1, 88 - len(title)))


def _check_dryrun() -> dict:
    """Run prelaunch dryrun and pull verdict + section list."""
    from eta_engine.scripts.diamond_prop_prelaunch_dryrun import run  # noqa: PLC0415

    return run()


def _check_lifecycle() -> dict:
    """Snapshot lifecycle table."""
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        DIAMOND_BOTS,
        LIFECYCLE_EVAL_LIVE,
        LIFECYCLE_EVAL_PAPER,
        LIFECYCLE_FUNDED_LIVE,
        LIFECYCLE_RETIRED,
        get_bot_lifecycle,
    )

    counts: dict[str, int] = {
        LIFECYCLE_EVAL_LIVE: 0,
        LIFECYCLE_EVAL_PAPER: 0,
        LIFECYCLE_FUNDED_LIVE: 0,
        LIFECYCLE_RETIRED: 0,
    }
    by_state: dict[str, list[str]] = {k: [] for k in counts}
    for bot_id in sorted(DIAMOND_BOTS):
        s = get_bot_lifecycle(bot_id)
        counts[s] = counts.get(s, 0) + 1
        by_state.setdefault(s, []).append(bot_id)
    return {"counts": counts, "by_state": by_state}


def _check_leaderboard() -> dict:
    """Top-5 leaderboard summary."""
    p = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_leaderboard_latest.json"
    if not p.exists():
        return {"missing": True}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"missing": True}
    return {
        "n_prop_ready": d.get("n_prop_ready", 0),
        "prop_ready_bots": d.get("prop_ready_bots", []),
        "top_5": d.get("leaderboard", [])[:5],
    }


def _check_alert_channels() -> dict:
    """Detect configured alert channels without sending."""
    import os  # noqa: PLC0415

    return {
        "telegram": bool(os.environ.get("ETA_TELEGRAM_BOT_TOKEN") and os.environ.get("ETA_TELEGRAM_CHAT_ID")),
        "discord": bool(os.environ.get("ETA_DISCORD_WEBHOOK_URL")),
        "generic": bool(os.environ.get("ETA_GENERIC_WEBHOOK_URL")),
    }


def _check_drawdown_guard() -> dict:
    """Latest drawdown guard signal + buffers."""
    p = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_prop_drawdown_guard_latest.json"
    if not p.exists():
        return {"missing": True}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"missing": True}
    return {
        "signal": d.get("signal", "?"),
        "daily_buffer_usd": (d.get("daily_dd_check") or {}).get("buffer_usd"),
        "static_buffer_usd": (d.get("static_dd_check") or {}).get("buffer_usd"),
        "consistency_status": (d.get("consistency_check") or {}).get("status"),
        "rationale": d.get("rationale", ""),
    }


def _check_launch_candidates() -> dict:
    """Scan every diamond bot for the launch-candidate profile.

    A bot is a defensible launch candidate when it meets ALL of:
      * n_trades >= 50 (sample sufficient)
      * cum_USD > 0 (real-money outcome profitable)
      * cum_R > 0 (R-edge backs up the USD)
      * win_rate >= 50%
      * NOT flagged ASYMMETRY_BUG by the qty audit (if available)

    Returns counts + the candidate list so the action-builder can
    surface "no candidates yet — don't launch" as a priority item.
    """
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        DIAMOND_BOTS,
    )
    from eta_engine.scripts.closed_trade_ledger import (  # noqa: PLC0415
        DEFAULT_PRODUCTION_DATA_SOURCES,
        load_close_records,
    )

    # Optional: read qty-asymmetry audit to mark BAD bots
    qa_path = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_qty_asymmetry_latest.json"
    asymmetric: set[str] = set()
    if qa_path.exists():
        try:
            d = json.loads(qa_path.read_text(encoding="utf-8"))
            asymmetric = {b for b in d.get("flagged_bots") or []}
        except (OSError, json.JSONDecodeError):
            pass

    candidates: list[dict] = []
    rejected: list[dict] = []
    for bot_id in sorted(DIAMOND_BOTS):
        rows = load_close_records(
            bot_filter=bot_id,
            data_sources=DEFAULT_PRODUCTION_DATA_SOURCES,
        )
        n = len(rows)
        if n < 5:
            continue
        cum_r = sum(float(r.get("realized_r", 0) or 0) for r in rows)
        wins = sum(1 for r in rows if float(r.get("realized_r", 0) or 0) > 0)
        wr = wins / n * 100 if n else 0
        pnls = []
        for r in rows:
            extra = r.get("extra") or {}
            usd = r.get("realized_pnl")
            if usd is None and isinstance(extra, dict):
                usd = extra.get("realized_pnl")
            try:
                if usd is not None:
                    pnls.append(float(usd))
            except (TypeError, ValueError):
                pass
        cum_usd = sum(pnls) if pnls else None
        is_asym = bot_id in asymmetric
        is_candidate = (
            n >= 50
            and (cum_usd or 0) > 0
            and cum_r > 0
            and wr >= 50.0
            and not is_asym
        )
        record = {
            "bot_id": bot_id,
            "n": n,
            "wr": round(wr, 1),
            "cum_r": round(cum_r, 2),
            "cum_usd": round(cum_usd, 2) if cum_usd is not None else None,
            "asymmetry_flagged": is_asym,
        }
        if is_candidate:
            candidates.append(record)
        else:
            rejected.append(record)
    return {
        "n_candidates": len(candidates),
        "candidates": candidates,
        "rejected_top5": sorted(rejected, key=lambda r: -(r["n"]))[:5],
    }


def _check_supervisor() -> dict:
    """Supervisor process health: heartbeat freshness + tick + mode + n_bots.

    Canonical path is
    ``var/eta_engine/state/jarvis_intel/supervisor/heartbeat.json``
    (also referenced by v25/v26/v27 policy modules). Receipt > 5 min
    old indicates the supervisor task is hung or stopped.
    """
    p = (
        WORKSPACE_ROOT
        / "var"
        / "eta_engine"
        / "state"
        / "jarvis_intel"
        / "supervisor"
        / "heartbeat.json"
    )
    if not p.exists():
        return {"missing": True, "path": str(p)}
    try:
        age_seconds = datetime.now(UTC).timestamp() - p.stat().st_mtime
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"missing": True, "path": str(p), "error": str(exc)}
    return {
        "missing": False,
        "age_seconds": round(age_seconds, 1),
        "tick_count": d.get("tick_count"),
        "mode": d.get("mode"),
        "feed": d.get("feed"),
        "n_bots": d.get("n_bots"),
        "live_money_enabled": d.get("live_money_enabled"),
    }


def _build_action_list(
    dryrun: dict,
    lifecycle: dict,
    leaderboard: dict,
    channels: dict,
    drawdown: dict,
    supervisor: dict | None = None,
    candidates: dict | None = None,
) -> list[str]:
    """Translate HOLD / NO_GO sections into operator-actionable steps."""
    actions: list[str] = []

    # Launch-candidate scan — if no bot meets the safety profile, that's the
    # #1 reason not to launch live. Surface this prominently.
    if candidates is not None:
        n = candidates.get("n_candidates", 0)
        if n == 0:
            top5 = candidates.get("rejected_top5", [])
            top5_summary = ", ".join(
                f"{r['bot_id']}(n={r['n']},USD={r.get('cum_usd')})"
                for r in top5[:3]
            )
            actions.append(
                "NO LAUNCH-CANDIDATE BOT exists yet. Profile required: "
                "n>=50 trades, cum_USD>0, cum_R>0, win_rate>=50%, NOT flagged "
                "ASYMMETRY_BUG. "
                f"Top-N rejected: {top5_summary}. "
                "Do not promote any bot to EVAL_LIVE until at least one "
                "meets the profile. See docs/LAUNCH_CANDIDATE_SCAN_*.md.",
            )

    # Supervisor health — front of queue if hung/missing
    if supervisor is not None:
        if supervisor.get("missing"):
            actions.append(
                f"Supervisor heartbeat MISSING at {supervisor.get('path', '?')}. "
                "Verify ApexRuntimeSupervisor scheduled task is Running. "
                "No trades will fire if the supervisor process is down.",
            )
        elif supervisor.get("age_seconds", 0) > 300:
            actions.append(
                f"Supervisor heartbeat STALE ({supervisor['age_seconds']}s old). "
                "Task may be hung. Restart ApexRuntimeSupervisor before launch.",
            )

    # Alert channels
    if not (channels["telegram"] or channels["discord"] or channels["generic"]):
        actions.append(
            "Set Telegram credentials (5 min): "
            '[Environment]::SetEnvironmentVariable("ETA_TELEGRAM_BOT_TOKEN", "<token>", "Machine"); '
            "ditto for ETA_TELEGRAM_CHAT_ID. "
            "Verify with `python -m eta_engine.scripts.verify_telegram --send-test`.",
        )

    # Lifecycle: any bots in live?
    n_live = lifecycle["counts"].get("EVAL_LIVE", 0) + lifecycle["counts"].get("FUNDED_LIVE", 0)
    if n_live == 0:
        actions.append(
            "Promote at least one PROP_READY bot to EVAL_LIVE: "
            "`python -m eta_engine.scripts.manage_lifecycle set <bot_id> EVAL_LIVE`. "
            "Currently EVERY bot is EVAL_PAPER — no live execution will occur.",
        )

    # Leaderboard: enough PROP_READY?
    n_ready = leaderboard.get("n_prop_ready", 0)
    if n_ready < 2:
        actions.append(
            f"Wait for >=2 PROP_READY bot designations (currently {n_ready}). "
            "The leaderboard recomputes hourly via ETA-Diamond-LeaderboardHourly cron. "
            "Bots qualify when n_trades >= 100 AND avg_r >= +0.20R on live+paper data.",
        )

    # Drawdown guard
    sig = drawdown.get("signal")
    if sig == "HALT":
        actions.append(
            "Drawdown guard is HALT — investigate "
            f"({drawdown.get('rationale', 'no detail')}). "
            "Consult docs/PROP_FUND_ROLLBACK_RUNBOOK.md before any live activity.",
        )
    elif sig == "WATCH":
        actions.append(
            "Drawdown guard is WATCH — supervisor will halve all entry sizes. "
            "Acceptable for cautious start; review live PnL vs buffer before scaling.",
        )

    # Stale freshness items
    for sec in dryrun.get("sections", []):
        if sec.get("name") == "freshness" and sec.get("status") in {"HOLD", "NO_GO"}:
            stale = sec.get("detail", {}).get("stale", [])
            if stale:
                actions.append(
                    "Refresh cron receipts (some are stale): "
                    "verify the ETA-Diamond-* tasks are running on schedule. "
                    f"Stale: {stale[:3]}{'...' if len(stale) > 3 else ''}",
                )
            break

    return actions


def _verdict_to_exit_code(verdict: str) -> int:
    return {"GO": 0, "HOLD": 1, "NO_GO": 2}.get(verdict, 2)


def _print_human(report: dict) -> None:
    dryrun = report["dryrun"]
    lifecycle = report["lifecycle"]
    leaderboard = report["leaderboard"]
    channels = report["alert_channels"]
    drawdown = report["drawdown_guard"]
    actions = report["actions"]

    verdict = dryrun.get("overall_verdict", "?")
    summary_text = dryrun.get("summary", "")

    print()
    print("=" * 92)
    print(f"  PROP LAUNCH CHECK  ({report['ts']})  verdict={verdict}")
    print("=" * 92)
    print(f"  {summary_text}")

    # Supervisor health
    supervisor = report.get("supervisor", {})
    _print_section_header("Supervisor health")
    if supervisor.get("missing"):
        print(f"  HEARTBEAT MISSING at {supervisor.get('path', '?')}")
        if supervisor.get("error"):
            print(f"    error: {supervisor['error']}")
    else:
        age = supervisor.get("age_seconds")
        stale = " (STALE)" if age and age > 300 else ""
        print(f"  heartbeat: {age}s old{stale}")
        print(
            f"  tick={supervisor.get('tick_count')} mode={supervisor.get('mode')} "
            f"feed={supervisor.get('feed')} n_bots={supervisor.get('n_bots')} "
            f"live_money={supervisor.get('live_money_enabled')}",
        )

    # Launch candidates
    candidates = report.get("launch_candidates", {})
    _print_section_header("Launch candidates (n>=50, cum_USD>0, cum_R>0, WR>=50%, !ASYM)")
    n_c = candidates.get("n_candidates", 0)
    if n_c == 0:
        print("  ZERO candidates — system says DO NOT LAUNCH live yet")
        top5 = candidates.get("rejected_top5", [])
        if top5:
            print(f"  Top {len(top5)} rejected (most data):")
            for r in top5:
                usd = f"${r.get('cum_usd'):+.0f}" if r.get("cum_usd") is not None else "n/a"
                asym = " ASYM" if r.get("asymmetry_flagged") else ""
                print(
                    f"    {r['bot_id']:<28} n={r['n']:>4} WR={r['wr']:>4.1f}% "
                    f"cum_R={r['cum_r']:>+7.1f} cum_USD={usd}{asym}",
                )
    else:
        print(f"  {n_c} candidate(s) meet the profile:")
        for c in candidates.get("candidates", []):
            print(
                f"    {c['bot_id']:<28} n={c['n']:>4} WR={c['wr']:>4.1f}% "
                f"cum_R={c['cum_r']:>+7.1f} cum_USD=${c['cum_usd']:+.2f}",
            )

    # Drawdown guard
    _print_section_header("Drawdown guard")
    if drawdown.get("missing"):
        print("  (no receipt — run python -m eta_engine.scripts.diamond_prop_drawdown_guard)")
    else:
        sig = drawdown.get("signal", "?")
        print(f"  signal: {sig}  ({drawdown.get('rationale', '')})")
        db = drawdown.get("daily_buffer_usd")
        sb = drawdown.get("static_buffer_usd")
        print(f"  daily buffer: ${db}  static buffer: ${sb}")
        print(f"  consistency: {drawdown.get('consistency_status', '?')}")

    # Leaderboard
    _print_section_header(f"Leaderboard top-5 (n_prop_ready={leaderboard.get('n_prop_ready', 0)})")
    if leaderboard.get("missing"):
        print("  (no leaderboard receipt)")
    else:
        print(f"  PROP_READY: {leaderboard.get('prop_ready_bots') or '(none)'}")
        for e in leaderboard.get("top_5", []):
            mark = "*" if e.get("prop_ready") else " "
            print(
                f"  {mark} rank={e.get('rank')} {e.get('bot_id', '?'):<28} "
                f"n={e.get('n_trades', 0):>4} avg_r={float(e.get('avg_r', 0.0)):>+6.3f} "
                f"wr={float(e.get('win_rate_pct', 0.0)):>5.1f}%",
            )

    # Lifecycle
    _print_section_header("Wave-25 lifecycle")
    lc = lifecycle["counts"]
    lc_line = (
        f"  EVAL_LIVE={lc.get('EVAL_LIVE', 0)} "
        f"EVAL_PAPER={lc.get('EVAL_PAPER', 0)} "
        f"FUNDED_LIVE={lc.get('FUNDED_LIVE', 0)} "
        f"RETIRED={lc.get('RETIRED', 0)}"
    )
    print(lc_line)
    for state, names in lifecycle["by_state"].items():
        if names:
            print(f"  {state}: {', '.join(names)}")

    # Alert channels
    _print_section_header("Alert channels")
    if channels["telegram"]:
        print("  telegram: configured")
    if channels["discord"]:
        print("  discord: configured")
    if channels["generic"]:
        print("  generic_webhook: configured")
    if not any(channels.values()):
        print("  (NONE configured — HALT will only show on dashboard)")

    # Dryrun sections summary
    _print_section_header("Dryrun sections")
    for sec in dryrun.get("sections", []):
        status = sec.get("status", "?")
        rat = sec.get("rationale", "").split("\n")[0][:80]
        print(f"  [{status:<5}] {sec.get('name', '?'):<22}  {rat}")

    # Actionable next steps
    _print_section_header("Action items (in priority order)")
    if not actions:
        print("  None — all gates clear.")
    else:
        for i, a in enumerate(actions, 1):
            print(f"  {i}. {a}")
            print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--verbose", action="store_true", help="Include full dryrun section details")
    args = parser.parse_args(argv)

    dryrun = _check_dryrun()
    lifecycle = _check_lifecycle()
    leaderboard = _check_leaderboard()
    channels = _check_alert_channels()
    drawdown = _check_drawdown_guard()
    supervisor = _check_supervisor()
    candidates = _check_launch_candidates()
    actions = _build_action_list(
        dryrun,
        lifecycle,
        leaderboard,
        channels,
        drawdown,
        supervisor=supervisor,
        candidates=candidates,
    )

    report = {
        "ts": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "dryrun": dryrun,
        "lifecycle": lifecycle,
        "leaderboard": leaderboard,
        "alert_channels": channels,
        "drawdown_guard": drawdown,
        "supervisor": supervisor,
        "launch_candidates": candidates,
        "actions": actions,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)

    return _verdict_to_exit_code(dryrun.get("overall_verdict", "NO_GO"))


if __name__ == "__main__":
    sys.exit(main())
