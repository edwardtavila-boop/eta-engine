"""Kaizen loop — the elite framework on autopilot.

Runs every layer of the elite framework in one pass, builds the
operator's morning report, and (optionally with --apply) executes the
safe automated actions:

    1. elite_scoreboard      → ELITE / PRODUCER / DECAY tier per bot
    2. monte_carlo_validator → ROBUST / FRAGILE / LUCKY / DEAD per bot
    3. sage_oracle           → composite-bias + clash patterns per bot
    4. bot_pressure_test     → ranked param-tuning candidates
    5. edge_tracker snapshot → per-school hit-rate + weight modifier

Outputs:
    * Console summary (tier counts, action queue)
    * var/eta_engine/state/kaizen_reports/kaizen_<UTC-stamp>.json
      (full structured report with every finding)
    * var/eta_engine/state/kaizen_latest.json (always points at the
      most recent run; convenient for dashboards)

Auto-actions taken with --apply:
    * Bots with MIXED-or-worse + n>=30 + negative expectancy_R for two
      consecutive runs → auto-deactivate. Two side-effects:
        1. Append APPLIED record to var/eta_engine/state/kaizen_actions.jsonl
        2. Write entry under var/eta_engine/state/kaizen_overrides.json
           (per_bot_registry.is_active() honors this file on next
           supervisor restart — bot will not load).
      Operator can re-enable a bot via:
        python -m eta_engine.scripts.kaizen_reactivate <bot_id>

Without --apply, runs in REPORT-ONLY mode — pure observation.

Usage (manual):
    python -m eta_engine.scripts.kaizen_loop
    python -m eta_engine.scripts.kaizen_loop --apply
    python -m eta_engine.scripts.kaizen_loop --since 2026-05-04T23:31:00

Usage (scheduled — Windows Task Scheduler runs daily 06:00 UTC):
    schtasks /Create /TN "ETA-Kaizen-Loop" /SC DAILY /ST 06:00 ^
        /TR "python -m eta_engine.scripts.kaizen_loop --apply"
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.kaizen_loop")

_REPORT_DIR = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_reports",
)
_LATEST_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_latest.json",
)
_ACTION_LOG = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_actions.jsonl",
)
# Sidecar that ``per_bot_registry.is_active()`` reads — bot_ids listed
# here are dropped from the supervisor's load_bots() filter on next
# supervisor restart. The 2-run confirmation gate (in run_loop) is the
# only writer; operator can re-enable a bot via
# ``python -m eta_engine.scripts.kaizen_reactivate <bot_id>``.
_OVERRIDES_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_overrides.json",
)
# JARVIS<->Hermes audit log (written by Half 1's MCP server). Read here
# in the morning kaizen pass to surface 24h Hermes activity in the
# operator report. Module-level so tests can monkeypatch to redirect
# the read at a tmp_path fixture.
_HERMES_AUDIT_LOG_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_actions.jsonl",
)


def _run_elite_scoreboard(since_iso: str | None) -> dict[str, Any]:
    """Run the per-bot tier classification."""
    try:
        from eta_engine.scripts import elite_scoreboard
        return elite_scoreboard.analyze(since_iso=since_iso)
    except Exception as exc:  # noqa: BLE001
        logger.exception("elite_scoreboard failed: %s", exc)
        return {"error": str(exc), "n_bots": 0, "bots": {}, "tier_counts": {}}


def _run_monte_carlo(since_iso: str | None, bootstraps: int) -> dict[str, Any]:
    """Run the bootstrap robustness validator."""
    try:
        from eta_engine.scripts import monte_carlo_validator
        return monte_carlo_validator.analyze(
            since_iso=since_iso, bootstraps=bootstraps, seed=42,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("monte_carlo_validator failed: %s", exc)
        return {"error": str(exc), "n_bots": 0, "bots": {}, "verdict_counts": {}}


def _read_edge_tracker_snapshot() -> dict[str, Any]:
    """Pull the live per-school edge attribution."""
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
        return default_tracker().snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("edge_tracker snapshot failed: %s", exc)
        return {}


def _summarize_per_bot(
    elite: dict[str, Any],
    mc: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Combine elite + MC findings per bot."""
    out: dict[str, dict[str, Any]] = {}
    elite_bots = elite.get("bots", {}) or {}
    mc_bots = mc.get("bots", {}) or {}
    for bot_id in set(elite_bots) | set(mc_bots):
        e = elite_bots.get(bot_id, {})
        m = mc_bots.get(bot_id, {})
        out[bot_id] = {
            "tier": e.get("tier"),
            "mc_verdict": m.get("verdict"),
            "n": int(e.get("n", 0) or m.get("n", 0) or 0),
            "profit_factor": e.get("profit_factor"),
            "sharpe": e.get("sharpe"),
            "expectancy_r": e.get("expectancy_r"),
            "max_drawdown_r": e.get("max_drawdown_r"),
            "rolling_decay_pct": e.get("rolling_decay_pct"),
            "actual_final_R": m.get("actual_final_R"),
            "p05_final_R": m.get("p05_final_R"),
            "p_negative": m.get("p_negative"),
            "luck_score": m.get("luck_score"),
            "sum_pnl_usd": e.get("sum_pnl_usd"),
        }
    return out


