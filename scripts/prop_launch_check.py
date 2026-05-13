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


def _build_action_list(
    dryrun: dict,
    lifecycle: dict,
    leaderboard: dict,
    channels: dict,
    drawdown: dict,
) -> list[str]:
    """Translate HOLD / NO_GO sections into operator-actionable steps."""
    actions: list[str] = []

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
    actions = _build_action_list(dryrun, lifecycle, leaderboard, channels, drawdown)

    report = {
        "ts": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "dryrun": dryrun,
        "lifecycle": lifecycle,
        "leaderboard": leaderboard,
        "alert_channels": channels,
        "drawdown_guard": drawdown,
        "actions": actions,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)

    return _verdict_to_exit_code(dryrun.get("overall_verdict", "NO_GO"))


if __name__ == "__main__":
    sys.exit(main())
