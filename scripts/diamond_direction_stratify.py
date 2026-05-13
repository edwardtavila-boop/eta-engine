"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_direction_stratify
=================================================================
Per-direction R-multiple stratification for the diamond fleet.

Why this exists
---------------
Wave-10 surfaced that the ``direction`` field in trade_closes.jsonl
was stuck on "long" across all 43,450 historical records — but the
``extra.side`` field correctly captured BUY/SELL. The diamonds are
aggressively bidirectional (~50% short on average).

Every prior stratification that bucketed by ``direction`` saw a single
'long' bucket and missed the real signal: whether a diamond earns its
edge primarily long, primarily short, or evenly.

This script does the analysis the broken-direction pipeline couldn't:
read the canonical dual-source trade archive, DERIVE direction from
``extra.side`` (BUY -> long, SELL -> short), and surface per-direction
R-stats per diamond.

Asymmetric edge is actionable kaizen:
  - If a diamond is +R on shorts and ~0 on longs: filter long-side
    signals (or reduce long-side sizing)
  - If both sides are +R: keep the strategy symmetric
  - If one side is negative: investigate the strategy mechanic on that
    side (likely a stop/target asymmetry)

Output
------
- stdout: per-bot long-vs-short scorecard with asymmetry verdict
- ``var/eta_engine/state/diamond_direction_stratify_latest.json``

Verdict bands per bot:
  SYMMETRIC          — |long_avg_r - short_avg_r| < 0.10R AND both > 0
  LONG_DOMINANT      — long_avg_r >= short_avg_r + 0.10R AND short_avg_r > 0
  SHORT_DOMINANT     — short_avg_r >= long_avg_r + 0.10R AND long_avg_r > 0
  LONG_ONLY_EDGE     — long_avg_r > 0 and short_avg_r <= 0
  SHORT_ONLY_EDGE    — short_avg_r > 0 and long_avg_r <= 0
  BIDIRECTIONAL_LOSS — both sides <= 0 (strategy not working)
  INSUFFICIENT_DATA  — fewer than MIN_PER_DIRECTION samples in one side

Run
---
::

    python -m eta_engine.scripts.diamond_direction_stratify
    python -m eta_engine.scripts.diamond_direction_stratify --json
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
OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_direction_stratify_latest.json"

#: Minimum trades per direction before the asymmetry verdict is trusted.
#: With fewer, sampling noise dominates the comparison.
MIN_PER_DIRECTION = 10

#: How much per-trade R-difference between long and short to call
#: it "dominance". 0.10R is roughly 25% of a typical strong-bot
#: per-trade edge (e.g. eur_sweep's +0.46R/trade).
DOMINANCE_THRESHOLD_R = 0.10


@dataclass
class DirectionSlice:
    n: int = 0
    cum_r: float = 0.0
    avg_r: float | None = None
    win_rate_pct: float | None = None


@dataclass
class DirectionScorecard:
    bot_id: str
    n_total: int = 0
    n_long: int = 0
    n_short: int = 0
    n_unknown: int = 0  # records where side is missing/malformed
    long: DirectionSlice = field(default_factory=DirectionSlice)
    short: DirectionSlice = field(default_factory=DirectionSlice)
    asymmetry_r: float | None = None  # long_avg_r - short_avg_r
    verdict: str = "INSUFFICIENT_DATA"
    rationale: str = ""


# ────────────────────────────────────────────────────────────────────
# IO
# ────────────────────────────────────────────────────────────────────


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


def derive_direction(rec: dict[str, Any]) -> str:
    """Return the canonical direction for a trade-close record.

    Pre-wave-10 the ``direction`` field in trade_closes.jsonl was
    hard-coded to "long" by the supervisor (BotInstance.direction
    default that was never updated per-trade). The truth lives in
    ``extra.side`` (BUY/SELL).

    Order of preference:
      1. ``extra.side`` mapped BUY -> long / SELL -> short
      2. Top-level ``side`` (some pipelines hoist it)
      3. ``direction`` field (post-wave-10 records have correct values)
      4. "unknown" — record has no usable direction signal
    """
    extra = rec.get("extra") or {}
    side = None
    if isinstance(extra, dict):
        side = extra.get("side")
    if side is None:
        side = rec.get("side")
    if isinstance(side, str):
        s = side.strip().upper()
        if s == "BUY":
            return "long"
        if s == "SELL":
            return "short"
    # Fall back to direction field (post-wave-10 will be correct)
    direction = rec.get("direction")
    if isinstance(direction, str):
        d = direction.strip().lower()
        if d in ("long", "short"):
            return d
    return "unknown"


# ────────────────────────────────────────────────────────────────────
# Scoring
# ────────────────────────────────────────────────────────────────────


def _build_slice(rs: list[float]) -> DirectionSlice:
    sl = DirectionSlice(n=len(rs))
    if not rs:
        return sl
    sl.cum_r = round(sum(rs), 4)
    sl.avg_r = round(sum(rs) / len(rs), 4)
    sl.win_rate_pct = round(
        100.0 * sum(1 for r in rs if r > 0) / len(rs),
        2,
    )
    return sl


