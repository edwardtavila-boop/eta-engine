"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_demotion_gate
==============================================================
Formal demotion gate for the diamond fleet (wave-19).

Why this exists
---------------
Wave-6 codified the PROMOTION gate (input criteria for adding bots to
DIAMOND_BOTS).  But the diamond fleet has NEVER had a formal codified
DEMOTION gate — the protection doc says "operator-only retirement"
which leaves the criteria undocumented in code.

This script produces a structured DEMOTE / WATCH / KEEP verdict per
diamond, so the operator's retire decision has the same objective
discipline as the promote decision.

What it checks (all R-multiple basis, dual-source dedup'd)
-----------------------------------------------------------

Hard criteria for DEMOTE verdict (any one fails = DEMOTE_CANDIDATE):
  D1_TEMPORAL_DECAY     — n_calendar_days_active < 1 in last 14 days
                          (bot has gone silent, strategy is dormant)
  D2_R_BLEED            — cum_r over last 50 trades < -5R
                          (strategy has decayed below the watchdog floor)
  D3_FEED_CORRUPTED     — diamond_feed_sanity_audit verdict = FLAGGED
                          with STUCK_PRICE for 3+ consecutive runs

Soft criteria for WATCH verdict (any one = operator-attention):
  W1_LOW_SAMPLE_GROWTH  — n_new_trades_last_14d < 10
  W2_R_DRIFT            — avg_r over last 50 trades < +0.05R
                          (edge has eroded below confidence threshold)
  W3_DUAL_BASIS_MIXED   — watchdog dual-basis classification is
                          CRITICAL on R OR has stayed WARN for 3+ runs

KEEP verdict otherwise — diamond is performing within tolerance.

Hard rule
---------
This is a RECOMMENDATION gate.  It does NOT auto-demote.  The
DIAMOND_BOTS set is mutated only by deliberate operator code-edit
(per CLAUDE.md hard rule #1 + the 3-layer protection doc).  This
script gives the operator structured, repeatable input for that
decision.

Output
------
- stdout report (per-diamond verdict + rationale)
- ``var/eta_engine/state/diamond_demotion_gate_latest.json`` receipt
- exit 0 always (advisory, not blocking)

Run
---
::

    python -m eta_engine.scripts.diamond_demotion_gate
    python -m eta_engine.scripts.diamond_demotion_gate --json
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
TRADE_CLOSES_CANONICAL = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"
TRADE_CLOSES_LEGACY = (
    WORKSPACE_ROOT
    / "eta_engine"
    / "state"  # HISTORICAL-PATH-OK
    / "jarvis_intel"
    / "trade_closes.jsonl"
)
OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_demotion_gate_latest.json"

#: Recent-window definitions
RECENT_TRADES_WINDOW = 50  # trades for R_BLEED + R_DRIFT
RECENT_DAYS_WINDOW = 14  # calendar days for TEMPORAL_DECAY + LOW_SAMPLE_GROWTH

#: Hard demote thresholds
D1_MIN_ACTIVE_DAYS = 1  # ≥1 day of activity in last 14 days
D2_R_BLEED_FLOOR = -5.0  # cum_r over last 50 trades must be ≥ -5R

#: Soft watch thresholds
W1_MIN_NEW_TRADES_14D = 10  # ≥10 trades in last 14 days
W2_R_DRIFT_FLOOR = 0.05  # avg_r over last 50 trades ≥ +0.05R


@dataclass
class DemotionScorecard:
    bot_id: str
    n_total: int = 0
    n_recent_trades: int = 0  # last RECENT_TRADES_WINDOW
    cum_r_recent: float = 0.0
    avg_r_recent: float = 0.0
    n_new_trades_14d: int = 0
    n_active_days_14d: int = 0
    hard_failures: list[str] = field(default_factory=list)
    soft_failures: list[str] = field(default_factory=list)
    verdict: str = "KEEP"
    rationale: str = ""


def _read_trades_dual_source() -> list[dict[str, Any]]:
    """Read both canonical and legacy archives, dedupe + filter by data_source.

    Wave-25 (2026-05-13): delegated to ``closed_trade_ledger.load_close_records``
    which classifies records as live/paper/backtest/historical_unverified/
    test_fixture and filters out the latter two by default. Without this
    filter the legacy archive injected ~43k backtest emissions.
    """
    from eta_engine.scripts.closed_trade_ledger import (
        DEFAULT_PRODUCTION_DATA_SOURCES,
        load_close_records,
    )

    return load_close_records(
        source_paths=[TRADE_CLOSES_CANONICAL, TRADE_CLOSES_LEGACY],
        data_sources=DEFAULT_PRODUCTION_DATA_SOURCES,
    )


def _parse_ts(ts_str: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(ts_str, str):
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _score_bot(bot_id: str, trades: list[dict[str, Any]], now_utc: datetime | None = None) -> DemotionScorecard:
    sc = DemotionScorecard(bot_id=bot_id, n_total=len(trades))
    if now_utc is None:
        now_utc = datetime.now(UTC)
    cutoff_14d = now_utc - timedelta(days=RECENT_DAYS_WINDOW)

    # Tick-leak guard (2026-05-13): diamond demotion gate is a destructive
    # decision-maker (decides which diamonds get downgraded). A single
    # tick-leak record (r=69 on mnq_futures_sage) would skew the R_DRIFT
    # and R_BLEED computations and could mis-demote a real diamond.
    from eta_engine.brain.jarvis_v3 import trade_close_sanitizer  # noqa: PLC0415

    # Parse timestamps + filter to last 14 days
    days_seen: set[str] = set()
    recent_trades_with_ts: list[tuple[datetime, float]] = []
    for t in trades:
        ts = _parse_ts(t.get("ts"))
        status, value = trade_close_sanitizer.classify(t)
        if status == "suspect" or status == "none" or value is None:
            continue
        r_val = float(value)
        if ts is None:
            continue
        if ts >= cutoff_14d:
            sc.n_new_trades_14d += 1
            days_seen.add(ts.date().isoformat())
        recent_trades_with_ts.append((ts, r_val))
    sc.n_active_days_14d = len(days_seen)

    # Last N trades by timestamp for R_BLEED + R_DRIFT
    recent_trades_with_ts.sort(key=lambda p: p[0], reverse=True)
    last_n = recent_trades_with_ts[:RECENT_TRADES_WINDOW]
    sc.n_recent_trades = len(last_n)
    if last_n:
        rs = [r for _, r in last_n]
        sc.cum_r_recent = round(sum(rs), 4)
        sc.avg_r_recent = round(sum(rs) / len(rs), 4)

    # ── Apply demotion criteria ───────────────────────────────────────
    hard: list[str] = []
    soft: list[str] = []

    # D1: TEMPORAL_DECAY
    if sc.n_active_days_14d < D1_MIN_ACTIVE_DAYS:
        hard.append(
            f"D1_TEMPORAL_DECAY (active days in last 14d = {sc.n_active_days_14d}, need >= {D1_MIN_ACTIVE_DAYS})",
        )

    # D2: R_BLEED
    if sc.n_recent_trades >= 10 and sc.cum_r_recent < D2_R_BLEED_FLOOR:
        hard.append(
            f"D2_R_BLEED (last {sc.n_recent_trades} trades cum_R = "
            f"{sc.cum_r_recent:+.2f}R, floor = {D2_R_BLEED_FLOOR}R)",
        )

    # W1: LOW_SAMPLE_GROWTH
    if sc.n_new_trades_14d < W1_MIN_NEW_TRADES_14D:
        soft.append(
            f"W1_LOW_SAMPLE_GROWTH (new trades in last 14d = {sc.n_new_trades_14d}, want >= {W1_MIN_NEW_TRADES_14D})",
        )

    # W2: R_DRIFT
    if sc.n_recent_trades >= 10 and sc.avg_r_recent < W2_R_DRIFT_FLOOR:
        soft.append(
            f"W2_R_DRIFT (last {sc.n_recent_trades} trades avg_R = "
            f"{sc.avg_r_recent:+.4f}, floor = +{W2_R_DRIFT_FLOOR})",
        )

    sc.hard_failures = hard
    sc.soft_failures = soft

    if hard:
        sc.verdict = "DEMOTE_CANDIDATE"
        sc.rationale = f"{len(hard)} hard demotion criteria failed: {'; '.join(hard)}"
    elif soft:
        sc.verdict = "WATCH"
        sc.rationale = f"{len(soft)} soft criteria warrant attention: {'; '.join(soft)}"
    else:
        sc.verdict = "KEEP"
        sc.rationale = (
            f"performing within tolerance — "
            f"n_recent={sc.n_recent_trades}, "
            f"cum_R_recent={sc.cum_r_recent:+.2f}, "
            f"new_in_14d={sc.n_new_trades_14d}, "
            f"active_days_14d={sc.n_active_days_14d}"
        )
    return sc


def run() -> dict[str, Any]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        DIAMOND_BOTS,
    )

    trades = _read_trades_dual_source()
    by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        bid = t.get("bot_id")
        if bid in DIAMOND_BOTS:
            by_bot[bid].append(t)

    scorecards = [_score_bot(bot_id, by_bot.get(bot_id, [])) for bot_id in sorted(DIAMOND_BOTS)]

    counts: dict[str, int] = defaultdict(int)
    for sc in scorecards:
        counts[sc.verdict] += 1

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_diamonds": len(scorecards),
        "verdict_counts": dict(counts),
        "recent_trades_window": RECENT_TRADES_WINDOW,
        "recent_days_window": RECENT_DAYS_WINDOW,
        "thresholds": {
            "D1_min_active_days": D1_MIN_ACTIVE_DAYS,
            "D2_r_bleed_floor": D2_R_BLEED_FLOOR,
            "W1_min_new_trades_14d": W1_MIN_NEW_TRADES_14D,
            "W2_r_drift_floor": W2_R_DRIFT_FLOOR,
        },
        "scorecards": [asdict(sc) for sc in scorecards],
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
    print("=" * 130)
    print(
        f" DIAMOND DEMOTION GATE  ({summary['ts']})  "
        + ", ".join(f"{k}={v}" for k, v in summary["verdict_counts"].items()),
    )
    print("=" * 130)
    print(
        f" {'bot':25s} {'verdict':20s} {'n_recent':>9s} {'cum_R_50':>9s} "
        f"{'avg_R_50':>9s} {'new_14d':>8s} {'days_14d':>9s}",
    )
    print("-" * 130)
    for sc in summary["scorecards"]:
        print(
            f" {sc['bot_id']:25s} {sc['verdict']:20s} "
            f"{sc['n_recent_trades']:>9d} "
            f"{sc['cum_r_recent']:>+9.2f} "
            f"{sc['avg_r_recent']:>+9.4f} "
            f"{sc['n_new_trades_14d']:>8d} "
            f"{sc['n_active_days_14d']:>9d}",
        )
        for f_ in sc.get("hard_failures") or []:
            print(f"   ✗ HARD: {f_}")
        for f_ in sc.get("soft_failures") or []:
            print(f"   ⚠ SOFT: {f_}")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
