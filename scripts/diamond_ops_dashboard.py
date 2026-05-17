"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_ops_dashboard
==============================================================
Unified diamond-program status dashboard.

Why this exists
---------------
Across waves 6-12 we built four independent audit scripts:
  - diamond_promotion_gate           (input gate)
  - diamond_sizing_audit             (middle gate)
  - diamond_falsification_watchdog   (output gate, USD + R dual-basis)
  - diamond_direction_stratify       (per-side R-edge analyzer)

Each is invaluable on its own.  But the operator was running four
scripts and mentally joining their outputs to answer "what's the state
of diamond N?"  This dashboard does the join in code: ONE script
produces a per-bot synthesis row joining all four signals plus
suggested operator actions.

Per-diamond synthesis row
-------------------------
For each diamond in DIAMOND_BOTS, the dashboard surfaces:

  - enrollment status (DIAMOND_BOTS membership — should be True)
  - promotion gate verdict (PROMOTE / NEEDS_MORE_DATA / REJECT)
  - sizing audit verdict (SIZING_OK / TIGHT / FRAGILE / BREACHED)
  - watchdog classification (HEALTHY / WATCH / WARN / CRITICAL)
  - direction stratify verdict (SYMMETRIC / LONG_DOMINANT / SHORT_DOMINANT
    / LONG_ONLY_EDGE / SHORT_ONLY_EDGE / BIDIRECTIONAL_LOSS)
  - lifetime cum_R + cum_USD
  - synthesized priority + recommended action

Priority bands (worst-first ordering)
-------------------------------------
  P0_CRITICAL       — watchdog CRITICAL or sizing BREACHED.  Operator
                       must review immediately.
  P1_REVIEW         — sizing FRAGILE or watchdog WARN.
  P2_MONITOR        — sizing TIGHT or watchdog WATCH; direction
                       asymmetry ≥ DOMINANT_R.
  P3_OK             — all green, no action needed.
  P4_INSUFFICIENT_DATA — too thin to evaluate; let trades accumulate.

Output
------
- stdout: one synthesis row per diamond, worst-first
- ``var/eta_engine/state/diamond_ops_dashboard_latest.json``
- exit 0 if no P0_CRITICAL bots; exit 2 otherwise

Run
---
::

    python -m eta_engine.scripts.diamond_ops_dashboard
    python -m eta_engine.scripts.diamond_ops_dashboard --json
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots
from eta_engine.scripts.retune_advisory_cache import build_retune_advisory, summarize_active_experiment

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
OUT_LATEST = workspace_roots.ETA_DIAMOND_OPS_DASHBOARD_PATH
PUBLIC_BROKER_CLOSE_CACHE = workspace_roots.ETA_PUBLIC_BROKER_CLOSE_TRUTH_CACHE_PATH
HEALTH_DIR = workspace_roots.ETA_RUNTIME_HEALTH_DIR


def _console_help_description(text: str | None) -> str:
    """Return argparse help text that is safe on Windows cp1252 consoles."""
    return (text or "").encode("ascii", "replace").decode("ascii")


def _console_ascii(text: str | None) -> str:
    """Return runtime console text that is safe on Windows cp1252 consoles."""
    normalized = text or ""
    replacements = {
        "→": "->",
        "↳": "->",
        "—": "-",
        "–": "-",
        "≥": ">=",
        "≤": "<=",
        "…": "...",
    }
    for original, replacement in replacements.items():
        normalized = normalized.replace(original, replacement)
    return normalized.encode("ascii", "replace").decode("ascii")


@dataclass
class DiamondSynthesis:
    bot_id: str
    enrolled: bool = False
    cum_r: float | None = None
    cum_usd: float | None = None
    n_trades: int | None = None
    promotion_verdict: str | None = None
    promotion_rationale: str | None = None
    broker_total_realized_pnl: float | None = None
    broker_profit_factor: float | None = None
    broker_trade_count: int | None = None
    broker_truth_source: str | None = None
    sizing_verdict: str | None = None
    watchdog_classification: str | None = None
    watchdog_classification_usd: str | None = None
    watchdog_classification_r: str | None = None
    direction_verdict: str | None = None
    direction_long_avg_r: float | None = None
    direction_short_avg_r: float | None = None
    feed_sanity_verdict: str | None = None
    feed_sanity_flags: list[str] = field(default_factory=list)
    priority: str = "P4_INSUFFICIENT_DATA"
    recommended_action: str = ""
    notes: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# Sub-audit invocation helpers