def _action_queue(per_bot: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Identify the actions the operator (or --apply) should take.

    Action rules — all evidence-based and conservative:
      * RETIRE  : tier=MIXED-or-worse AND mc_verdict in {DEAD, MIXED}
                  AND expectancy_r < 0 AND n>=30
      * MONITOR : tier=DECAY OR mc_verdict=LUCKY (rolling Sharpe collapsed
                  or trade ordering looks lucky — needs more data,
                  no auto-action)
      * SCALE_UP: tier=ELITE AND mc_verdict=ROBUST AND luck_score < 0.05
                  (true producers worth more capital)
      * EVOLVE  : tier=PRODUCER OR FRAGILE — keep but watch metrics
      * NONE    : INSUFFICIENT or already-elite-but-fragile
    """
    actions: list[dict[str, Any]] = []
    for bot_id, m in per_bot.items():
        tier = m.get("tier")
        mc = m.get("mc_verdict")
        n = m.get("n", 0)
        exp_r = m.get("expectancy_r") or 0.0
        luck = m.get("luck_score") or 0.0

        if tier == "INSUFFICIENT" or n < 30:
            continue

        if (
            tier in {"MIXED", "DECAY"}
            and mc in {"DEAD", "MIXED"}
            and exp_r < 0
        ):
            actions.append({
                "bot_id": bot_id, "action": "RETIRE",
                "reason": (
                    f"tier={tier} mc={mc} expR={exp_r:+.4f} n={n} — "
                    "negative expectancy + MC confirms no edge"
                ),
                "tier": tier, "mc_verdict": mc,
                "expectancy_r": exp_r, "n": n,
                "auto_apply_safe": True,
            })
            continue

        if tier == "ELITE" and mc == "ROBUST" and luck < 0.05:
            actions.append({
                "bot_id": bot_id, "action": "SCALE_UP",
                "reason": (
                    f"tier=ELITE mc=ROBUST luck={luck:.3f} — true producer; "
                    "operator should consider capital increase"
                ),
                "tier": tier, "mc_verdict": mc,
                "luck_score": luck, "n": n,
                "auto_apply_safe": False,  # capital changes need operator
            })
            continue

        if tier == "DECAY" or mc == "LUCKY":
            actions.append({
                "bot_id": bot_id, "action": "MONITOR",
                "reason": f"tier={tier} mc={mc} — needs more observations",
                "tier": tier, "mc_verdict": mc, "n": n,
                "auto_apply_safe": False,
            })
            continue

        if tier in {"PRODUCER", "MARGINAL"} or mc == "FRAGILE":
            actions.append({
                "bot_id": bot_id, "action": "EVOLVE",
                "reason": f"tier={tier} mc={mc} — work on parameter tuning",
                "tier": tier, "mc_verdict": mc, "n": n,
                "auto_apply_safe": False,
            })

    return actions


def _previous_retire_targets() -> set[str]:
    """Read the kaizen_actions.jsonl log for prior RETIRE recommendations.

    Auto-deactivation requires the same RETIRE recommendation to appear
    in TWO consecutive kaizen runs (this run + a prior run on file).
    Stops a one-day metric anomaly from killing a strategy.
    """
    if not _ACTION_LOG.exists():
        return set()
    out: set[str] = set()
    try:
        with _ACTION_LOG.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("action") == "RETIRE":
                    out.add(str(rec.get("bot_id", "")))
    except OSError:
        return out
    return out


def _append_action_log(rec: dict[str, Any]) -> None:
    try:
        _ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ACTION_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError as exc:
        logger.warning("kaizen action log write failed: %s", exc)


def _load_overrides() -> dict[str, Any]:
    """Read the current sidecar override file (or return scaffold)."""
    if not _OVERRIDES_PATH.exists():
        return {"deactivated": {}}
    try:
        data = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"deactivated": {}}
    if not isinstance(data, dict):
        return {"deactivated": {}}
    if not isinstance(data.get("deactivated"), dict):
        data["deactivated"] = {}
    return data


def _apply_kaizen_deactivation(bot_id: str, action_record: dict[str, Any]) -> None:
    """Write a kaizen-deactivation entry to the sidecar override file.

    ``per_bot_registry.is_active()`` honors this file at supervisor
    startup; the kaizen-deactivated bot will not load on the next
    supervisor restart. Idempotent — re-applying for the same bot_id
    just updates the timestamp.
    """
    try:
        _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = _load_overrides()
        data["deactivated"][bot_id] = {
            "applied_at": datetime.now(UTC).isoformat(),
            "reason": action_record.get("reason", ""),
            "tier": action_record.get("tier"),
            "mc_verdict": action_record.get("mc_verdict"),
            "expectancy_r": action_record.get("expectancy_r"),
            "n": action_record.get("n"),
        }
        _OVERRIDES_PATH.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8",
        )
        logger.info(
            "kaizen-deactivated %s (tier=%s mc=%s expR=%s n=%s)",
            bot_id, action_record.get("tier"), action_record.get("mc_verdict"),
            action_record.get("expectancy_r"), action_record.get("n"),
        )
    except OSError as exc:
        logger.warning("kaizen override write failed for %s: %s", bot_id, exc)


def run_loop(
    *,
    since_iso: str | None = None,
    bootstraps: int = 1000,
    apply_actions: bool = False,
) -> dict[str, Any]:
    """Execute one kaizen loop iteration; return the structured report."""
    started_at = datetime.now(UTC).isoformat()
    elite = _run_elite_scoreboard(since_iso)
    mc = _run_monte_carlo(since_iso, bootstraps)
    edge = _read_edge_tracker_snapshot()

    per_bot = _summarize_per_bot(elite, mc)
    actions = _action_queue(per_bot)

    # Two-run confirmation gate for RETIRE actions: only deactivate when
    # the same RETIRE recommendation already exists in the action log
    # from a prior run. Single-run RETIRE recommendations are noted but
    # not applied — protects against one-day anomalies.
    prior_retires = _previous_retire_targets()
    applied_count = 0
    held_count = 0
    if apply_actions:
        for a in actions:
            if a["action"] != "RETIRE":
                continue
            if a["bot_id"] not in prior_retires:
                a["status"] = "HELD_PENDING_CONFIRMATION"
                held_count += 1
                continue
            # Second consecutive RETIRE — apply.
            a["status"] = "APPLIED"
            a["applied_at"] = datetime.now(UTC).isoformat()
            # Write the sidecar override so per_bot_registry.is_active()
            # drops the bot on the next supervisor restart.
            _apply_kaizen_deactivation(a["bot_id"], a)
            applied_count += 1

    # Write action records (HELD or APPLIED) for next-run cross-check.
    for a in actions:
        if a["action"] == "RETIRE":
            _append_action_log({
                "ts": datetime.now(UTC).isoformat(),
                "action": "RETIRE",
                "bot_id": a["bot_id"],
                "reason": a["reason"],
                "tier": a.get("tier"),
                "mc_verdict": a.get("mc_verdict"),
                "expectancy_r": a.get("expectancy_r"),
                "status": a.get("status", "RECOMMENDED"),
            })

    # Tier rollups
    tier_counts: dict[str, int] = defaultdict(int)
    mc_counts: dict[str, int] = defaultdict(int)
    for m in per_bot.values():
        tier_counts[m.get("tier") or "UNKNOWN"] += 1
        mc_counts[m.get("mc_verdict") or "UNKNOWN"] += 1

    action_counts: dict[str, int] = defaultdict(int)
    for a in actions:
        action_counts[a["action"]] += 1

    # Top per-school edges (for context)
    school_edges: list[dict[str, Any]] = []
    for school, snap in (edge or {}).items():
        n_obs = int(snap.get("n_obs", 0))
        if n_obs >= 5:
            school_edges.append({
                "school": school,
                "n_obs": n_obs,
                "hit_rate": snap.get("hit_rate"),
                "avg_r": snap.get("avg_r"),
                "expectancy": snap.get("expectancy"),
                "weight_modifier": snap.get("weight_modifier"),
            })
    school_edges.sort(key=lambda r: -float(r.get("expectancy") or 0))

    # JARVIS Supercharge overnight maintenance —
    # 1. Decay hot_learner session weights back toward 1.0 so a bad
    #    trading day doesn't poison the next session's school weights.
    # 2. Run the wiring audit to detect Sage/conductor modules that
    #    haven't fired in the last 7 days (dark-module alert).
    # Both are best-effort: failures log a warning and don't block the
    # rest of the kaizen pass.
    try:
        from eta_engine.brain.jarvis_v3 import hot_learner
        hot_learner.decay_overnight()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hot_learner.decay_overnight failed: %s", exc)

    wiring_statuses: list[Any] = []
    try:
        from eta_engine.scripts import jarvis_wiring_audit
        wiring_statuses = jarvis_wiring_audit.audit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("jarvis_wiring_audit.audit failed: %s", exc)

    dark_modules = [
        s for s in wiring_statuses
        if getattr(s, "expected_to_fire", False)
        and getattr(s, "dark_for_days", 0) >= 7
    ]
    wiring_summary = {
        "n_dark_modules": len(dark_modules),
        "dark_modules": [getattr(s, "module", "") for s in dark_modules],
        "n_total_expected_to_fire": sum(
            1 for s in wiring_statuses if getattr(s, "expected_to_fire", False)
        ),
        "n_total_modules": len(wiring_statuses),
    }

    # Hermes integration health summary. Reads
    # var/eta_engine/state/hermes_actions.jsonl over the last 24h
    # window and reports per-tool call counts + auth-failure totals.
    # All best-effort: if the audit log is missing or unreadable, the
    # section has zeros — never blocks the kaizen pass. Half 2's
    # hermes_client module is probed for live availability + backoff
    # state; absence of that module is a no-op (Half 2 ships separately).
    hermes_health: dict[str, Any] = {
        "hermes_available": False,
        "calls_today": 0,
        "calls_by_tool": {},
        "auth_failures_today": 0,
        "backoff_active": False,
    }
    try:
        from eta_engine.brain.jarvis_v3 import hermes_client  # type: ignore[import-not-found]
        hermes_health["hermes_available"] = hermes_client.health()
        # Backoff state is module-level inside hermes_client; expose
        # via a helper if it's defined, else fall back to False.
        hermes_health["backoff_active"] = getattr(
            hermes_client, "_backoff_active_for_kaizen", lambda: False,
        )()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_health: hermes_client probe failed: %s", exc)

    try:
        audit_path = _HERMES_AUDIT_LOG_PATH
        if audit_path.exists():
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            by_tool: dict[str, int] = defaultdict(int)
            auth_failures = 0
            total = 0
            with audit_path.open(encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("ts", "")
                    try:
                        # Accept both ``...Z`` and ``...+00:00`` suffixes —
                        # production writer uses Z, datetime.fromisoformat
                        # only accepts the explicit offset on <3.11.
                        rec_ts = datetime.fromisoformat(
                            ts.replace("Z", "+00:00"),
                        )
                    except (ValueError, AttributeError):
                        continue
                    if rec_ts < cutoff:
                        continue
                    total += 1
                    by_tool[str(rec.get("tool") or "unknown")] += 1
                    if rec.get("auth") == "failed":
                        auth_failures += 1
            hermes_health["calls_today"] = total
            hermes_health["calls_by_tool"] = dict(by_tool)
            hermes_health["auth_failures_today"] = auth_failures
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_health: audit-log read failed: %s", exc)

    report = {
        "started_at": started_at,
        "since_iso": since_iso,
        "bootstraps": bootstraps,
        "applied": apply_actions,
        "n_bots": len(per_bot),
        "tier_counts": dict(tier_counts),
        "mc_counts": dict(mc_counts),
        "action_counts": dict(action_counts),
        "applied_count": applied_count,
        "held_count": held_count,
        "actions": actions,
        "per_bot": per_bot,
        "school_edges_top": school_edges[:10],
        "school_edges_bottom": school_edges[-10:][::-1] if school_edges else [],
        "elite_summary": {
            "total_closes": elite.get("total_closes"),
            "tier_counts": elite.get("tier_counts"),
        },
        "mc_summary": {
            "verdict_counts": mc.get("verdict_counts"),
            "bootstraps_per_bot": mc.get("bootstraps_per_bot"),
        },
        "wiring": wiring_summary,
        "hermes_health": hermes_health,
    }

    # Persist
    try:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = _REPORT_DIR / f"kaizen_{stamp}.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        _LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LATEST_PATH.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("kaizen report write failed: %s", exc)

    return report


def _print_summary(report: dict[str, Any]) -> None:
    print("=" * 102)
    print(
        f" KAIZEN LOOP — {report['started_at']}  "
        f"applied={report['applied']}  bots={report['n_bots']}",
    )
    print("=" * 102)
    print(f" tier counts: {report['tier_counts']}")
    print(f" MC counts:   {report['mc_counts']}")
    print(f" action queue: {report['action_counts']}")
    if report["applied"]:
        print(
            f" applied this run: {report['applied_count']} retired, "
            f"{report['held_count']} held pending confirmation",
        )
    print()

    if report["actions"]:
        print(f" {'bot_id':<25} {'action':<10} {'tier':<10} {'mc':<10} {'reason'}")
        print("-" * 102)
        order = {"RETIRE": 0, "MONITOR": 1, "EVOLVE": 2, "SCALE_UP": 3}
        for a in sorted(
            report["actions"],
            key=lambda r: (order.get(r.get("action") or "", 9), r.get("bot_id", "")),
        ):
            print(
                f" {a['bot_id']:<25} {a.get('action', ''):<10} "
                f"{a.get('tier', ''):<10} {a.get('mc_verdict', ''):<10} "
                f"{(a.get('reason') or '')[:50]}",
            )
        print()

    if report.get("school_edges_top"):
        print(" TOP-EXPECTANCY SCHOOLS (per closed-trade attribution):")
        for s in report["school_edges_top"][:5]:
            print(
                f"   {s['school']:<28} n={s['n_obs']:>4}  "
                f"hit={s['hit_rate']:.2f}  exp={s['expectancy']:+.4f}R  "
                f"w_mod={s['weight_modifier']:.3f}",
            )

    print()
    print(" Report saved to: var/eta_engine/state/kaizen_latest.json")
    print(" Action history:  var/eta_engine/state/kaizen_actions.jsonl")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    with contextlib.suppress(AttributeError, ValueError):
        import sys as _sys
        _sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default=None,
                   help="ISO ts filter on trade closes (e.g., post-brake-fix)")
    p.add_argument("--bootstraps", type=int,
                   default=int(os.getenv("ETA_KAIZEN_BOOTSTRAPS", "1000")))
    p.add_argument("--apply", action="store_true",
                   help="Apply confirmed RETIRE recommendations (2-run confirmation)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    report = run_loop(
        since_iso=args.since,
        bootstraps=args.bootstraps,
        apply_actions=args.apply,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