def _classify(sc: DirectionScorecard) -> None:
    """Compute the verdict + rationale from the slice stats."""
    if sc.n_long < MIN_PER_DIRECTION or sc.n_short < MIN_PER_DIRECTION:
        sc.verdict = "INSUFFICIENT_DATA"
        sc.rationale = f"need >= {MIN_PER_DIRECTION} per direction; have long={sc.n_long}, short={sc.n_short}"
        return

    long_avg = sc.long.avg_r if sc.long.avg_r is not None else 0.0
    short_avg = sc.short.avg_r if sc.short.avg_r is not None else 0.0
    sc.asymmetry_r = round(long_avg - short_avg, 4)

    long_positive = long_avg > 0
    short_positive = short_avg > 0

    if not long_positive and not short_positive:
        sc.verdict = "BIDIRECTIONAL_LOSS"
        sc.rationale = (
            f"both sides negative: long_avg={long_avg:+.3f}R, short_avg={short_avg:+.3f}R — strategy not working"
        )
        return
    if long_positive and not short_positive:
        sc.verdict = "LONG_ONLY_EDGE"
        sc.rationale = (
            f"long edge ({long_avg:+.3f}R) carries the bot; "
            f"shorts are net-negative ({short_avg:+.3f}R) — consider "
            "filtering or de-sizing shorts"
        )
        return
    if short_positive and not long_positive:
        sc.verdict = "SHORT_ONLY_EDGE"
        sc.rationale = (
            f"short edge ({short_avg:+.3f}R) carries the bot; "
            f"longs are net-negative ({long_avg:+.3f}R) — consider "
            "filtering or de-sizing longs"
        )
        return
    # Both positive
    diff = abs(sc.asymmetry_r)
    if diff < DOMINANCE_THRESHOLD_R:
        sc.verdict = "SYMMETRIC"
        sc.rationale = (
            f"both sides similarly profitable: long={long_avg:+.3f}R, short={short_avg:+.3f}R, |diff|={diff:.3f}R"
        )
        return
    if long_avg > short_avg:
        sc.verdict = "LONG_DOMINANT"
        sc.rationale = (
            f"longs {long_avg:+.3f}R vs shorts {short_avg:+.3f}R "
            f"(long advantage = {diff:.3f}R) — operator could lean "
            "into long-side sizing"
        )
    else:
        sc.verdict = "SHORT_DOMINANT"
        sc.rationale = (
            f"shorts {short_avg:+.3f}R vs longs {long_avg:+.3f}R "
            f"(short advantage = {diff:.3f}R) — operator could lean "
            "into short-side sizing"
        )


def _score_bot(bot_id: str, trades: list[dict[str, Any]]) -> DirectionScorecard:
    sc = DirectionScorecard(bot_id=bot_id, n_total=len(trades))
    long_rs: list[float] = []
    short_rs: list[float] = []
    for rec in trades:
        r = rec.get("realized_r")
        try:
            r_val = float(r) if r is not None else None
        except (TypeError, ValueError):
            r_val = None
        if r_val is None:
            continue
        d = derive_direction(rec)
        if d == "long":
            long_rs.append(r_val)
        elif d == "short":
            short_rs.append(r_val)
        else:
            sc.n_unknown += 1

    sc.n_long = len(long_rs)
    sc.n_short = len(short_rs)
    sc.long = _build_slice(long_rs)
    sc.short = _build_slice(short_rs)
    _classify(sc)
    return sc


# ────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────


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

    scorecards: list[DirectionScorecard] = [
        _score_bot(bot_id, by_bot.get(bot_id, [])) for bot_id in sorted(DIAMOND_BOTS)
    ]

    counts: dict[str, int] = defaultdict(int)
    for sc in scorecards:
        counts[sc.verdict] += 1

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_diamonds": len(scorecards),
        "verdict_counts": dict(counts),
        "min_per_direction": MIN_PER_DIRECTION,
        "dominance_threshold_r": DOMINANCE_THRESHOLD_R,
        "statuses": [asdict(sc) for sc in scorecards],
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
        f" DIAMOND DIRECTION STRATIFICATION  ({summary['ts']})  "
        + ", ".join(f"{k}={v}" for k, v in summary["verdict_counts"].items()),
    )
    print("=" * 130)
    print(
        f" {'bot':25s} {'verdict':22s} | "
        f"{'long n':>6s} {'long_avg':>9s} {'long_wr':>7s} | "
        f"{'short n':>7s} {'short_avg':>10s} {'short_wr':>8s} | "
        f"{'asym':>7s}",
    )
    print("-" * 130)
    for sc in summary["statuses"]:
        long_avg = sc["long"]["avg_r"]
        long_wr = sc["long"]["win_rate_pct"]
        short_avg = sc["short"]["avg_r"]
        short_wr = sc["short"]["win_rate_pct"]
        asym = sc.get("asymmetry_r")
        long_avg_s = f"{long_avg:>+9.3f}" if long_avg is not None else f"{'—':>9s}"
        long_wr_s = f"{long_wr:>6.1f}%" if long_wr is not None else f"{'—':>7s}"
        short_avg_s = f"{short_avg:>+10.3f}" if short_avg is not None else f"{'—':>10s}"
        short_wr_s = f"{short_wr:>7.1f}%" if short_wr is not None else f"{'—':>8s}"
        asym_s = f"{asym:>+7.3f}" if asym is not None else f"{'—':>7s}"
        print(
            f" {sc['bot_id']:25s} {sc['verdict']:22s} | "
            f"{sc['n_long']:>6d} {long_avg_s} {long_wr_s} | "
            f"{sc['n_short']:>7d} {short_avg_s} {short_wr_s} | "
            f"{asym_s}",
        )
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