# ────────────────────────────────────────────────────────────────────


def _safe_run(audit_name: str, fn: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
    """Invoke a sub-audit, swallowing any exception so the dashboard
    never crashes if one audit fails. Returns {} on failure."""
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARN: {audit_name} failed: {exc}",
            file=sys.stderr,
        )
        return {}


def _run_promotion_gate() -> dict[str, dict[str, Any]]:
    """Returns {bot_id: candidate_dict} for the promotion gate.
    Includes existing diamonds (--include-existing semantics) so we
    can join verdicts onto the dashboard rows."""
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.scripts import diamond_promotion_gate as gate  # noqa: PLC0415

    summary = _safe_run("promotion_gate", gate.run, include_existing=True)
    return {c["bot_id"]: c for c in summary.get("candidates", [])}


def _run_sizing_audit() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.scripts import diamond_sizing_audit as sa  # noqa: PLC0415

    summary = _safe_run("sizing_audit", sa.run)
    return {s["bot_id"]: s for s in summary.get("statuses", [])}


def _run_watchdog() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.scripts import diamond_falsification_watchdog as wd  # noqa: PLC0415

    report = _safe_run("watchdog", wd.run_watchdog)
    return {s["bot_id"]: s for s in report.get("statuses", [])}


def _run_direction_stratify() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.scripts import diamond_direction_stratify as ds  # noqa: PLC0415

    summary = _safe_run("direction_stratify", ds.run)
    return {s["bot_id"]: s for s in summary.get("statuses", [])}


def _run_feed_sanity() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.scripts import diamond_feed_sanity_audit as fs  # noqa: PLC0415

    summary = _safe_run("feed_sanity", fs.run)
    return {s["bot_id"]: s for s in summary.get("scorecards", [])}


# ────────────────────────────────────────────────────────────────────
# Synthesis
# ────────────────────────────────────────────────────────────────────


# Rank order for worst-first sorting (higher = worse / shown first)
_PRIORITY_ORDER = {
    "P0_CRITICAL": 4,
    "P1_REVIEW": 3,
    "P2_MONITOR": 2,
    "P3_OK": 1,
    "P4_INSUFFICIENT_DATA": 0,
}


def _float_or_none(value: Any) -> float | None:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:  # noqa: ANN401
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_public_broker_close_cache() -> dict[str, Any]:
    payload = _load_json(PUBLIC_BROKER_CLOSE_CACHE)
    if payload.get("kind") != "eta_public_broker_close_truth_cache":
        return {}
    return payload


def _public_broker_close_focus_for_bot(bot_id: str, cache: dict[str, Any]) -> dict[str, Any]:
    if str(cache.get("focus_bot") or "") != bot_id:
        return {}
    return cache


