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
  2. Wave-25 lifecycle table â€” which bots route to live vs paper
  3. Leaderboard top-5 with PROP_READY designation
  4. Drawdown guard signal + buffer state
  5. Alert channel configuration
  6. Cron task freshness (which receipts are stale)
  7. **Actionable next steps** â€” exactly what the operator needs to do
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

from eta_engine.scripts.retune_advisory_cache import build_retune_advisory

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def _state_dir() -> Path:
    return WORKSPACE_ROOT / "var" / "eta_engine" / "state"


def _health_dir() -> Path:
    return _state_dir() / "health"


def _read_json_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _dict_field(payload: dict, key: str) -> dict:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _string_list(payload: dict, key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


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
    from eta_engine.scripts import alert_channel_config  # noqa: PLC0415

    return {
        "telegram": alert_channel_config.telegram_configured(),
        "discord": alert_channel_config.discord_configured(),
        "generic": alert_channel_config.generic_configured(),
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


def _check_live_capital_calendar() -> dict:
    """Calendar policy: live capital is blocked until the operator-set floor."""
    from eta_engine.feeds.capital_allocator import build_live_capital_calendar_status  # noqa: PLC0415

    return build_live_capital_calendar_status()


def _check_retune_advisory() -> dict:
    return build_retune_advisory(_health_dir())


def _check_launch_candidates() -> dict:
    """Scan every diamond bot for the launch-candidate profile.

    A bot is a defensible launch candidate when it meets ALL of:
      * n_trades >= 50 (sample sufficient)
      * cum_USD > 0 (real-money outcome profitable)
      * cum_R > 0 (R-edge backs up the USD)
      * win_rate >= 50%
      * NOT flagged ASYMMETRY_BUG by the qty audit (if available)

    Also computes a per-qty-band breakdown to surface "vol-regime filter
    candidates" â€” bots whose qty<1 sub-cohort meets the launch profile
    even though the aggregate fails (wave-25o). A filter-candidate has:
      * qty<1 band n >= 20
      * qty<1 band cum_USD > 0
      * qty<1 band WR >= 80%
    This is a diagnostic lead, not a universal config prescription. For
    ``mnq_futures_sage`` specifically, the corrected remediation is the
    bot-scoped partial-profit experiment in
    ``docs/MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC_CORRECTION_2026_05_13.md``.

    Returns counts + the candidate list so the action-builder can
    surface "no candidates yet â€” don't launch" as a priority item.
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

    def _band_stats(subset_rows: list[dict]) -> dict:
        """Compute n, WR, cum_R, cum_USD for a row subset."""
        n_sub = len(subset_rows)
        if not n_sub:
            return {"n": 0, "wr": 0.0, "cum_r": 0.0, "cum_usd": None}
        cum_r_sub = sum(float(r.get("realized_r", 0) or 0) for r in subset_rows)
        wins_sub = sum(1 for r in subset_rows if float(r.get("realized_r", 0) or 0) > 0)
        wr_sub = wins_sub / n_sub * 100
        pnls_sub: list[float] = []
        for r in subset_rows:
            extra = r.get("extra") or {}
            usd = r.get("realized_pnl")
            if usd is None and isinstance(extra, dict):
                usd = extra.get("realized_pnl")
            try:
                if usd is not None:
                    pnls_sub.append(float(usd))
            except (TypeError, ValueError):
                pass
        cum_usd_sub = sum(pnls_sub) if pnls_sub else None
        return {
            "n": n_sub,
            "wr": round(wr_sub, 1),
            "cum_r": round(cum_r_sub, 2),
            "cum_usd": round(cum_usd_sub, 2) if cum_usd_sub is not None else None,
        }

    def _row_qty(r: dict) -> float:
        """Extract numeric qty (handles missing or extra-embedded values)."""
        q = r.get("qty")
        if q is None:
            extra = r.get("extra") or {}
            if isinstance(extra, dict):
                q = extra.get("qty")
        try:
            return float(q) if q is not None else 1.0
        except (TypeError, ValueError):
            return 1.0

    candidates: list[dict] = []
    filter_candidates: list[dict] = []
    rejected: list[dict] = []
    for bot_id in sorted(DIAMOND_BOTS):
        rows = load_close_records(
            bot_filter=bot_id,
            data_sources=DEFAULT_PRODUCTION_DATA_SOURCES,
        )
        n = len(rows)
        if n < 5:
            continue
        agg = _band_stats(rows)
        # Per-qty-band breakdown (wave-25o): a qty split often exposes
        # a useful regime/provenance clue (one band profitable, one
        # churning), but the remediation is bot-specific.
        rows_full = [r for r in rows if _row_qty(r) >= 1.0]
        rows_half = [r for r in rows if _row_qty(r) < 1.0]
        band_full = _band_stats(rows_full)
        band_half = _band_stats(rows_half)
        is_asym = bot_id in asymmetric
        is_candidate = (
            n >= 50
            and (agg["cum_usd"] or 0) > 0
            and agg["cum_r"] > 0
            and agg["wr"] >= 50.0
            and not is_asym
        )
        # Vol-regime filter candidate: qty<1 band meets launch profile
        # even though aggregate fails.
        is_filter_candidate = (
            not is_candidate
            and band_half["n"] >= 20
            and (band_half["cum_usd"] or 0) > 0
            and band_half["wr"] >= 80.0
        )
        record = {
            "bot_id": bot_id,
            "n": n,
            "wr": agg["wr"],
            "cum_r": agg["cum_r"],
            "cum_usd": agg["cum_usd"],
            "asymmetry_flagged": is_asym,
            "qty_band_full": band_full,
            "qty_band_half": band_half,
            "vol_regime_filter_candidate": is_filter_candidate,
        }
        if is_candidate:
            candidates.append(record)
        else:
            rejected.append(record)
            if is_filter_candidate:
                filter_candidates.append(record)
    return {
        "n_candidates": len(candidates),
        "candidates": candidates,
        "n_filter_candidates": len(filter_candidates),
        "filter_candidates": filter_candidates,
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
    live_capital_calendar: dict | None = None,
    retune_advisory: dict | None = None,
) -> list[str]:
    """Translate HOLD / NO_GO sections into operator-actionable steps."""
    actions: list[str] = []

    calendar_allows_live = True
    if live_capital_calendar is not None:
        calendar_allows_live = bool(live_capital_calendar.get("live_capital_allowed_by_date"))
        if not calendar_allows_live:
            actions.append(
                "NO LIVE CAPITAL BEFORE "
                f"{live_capital_calendar.get('not_before', '2026-07-08')}. "
                "Keep the supervisor in paper_live/paper_sim, keep order routing paper-only, "
                "and use the next week for prop-firm readiness drills only.",
            )

    # Launch-candidate scan â€” if no bot meets the safety profile, that's the
    # #1 reason not to launch live. Surface this prominently.
    if candidates is not None:
        n = candidates.get("n_candidates", 0)
        if n == 0:
            top5 = candidates.get("rejected_top5", [])
            top5_summary = ", ".join(
                f"{r['bot_id']}(n={r['n']},USD={r.get('cum_usd')})"
                for r in top5[:3]
            )
            top5_clause = (
                f"Top-N rejected: {top5_summary}. "
                if top5_summary
                else "No bots have enough production data (n>=5) on this filter yet. "
            )
            actions.append(
                "NO LAUNCH-CANDIDATE BOT exists yet. Profile required: "
                "n>=50 trades, cum_USD>0, cum_R>0, win_rate>=50%, NOT flagged "
                "ASYMMETRY_BUG. "
                f"{top5_clause}"
                "Do not promote any bot to EVAL_LIVE until at least one "
                "meets the profile. See docs/LAUNCH_CANDIDATE_SCAN_*.md.",
            )

        # Wave-25o: surface qty-band filter candidates â€” bots whose qty<1
        # band is launch-profile-quality even when aggregate fails. The
        # remediation is bot-specific; do not assume every lane has a
        # strategy-native vol-size knob.
        for fc in candidates.get("filter_candidates", []) or []:
            half = fc.get("qty_band_half", {}) or {}
            full = fc.get("qty_band_full", {}) or {}
            half_usd = half.get("cum_usd")
            full_usd = full.get("cum_usd")
            half_usd_fmt = f"${half_usd:+.0f}" if half_usd is not None else "n/a"
            full_usd_fmt = f"${full_usd:+.0f}" if full_usd is not None else "n/a"
            lead = (
                f"VOL-REGIME FILTER candidate: {fc['bot_id']} - "
                f"qty<1 band shows n={half.get('n', 0)}, "
                f"WR={half.get('wr', 0.0):.1f}%, cum_USD={half_usd_fmt}. "
                f"qty=1 band churns "
                f"(n={full.get('n', 0)}, WR={full.get('wr', 0.0):.1f}%, "
                f"cum_USD={full_usd_fmt}). "
            )
            if fc.get("bot_id") == "mnq_futures_sage":
                actions.append(
                    lead
                    + "Do not use the legacy normal-vol skip knob here: this bot runs "
                    + "orb_sage_gated, and the qty split traced to supervisor "
                    + "partial-profit slicing rather than a strategy vol-size "
                    + "knob. Run the corrected paper-soak with "
                    + "partial_profit_enabled=false and review "
                    + "docs/MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC_CORRECTION_2026_05_13.md."
                )
            else:
                actions.append(
                    lead
                    + "Treat this as a qty-band divergence investigation lead. "
                    + "Confirm whether the split comes from strategy sizing, "
                    + "execution geometry, or supervisor features before "
                    + "changing launch config, then paper-soak the bot-specific "
                    + "remediation before EVAL_LIVE."
                )

    # Supervisor health â€” front of queue if hung/missing
    if retune_advisory and retune_advisory.get("available"):
        focus_bot = retune_advisory.get("focus_bot") or "unknown"
        focus_state = retune_advisory.get("focus_state") or "unknown"
        focus_issue = retune_advisory.get("focus_issue") or "unknown"
        focus_closes = retune_advisory.get("focus_closed_trade_count")
        focus_pnl = retune_advisory.get("focus_total_realized_pnl")
        focus_pf = retune_advisory.get("focus_profit_factor")
        if focus_issue == "broker_pnl_negative" or retune_advisory.get("diagnosis"):
            closes_text = focus_closes if focus_closes is not None else "n/a"
            pnl_text = f"${focus_pnl:+.2f}" if isinstance(focus_pnl, int | float) else "n/a"
            pf_text = f"{focus_pf:.2f}" if isinstance(focus_pf, int | float) else "n/a"
            action = (
                f"Broker-backed retune truth still flags {focus_bot} "
                f"({focus_state}; issue={focus_issue}; closes={closes_text}; "
                f"pnl={pnl_text}; PF={pf_text}). "
                "Do not treat it as a launch candidate yet."
            )
            preferred_action = retune_advisory.get("preferred_action")
            if isinstance(preferred_action, str) and preferred_action.strip():
                action += f" {preferred_action}"
            actions.append(action)
        experiment = retune_advisory.get("active_experiment") or {}
        if isinstance(experiment, dict) and experiment:
            started_at = experiment.get("started_at") or "unknown"
            sample_n = experiment.get("post_change_closed_trade_count")
            sample_pnl = experiment.get("post_change_total_realized_pnl")
            sample_pf = experiment.get("post_change_profit_factor")
            n_text = sample_n if isinstance(sample_n, int) else "n/a"
            pnl_text = f"${sample_pnl:+.2f}" if isinstance(sample_pnl, int | float) else "n/a"
            pf_text = f"{sample_pf:.2f}" if isinstance(sample_pf, int | float) else "n/a"
            if focus_bot == "mnq_futures_sage" and experiment.get("partial_profit_enabled") is False:
                actions.append(
                    f"Corrected {focus_bot} experiment active since {started_at}: "
                    f"partial_profit_enabled=false, post-fix sample n={n_text}, "
                    f"pnl={pnl_text}, PF={pf_text}. Let fresh post-fix closes "
                    "accumulate before treating the legacy aggregate as the new verdict."
                )

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
            "Set Telegram credentials (5 min): seed ETA_TELEGRAM_BOT_TOKEN + ETA_TELEGRAM_CHAT_ID as machine env vars, "
            "or populate `eta_engine/secrets/telegram_bot_token.txt` + `eta_engine/secrets/telegram_chat_id.txt`. "
            "Verify with `python -m eta_engine.scripts.verify_telegram --send-test`.",
        )

    # Lifecycle: any bots in live?
    n_live = lifecycle["counts"].get("EVAL_LIVE", 0) + lifecycle["counts"].get("FUNDED_LIVE", 0)
    if n_live == 0:
        if calendar_allows_live:
            actions.append(
                "Promote at least one PROP_READY bot to EVAL_LIVE: "
                "`python -m eta_engine.scripts.manage_lifecycle set <bot_id> EVAL_LIVE`. "
                "Currently EVERY bot is EVAL_PAPER - no live execution will occur.",
            )
        else:
            actions.append(
                "Do not promote bots to EVAL_LIVE/FUNDED_LIVE during the paper-only window. "
                "Use paper_live execution plus daily prop_launch_check evidence instead.",
            )
    elif not calendar_allows_live:
        actions.append(
            f"{n_live} bot(s) are marked EVAL_LIVE/FUNDED_LIVE, but runtime routing is "
            "calendar-held to paper until the live-capital date floor. Treat these as "
            "dry-run labels, not permission to route capital.",
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
            "Drawdown guard is HALT â€” investigate "
            f"({drawdown.get('rationale', 'no detail')}). "
            "Consult docs/PROP_FUND_ROLLBACK_RUNBOOK.md before any live activity.",
        )
    elif sig == "WATCH":
        actions.append(
            "Drawdown guard is WATCH â€” supervisor will halve all entry sizes. "
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

    for sec in dryrun.get("sections", []):
        if sec.get("name") != "task_registration" or sec.get("status") not in {"HOLD", "NO_GO"}:
            continue
        detail = sec.get("detail", {}) or {}
        missing = detail.get("missing", []) or []
        nonready = detail.get("nonready", []) or []
        if missing:
            actions.append(
                "Register the missing ETA-Diamond scheduled tasks on the intended Windows runtime host: "
                "`powershell -ExecutionPolicy Bypass -File eta_engine/deploy/scripts/register_diamond_cron_tasks.ps1 "
                "-WorkspaceRoot C:\\EvolutionaryTradingAlgo -StartNow`. "
                f"Missing: {missing[:5]}{'...' if len(missing) > 5 else ''}",
            )
        elif nonready:
            actions.append(
                "Inspect the registered ETA-Diamond scheduled tasks in Task Scheduler "
                "and restore them to Ready/Running. "
                f"Non-ready: {nonready[:5]}{'...' if len(nonready) > 5 else ''}",
            )
        else:
            actions.append(
                "Scheduled-task probe was unavailable. Verify the ETA-Diamond task lane "
                "on the intended Windows runtime host "
                "before treating fresh receipts as self-sustaining automation.",
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
    retune_advisory = report.get("retune_advisory", {})

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

    # Live-capital calendar
    calendar = report.get("live_capital_calendar", {})
    _print_section_header("Live-capital calendar")
    if calendar:
        status = "allowed" if calendar.get("live_capital_allowed_by_date") else "paper-only"
        print(
            f"  status={status} today={calendar.get('today')} "
            f"not_before={calendar.get('not_before')} "
            f"days_until={calendar.get('days_until_live_capital')}",
        )
        print(f"  {calendar.get('reason', '')}")

    _print_section_header("Broker-backed retune advisory")
    if not retune_advisory.get("available"):
        print("  (no public retune advisory cache)")
    else:
        focus_pnl = retune_advisory.get("focus_total_realized_pnl")
        focus_pf = retune_advisory.get("focus_profit_factor")
        broker_mtd = retune_advisory.get("broker_mtd_pnl")
        today_realized = retune_advisory.get("today_realized_pnl")
        total_unrealized = retune_advisory.get("total_unrealized_pnl")
        print(
            f"  focus={retune_advisory.get('focus_bot')} "
            f"state={retune_advisory.get('focus_state')} "
            f"issue={retune_advisory.get('focus_issue')}",
        )
        print(
            f"  broker proof: closes={retune_advisory.get('focus_closed_trade_count')} "
            f"pnl={f'${focus_pnl:+.2f}' if isinstance(focus_pnl, int | float) else 'n/a'} "
            f"pf={f'{focus_pf:.2f}' if isinstance(focus_pf, int | float) else 'n/a'}",
        )
        print(
            f"  broker state: mtd={f'${broker_mtd:+.2f}' if isinstance(broker_mtd, int | float) else 'n/a'} "
            f"today={f'${today_realized:+.2f}' if isinstance(today_realized, int | float) else 'n/a'} "
            f"open={f'${total_unrealized:+.2f}' if isinstance(total_unrealized, int | float) else 'n/a'} "
            f"positions={retune_advisory.get('open_position_count')} "
            f"source={retune_advisory.get('broker_snapshot_source')}",
        )
        if retune_advisory.get("diagnosis"):
            print(f"  local drift: {retune_advisory.get('diagnosis')}")
        if retune_advisory.get("preferred_warning"):
            print(f"  warning: {retune_advisory.get('preferred_warning')}")
        experiment = retune_advisory.get("active_experiment") or {}
        if isinstance(experiment, dict) and experiment:
            sample_pnl = experiment.get("post_change_total_realized_pnl")
            sample_pf = experiment.get("post_change_profit_factor")
            pnl_text = f"${sample_pnl:+.2f}" if isinstance(sample_pnl, int | float) else "n/a"
            pf_text = f"{sample_pf:.2f}" if isinstance(sample_pf, int | float) else "n/a"
            print(
                f"  experiment: id={experiment.get('experiment_id')} "
                f"started={experiment.get('started_at')} "
                f"partial_profit_enabled={experiment.get('partial_profit_enabled')} "
                f"post_fix_n={experiment.get('post_change_closed_trade_count')} "
                f"post_fix_pnl={pnl_text} post_fix_pf={pf_text}",
            )

    # Launch candidates
    candidates = report.get("launch_candidates", {})
    _print_section_header("Launch candidates (n>=50, cum_USD>0, cum_R>0, WR>=50%, !ASYM)")
    n_c = candidates.get("n_candidates", 0)
    n_fc = candidates.get("n_filter_candidates", 0)
    if n_c == 0:
        print("  ZERO strict candidates â€” system says DO NOT LAUNCH live yet")
        if n_fc > 0:
            print(
                f"  {n_fc} VOL-REGIME FILTER candidate(s): aggregate fails but the "
                "qty<1 band passes. See action items for bot-specific remediation; "
                "mnq_futures_sage uses the corrected partial-profit experiment in "
                "docs/MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC_CORRECTION_2026_05_13.md.",
            )
        top5 = candidates.get("rejected_top5", [])
        if top5:
            print(f"  Top {len(top5)} rejected (most data):")
            for r in top5:
                usd = f"${r.get('cum_usd'):+.0f}" if r.get("cum_usd") is not None else "n/a"
                asym = " ASYM" if r.get("asymmetry_flagged") else ""
                fc_marker = " *VOL_FILTER" if r.get("vol_regime_filter_candidate") else ""
                print(
                    f"    {r['bot_id']:<28} n={r['n']:>4} WR={r['wr']:>4.1f}% "
                    f"cum_R={r['cum_r']:>+7.1f} cum_USD={usd}{asym}{fc_marker}",
                )
                # Per-qty-band split (wave-25o) â€” surface only when both
                # bands have material samples; this is where the
                # vol_adjusted_sizing asymmetry is most visible.
                half = r.get("qty_band_half", {}) or {}
                full = r.get("qty_band_full", {}) or {}
                if half.get("n", 0) >= 5 and full.get("n", 0) >= 5:
                    h_usd = (
                        f"${half.get('cum_usd'):+.0f}"
                        if half.get("cum_usd") is not None
                        else "n/a"
                    )
                    f_usd = (
                        f"${full.get('cum_usd'):+.0f}"
                        if full.get("cum_usd") is not None
                        else "n/a"
                    )
                    print(
                        f"        qty<1: n={half['n']:>3} WR={half['wr']:>5.1f}% USD={h_usd:>7}    "
                        f"qty>=1: n={full['n']:>3} WR={full['wr']:>5.1f}% USD={f_usd:>7}",
                    )
    else:
        print(f"  {n_c} candidate(s) meet the profile:")
        for c in candidates.get("candidates", []):
            print(
                f"    {c['bot_id']:<28} n={c['n']:>4} WR={c['wr']:>4.1f}% "
                f"cum_R={c['cum_r']:>+7.1f} cum_USD=${c['cum_usd']:+.2f}",
            )
        if n_fc > 0:
            print(
                f"  {n_fc} additional VOL-REGIME FILTER candidate(s) - see action items.",
            )

    # Drawdown guard
    _print_section_header("Drawdown guard")
    if drawdown.get("missing"):
        print("  (no receipt â€” run python -m eta_engine.scripts.diamond_prop_drawdown_guard)")
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
        print("  (NONE configured â€” HALT will only show on dashboard)")

    # Dryrun sections summary
    _print_section_header("Dryrun sections")
    for sec in dryrun.get("sections", []):
        status = sec.get("status", "?")
        rat = sec.get("rationale", "").split("\n")[0][:80]
        print(f"  [{status:<5}] {sec.get('name', '?'):<22}  {rat}")

    # Actionable next steps
    _print_section_header("Action items (in priority order)")
    if not actions:
        print("  None â€” all gates clear.")
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
    live_capital_calendar = _check_live_capital_calendar()
    retune_advisory = _check_retune_advisory()
    actions = _build_action_list(
        dryrun,
        lifecycle,
        leaderboard,
        channels,
        drawdown,
        supervisor=supervisor,
        candidates=candidates,
        live_capital_calendar=live_capital_calendar,
        retune_advisory=retune_advisory,
    )

    report = {
        "ts": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "dryrun": dryrun,
        "lifecycle": lifecycle,
        "leaderboard": leaderboard,
        "alert_channels": channels,
        "drawdown_guard": drawdown,
        "supervisor": supervisor,
        "live_capital_calendar": live_capital_calendar,
        "launch_candidates": candidates,
        "retune_advisory": retune_advisory,
        "actions": actions,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)

    return _verdict_to_exit_code(dryrun.get("overall_verdict", "NO_GO"))


if __name__ == "__main__":
    sys.exit(main())