def _synthesize(
    bot_id: str,
    enrolled: bool,
    promotion: dict[str, Any] | None,
    sizing: dict[str, Any] | None,
    watchdog: dict[str, Any] | None,
    direction: dict[str, Any] | None,
    feed_sanity: dict[str, Any] | None = None,
    public_broker_close: dict[str, Any] | None = None,
) -> DiamondSynthesis:
    syn = DiamondSynthesis(bot_id=bot_id, enrolled=enrolled)

    # Pull canonical metrics from the sizing audit (it has the cleanest
    # cum_r/cum_usd numbers; falls back to direction stratify or
    # watchdog if needed).
    if sizing is not None:
        syn.cum_r = sizing.get("cum_r")
        syn.cum_usd = sizing.get("cum_usd")
        syn.n_trades = sizing.get("n_trades_with_pnl")
        syn.sizing_verdict = sizing.get("verdict")
    if direction is not None:
        syn.direction_verdict = direction.get("verdict")
        if direction.get("long"):
            syn.direction_long_avg_r = direction["long"].get("avg_r")
        if direction.get("short"):
            syn.direction_short_avg_r = direction["short"].get("avg_r")
        if syn.n_trades is None:
            syn.n_trades = direction.get("n_total")
    if watchdog is not None:
        syn.watchdog_classification = watchdog.get("classification")
        syn.watchdog_classification_usd = watchdog.get("classification_usd")
        syn.watchdog_classification_r = watchdog.get("classification_r")
    if promotion is not None:
        syn.promotion_verdict = promotion.get("verdict")
        syn.promotion_rationale = promotion.get("rationale")
        syn.broker_total_realized_pnl = _float_or_none(promotion.get("total_realized_pnl"))
        syn.broker_profit_factor = _float_or_none(promotion.get("profit_factor"))
        try:
            syn.broker_trade_count = int(promotion.get("n_trades") or 0)
        except (TypeError, ValueError):
            syn.broker_trade_count = None
        syn.broker_truth_source = "promotion_gate"
    if feed_sanity is not None:
        syn.feed_sanity_verdict = feed_sanity.get("verdict")
        syn.feed_sanity_flags = list(feed_sanity.get("flags") or [])

    advisory_trade_count = _int_or_none((public_broker_close or {}).get("focus_closed_trade_count"))
    if advisory_trade_count is not None and advisory_trade_count > (syn.broker_trade_count or 0):
        syn.broker_trade_count = advisory_trade_count
        syn.broker_total_realized_pnl = _float_or_none((public_broker_close or {}).get("focus_total_realized_pnl"))
        syn.broker_profit_factor = _float_or_none((public_broker_close or {}).get("focus_profit_factor"))
        syn.broker_truth_source = "public_broker_close_truth_cache"
        syn.notes.append("broker proof refreshed from public advisory close cache")

    # ── Compute priority and action ──────────────────────────────────
    cls = syn.watchdog_classification or "INCONCLUSIVE"
    sz = syn.sizing_verdict or "INSUFFICIENT_DATA"
    fs = syn.feed_sanity_verdict or "INSUFFICIENT_DATA"
    broker_pnl_failed = syn.broker_total_realized_pnl is not None and syn.broker_total_realized_pnl <= 0
    broker_pf_failed = syn.broker_profit_factor is not None and syn.broker_profit_factor < 1.10

    if fs == "FLAGGED" and any("STUCK_PRICE" in f or "ZERO_PNL_ACTIVITY" in f for f in syn.feed_sanity_flags):
        # Wave-17: feed-sanity FLAGGED with stuck-price or zero-PnL is a
        # data-quality emergency — the bot's USD verdicts can't be trusted
        # at all until the feed is fixed.
        syn.priority = "P0_CRITICAL"
        flag_summary = "; ".join(syn.feed_sanity_flags)
        syn.recommended_action = (
            f"feed sanity FLAGGED ({flag_summary}) — broken data feed "
            "or writer; ops fix needed before USD verdicts are meaningful"
        )
    elif cls == "CRITICAL" or sz == "SIZING_BREACHED":
        syn.priority = "P0_CRITICAL"
        actions = []
        if cls == "CRITICAL":
            usd = syn.watchdog_classification_usd or "?"
            r = syn.watchdog_classification_r or "?"
            if usd == "CRITICAL" and r != "CRITICAL":
                actions.append(
                    "watchdog CRITICAL on USD basis only "
                    "(R-edge intact) → likely SIZING failure; "
                    "see sizing audit + cut risk_per_trade_pct",
                )
            elif r == "CRITICAL":
                actions.append(
                    "watchdog CRITICAL on R basis → strategy edge has decayed; consider operator retire decision",
                )
            else:
                actions.append("watchdog CRITICAL — operator review needed")
        if sz == "SIZING_BREACHED":
            actions.append(
                "sizing BREACHED — single stopout breaches USD floor; halve risk_per_trade_pct in the preset",
            )
        syn.recommended_action = " | ".join(actions)
    elif broker_pnl_failed or broker_pf_failed:
        syn.priority = "P1_REVIEW"
        pnl = syn.broker_total_realized_pnl
        pf = syn.broker_profit_factor
        pnl_s = f"${pnl:+.2f}" if pnl is not None else "unavailable"
        pf_s = f"{pf:.2f}" if pf is not None else "unavailable"
        syn.recommended_action = (
            f"broker proof failed (PnL {pnl_s}, PF {pf_s}) - keep paper-only; "
            "retune the setup or demote until real closes prove edge"
        )
    elif sz == "SIZING_FRAGILE" or cls == "WARN":
        syn.priority = "P1_REVIEW"
        if sz == "SIZING_FRAGILE":
            syn.recommended_action = (
                "sizing FRAGILE (1-2 stopouts to breach floor) — consider tightening risk_per_trade_pct in next cycle"
            )
        else:
            syn.recommended_action = (
                "watchdog WARN (under 20% buffer to floor) — monitor closely; review if WARN persists 3+ days"
            )
    elif sz == "SIZING_TIGHT" or cls == "WATCH":
        syn.priority = "P2_MONITOR"
        bits = []
        if sz == "SIZING_TIGHT":
            bits.append("sizing TIGHT (2-4 stopouts to breach)")
        if cls == "WATCH":
            bits.append("watchdog WATCH (under 50% buffer)")
        if syn.direction_verdict in (
            "LONG_DOMINANT",
            "SHORT_DOMINANT",
            "LONG_ONLY_EDGE",
            "SHORT_ONLY_EDGE",
        ):
            bits.append(
                f"direction asymmetry: {syn.direction_verdict}",
            )
        syn.recommended_action = " | ".join(bits)
    elif syn.direction_verdict in (
        "LONG_DOMINANT",
        "SHORT_DOMINANT",
    ):
        syn.priority = "P2_MONITOR"
        syn.recommended_action = (
            f"direction {syn.direction_verdict} — consider sizing the stronger side higher when n>=100 per side"
        )
    elif syn.direction_verdict in (
        "LONG_ONLY_EDGE",
        "SHORT_ONLY_EDGE",
    ):
        syn.priority = "P1_REVIEW"
        syn.recommended_action = (
            f"direction {syn.direction_verdict} — weak side is net negative; consider filtering it once n>=100 per side"
        )
    elif cls == "INCONCLUSIVE" and sz == "INSUFFICIENT_DATA":
        syn.priority = "P4_INSUFFICIENT_DATA"
        syn.recommended_action = "let trades accumulate; insufficient data for any verdict"
    else:
        syn.priority = "P3_OK"
        syn.recommended_action = "all green; no action"

    return syn


# ────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────


def run() -> dict[str, Any]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        DIAMOND_BOTS,
    )

    promo = _run_promotion_gate()
    sizing = _run_sizing_audit()
    watch = _run_watchdog()
    direction = _run_direction_stratify()
    feed = _run_feed_sanity()
    public_broker_close_cache = _load_public_broker_close_cache()
    retune_advisory = build_retune_advisory(HEALTH_DIR)
    active_experiment = (
        retune_advisory.get("active_experiment") if isinstance(retune_advisory.get("active_experiment"), dict) else None
    )
    active_experiment_summary = summarize_active_experiment(active_experiment)

    syntheses: list[DiamondSynthesis] = []
    for bot_id in sorted(DIAMOND_BOTS):
        syn = _synthesize(
            bot_id,
            enrolled=True,
            promotion=promo.get(bot_id),
            sizing=sizing.get(bot_id),
            watchdog=watch.get(bot_id),
            direction=direction.get(bot_id),
            feed_sanity=feed.get(bot_id),
            public_broker_close=_public_broker_close_focus_for_bot(bot_id, public_broker_close_cache),
        )
        syntheses.append(syn)

    # Sort worst-first
    syntheses.sort(
        key=lambda s: (
            -_PRIORITY_ORDER.get(s.priority, 0),
            s.bot_id,
        ),
    )

    counts: dict[str, int] = defaultdict(int)
    for s in syntheses:
        counts[s.priority] += 1

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_diamonds": len(syntheses),
        "priority_counts": dict(counts),
        "public_advisory_focus": {
            "focus_bot": retune_advisory.get("focus_bot"),
            "focus_closed_trade_count": retune_advisory.get("focus_closed_trade_count"),
            "focus_total_realized_pnl": retune_advisory.get("focus_total_realized_pnl"),
            "focus_profit_factor": retune_advisory.get("focus_profit_factor"),
            "broker_mtd_pnl": retune_advisory.get("broker_mtd_pnl"),
            "today_realized_pnl": retune_advisory.get("today_realized_pnl"),
            "active_experiment": active_experiment or {},
            "active_experiment_summary_line": (
                active_experiment_summary["headline"]
                if active_experiment_summary
                else ""
            ),
        }
        if retune_advisory.get("focus_bot")
        else {},
        "syntheses": [asdict(s) for s in syntheses],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict[str, Any]) -> None:
    print(_console_ascii("=" * 130))
    print(
        _console_ascii(
            f" DIAMOND OPS DASHBOARD  ({summary['ts']})  "
            + ", ".join(f"{k}={v}" for k, v in summary["priority_counts"].items()),
        ),
    )
    print(_console_ascii("=" * 130))
    print(
        _console_ascii(
            f" {'bot':25s} {'priority':22s} {'cum_R':>8s} {'cum_USD':>10s} | "
            f"{'promo':18s} {'sizing':18s} {'watchdog':10s} {'direction':22s}",
        ),
    )
    advisory = summary.get("public_advisory_focus") if isinstance(summary.get("public_advisory_focus"), dict) else {}
    if advisory.get("focus_bot"):
        pnl = _float_or_none(advisory.get("focus_total_realized_pnl"))
        pf = _float_or_none(advisory.get("focus_profit_factor"))
        trade_count = advisory.get("focus_closed_trade_count")
        pnl_text = f"${pnl:+,.2f}" if pnl is not None else "n/a"
        pf_text = f"{pf:.2f}" if pf is not None else "n/a"
        print(
            _console_ascii(
                " advisory focus: "
                f"{advisory.get('focus_bot')} closes={trade_count} pnl={pnl_text} pf={pf_text}"
            ),
        )
        experiment_summary_line = str(advisory.get("active_experiment_summary_line") or "")
        experiment = advisory.get("active_experiment") if isinstance(advisory.get("active_experiment"), dict) else {}
        if experiment_summary_line:
            print(_console_ascii(f" advisory experiment: {experiment_summary_line}"))
            experiment_summary = summarize_active_experiment(experiment)
            if experiment_summary:
                print(_console_ascii(f"                   advisory outcome: {experiment_summary['outcome_line']}"))
    print(_console_ascii("-" * 130))
    for s in summary["syntheses"]:
        cum_r = s.get("cum_r")
        cum_usd = s.get("cum_usd")
        cum_r_s = f"{cum_r:>+8.2f}" if cum_r is not None else f"{'-':>8s}"
        cum_usd_s = f"{cum_usd:>+10.0f}" if cum_usd is not None else f"{'-':>10s}"
        promo = (s.get("promotion_verdict") or "-")[:18]
        sizing = (s.get("sizing_verdict") or "-")[:18]
        watch = (s.get("watchdog_classification") or "-")[:10]
        direction = (s.get("direction_verdict") or "-")[:22]
        print(
            _console_ascii(
                f" {s['bot_id']:25s} {s['priority']:22s} {cum_r_s} {cum_usd_s} | "
                f"{promo:18s} {sizing:18s} {watch:10s} {direction:22s}",
            ),
        )
        if s.get("recommended_action"):
            print(_console_ascii(f"   -> {s['recommended_action']}"))
    print(_console_ascii(""))


def main() -> int:
    ap = argparse.ArgumentParser(description=_console_help_description(__doc__))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    if summary["priority_counts"].get("P0_CRITICAL", 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
