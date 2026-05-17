"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_feed_sanity_audit
==================================================================
Per-diamond data-feed sanity audit.

Why this exists (wave-17 kaizen)
--------------------------------
The MBT family (mbt_sweep_reclaim, mbt_overnight_gap, mbt_rth_orb,
mbt_funding_basis) was discovered to have fill_price=5.0 and
realized_pnl=0 across 374 trades. Real Bitcoin futures should fill at
50k+; 5.00 is a placeholder value indicating a missing or mis-routed
market data subscription on IBKR.

This audit catches that class of bug at the FEED layer, BEFORE the
broken data pollutes the diamond fleet's trade ledger and BEFORE the
operator promotes a bot whose strategy edge cannot be evaluated in
USD terms.

Important context
-----------------
The fleet uses SCALED fill prices on most instruments (likely a
dev/paper-feed normalization). Absolute price ranges aren't reliable
verdicts because most fills are scaled — we'd flag the entire fleet
on absolute checks. The real signals are:

  1. STUCK_PRICE       — fill_price has near-zero variance (placeholder
                         feed returning the same value every trade)
  2. ZERO_PNL_ACTIVITY — n>=10 closed trades but realized_pnl=0 across
                         ALL of them (broken writer or feed)
  3. MISSING_PNL_FIELD — extra.realized_pnl absent from majority of
                         records (writer regression)
  4. MISSING_SIDE_FIELD — extra.side absent from majority (wave-10
                         direction-derivation will misclassify)

R-multiples remain trustworthy regardless of price scaling (entry +
stop both scale by the same factor, so the ratio is invariant). The
strategy-edge evaluation continues to use R-basis. This audit gates
USD-basis trustworthiness, NOT R-basis.

Verdicts
--------
  CLEAN              — no data-quality flags
  FLAGGED            — at least one flag
  INSUFFICIENT_DATA  — fewer than SAMPLE_THRESHOLD records

Output
------
- stdout: per-diamond verdict report
- var/eta_engine/state/diamond_feed_sanity_audit_latest.json
- exit 2 if any diamond is FLAGGED

Run
---
::

    python -m eta_engine.scripts.diamond_feed_sanity_audit
    python -m eta_engine.scripts.diamond_feed_sanity_audit --json
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

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
TRADE_CLOSES_CANONICAL = workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH
TRADE_CLOSES_LEGACY = workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH
OUT_LATEST = workspace_roots.ETA_DIAMOND_FEED_SANITY_AUDIT_PATH

#: Below this trade count, we don't have enough samples to detect
#: a pattern. Below SAMPLE_THRESHOLD records the verdict is INSUFFICIENT_DATA.
SAMPLE_THRESHOLD = 10

#: STUCK_PRICE detection: if (max - min) / median < this fraction,
#: the fill_price feed is essentially returning the same value across
#: trades. Real markets move enough that even minute-bar fills should
#: span at least a few percent over hundreds of trades.
STUCK_PRICE_RANGE_FRACTION = 0.02  # < 2% range across all trades = stuck

#: STUCK_PRICE guard: a narrow-window scalping strategy can legitimately
#: produce a tight price range, but it will still produce many DISTINCT
#: fill prices (one per fill). A truly stuck feed returns the same
#: handful of values repeatedly. Require BOTH narrow range AND low
#: distinct fraction before flagging. The MBT pollution case had
#: ~5 unique prices across 374 trades (~1.3% distinct).
STUCK_PRICE_DISTINCT_FRACTION_FLOOR = 0.5  # >=50% unique = not stuck

#: Schema cutoff: records before this timestamp predate wave-10's
#: extra.{realized_pnl,side,fill_price} fields. Excluding them from
#: the MISSING_* checks prevents legitimate historical records from
#: being scored as writer regressions. (Wave-10 = direction
#: stratification, landed ~2026-05-05.)
LEGACY_SCHEMA_CUTOFF_TS = "2026-05-05T00:00:00+00:00"


@dataclass
class FeedSanityScorecard:
    bot_id: str
    n_records: int = 0
    n_modern_records: int = 0
    n_with_fill_price: int = 0
    n_with_pnl: int = 0
    n_with_side: int = 0
    n_zero_pnl: int = 0
    n_distinct_fill_prices: int = 0
    fill_price_min: float | None = None
    fill_price_max: float | None = None
    fill_price_median: float | None = None
    fill_price_range_fraction: float | None = None
    fill_price_distinct_fraction: float | None = None
    verdict: str = "INSUFFICIENT_DATA"
    flags: list[str] = field(default_factory=list)
    rationale: str = ""


# ────────────────────────────────────────────────────────────────────
# IO + helpers
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


def _score_bot(bot_id: str, trades: list[dict[str, Any]]) -> FeedSanityScorecard:
    sc = FeedSanityScorecard(bot_id=bot_id, n_records=len(trades))

    fill_prices: list[float] = []
    for t in trades:
        # Only score records from the modern schema era for the
        # MISSING_* writer-regression checks. Pre-wave-10 records lack
        # the extra.* fields by design, not by writer bug.
        ts = str(t.get("ts") or "")
        is_modern = ts >= LEGACY_SCHEMA_CUTOFF_TS
        if is_modern:
            sc.n_modern_records += 1
        extra = t.get("extra") or {}
        if not isinstance(extra, dict):
            continue
        fp = extra.get("fill_price")
        if fp is not None:
            try:
                fill_prices.append(float(fp))
                sc.n_with_fill_price += 1
            except (TypeError, ValueError):
                pass
        pnl = extra.get("realized_pnl")
        if pnl is not None:
            sc.n_with_pnl += 1
            try:
                if float(pnl) == 0:
                    sc.n_zero_pnl += 1
            except (TypeError, ValueError):
                pass
        if extra.get("side"):
            sc.n_with_side += 1

    if fill_prices:
        sc.fill_price_min = round(min(fill_prices), 4)
        sc.fill_price_max = round(max(fill_prices), 4)
        sorted_fps = sorted(fill_prices)
        sc.fill_price_median = round(
            sorted_fps[len(sorted_fps) // 2],
            4,
        )
        # Range fraction: (max - min) / median.  A real market shows
        # at least a few percent of variation across hundreds of trades;
        # a stuck/placeholder feed shows zero or tiny variation.
        if sc.fill_price_median and sc.fill_price_median != 0:
            sc.fill_price_range_fraction = round(
                (sc.fill_price_max - sc.fill_price_min) / sc.fill_price_median,
                4,
            )
        # Distinct-fraction: a stuck feed returns the same few values
        # repeatedly (low distinct fraction). A narrow-window scalper
        # returns many distinct values within a tight range (high
        # distinct fraction). Use this to disambiguate.
        sc.n_distinct_fill_prices = len(set(fill_prices))
        if sc.n_with_fill_price:
            sc.fill_price_distinct_fraction = round(
                sc.n_distinct_fill_prices / sc.n_with_fill_price,
                4,
            )

    if sc.n_records < SAMPLE_THRESHOLD:
        sc.verdict = "INSUFFICIENT_DATA"
        sc.rationale = f"only {sc.n_records} records (need >= {SAMPLE_THRESHOLD})"
        return sc

    # ── Apply checks ──────────────────────────────────────────────────
    flags: list[str] = []

    # STUCK_PRICE: fill_price has near-zero variance across trades
    # AND most prices are repeats (low distinct fraction). The
    # distinct guard prevents narrow-window scalping strategies from
    # being false-flagged when they trade in a tight price band but
    # still produce unique fills.
    if (
        sc.fill_price_range_fraction is not None
        and sc.n_with_fill_price >= SAMPLE_THRESHOLD
        and sc.fill_price_range_fraction < STUCK_PRICE_RANGE_FRACTION
        and (
            sc.fill_price_distinct_fraction is not None
            and sc.fill_price_distinct_fraction < STUCK_PRICE_DISTINCT_FRACTION_FLOOR
        )
    ):
        flags.append(
            f"STUCK_PRICE (fill_price range = "
            f"{sc.fill_price_range_fraction * 100:.2f}% of median, "
            f"{sc.n_distinct_fill_prices}/{sc.n_with_fill_price} distinct "
            f"({sc.fill_price_distinct_fraction * 100:.0f}%) — feed appears "
            "placeholder/static)",
        )

    # ZERO_PNL_ACTIVITY: ALL records have zero PnL despite trading
    if sc.n_with_pnl >= SAMPLE_THRESHOLD and sc.n_zero_pnl == sc.n_with_pnl:
        flags.append(
            f"ZERO_PNL_ACTIVITY ({sc.n_with_pnl} closed trades, "
            "all realized_pnl=0 — broken writer or zero price action)",
        )

    # MISSING_PNL_FIELD: majority of MODERN-ERA records lack the field.
    # Pre-wave-10 records (ts < LEGACY_SCHEMA_CUTOFF_TS) are excluded
    # from the denominator since the schema did not include extra.* by
    # design back then.
    if sc.n_modern_records >= SAMPLE_THRESHOLD and sc.n_with_pnl < sc.n_modern_records / 2:
        flags.append(
            f"MISSING_PNL_FIELD ({sc.n_with_pnl}/{sc.n_modern_records} modern-era "
            "records have extra.realized_pnl)",
        )

    # MISSING_SIDE_FIELD: writer skipping side field on modern-era records
    if sc.n_modern_records >= SAMPLE_THRESHOLD and sc.n_with_side < sc.n_modern_records / 2:
        flags.append(
            f"MISSING_SIDE_FIELD ({sc.n_with_side}/{sc.n_modern_records} modern-era "
            "records have extra.side — wave-10 direction derivation will misfire)",
        )

    sc.flags = flags
    sc.verdict = "FLAGGED" if flags else "CLEAN"
    if flags:
        sc.rationale = "; ".join(flags)
    elif sc.fill_price_range_fraction is not None:
        sc.rationale = (
            f"all checks pass — {sc.n_records} records, "
            f"fill_price ${sc.fill_price_min}-${sc.fill_price_max} "
            f"(range {sc.fill_price_range_fraction:.2%} of median), "
            f"{sc.n_zero_pnl}/{sc.n_with_pnl} zero-PnL"
        )
    else:
        sc.rationale = (
            f"all checks pass — {sc.n_records} records "
            f"({sc.n_modern_records} modern-era), no fill_price data"
        )
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

    # Also include MBT family bots (they're not in DIAMOND_BOTS but
    # they're the prime example of feed-sanity issues — we want them
    # in the report so the operator can see when the data catches up).
    extra_targets = {
        "mbt_sweep_reclaim",
        "mbt_overnight_gap",
        "mbt_rth_orb",
        "mbt_funding_basis",
    }
    for t in trades:
        bid = t.get("bot_id")
        if bid in extra_targets:
            by_bot[bid].append(t)

    scorecards: list[FeedSanityScorecard] = [_score_bot(bot_id, by_bot.get(bot_id, [])) for bot_id in sorted(by_bot)]

    counts: dict[str, int] = defaultdict(int)
    for sc in scorecards:
        counts[sc.verdict] += 1

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_audited": len(scorecards),
        "verdict_counts": dict(counts),
        "sample_threshold": SAMPLE_THRESHOLD,
        "stuck_price_range_fraction": STUCK_PRICE_RANGE_FRACTION,
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
        f" DIAMOND FEED SANITY AUDIT  ({summary['ts']})  "
        + ", ".join(f"{k}={v}" for k, v in summary["verdict_counts"].items()),
    )
    print("=" * 130)
    print(
        f" {'bot':25s} {'verdict':18s} {'n':>5s}  {'fill_price_range':>22s}  rationale",
    )
    print("-" * 130)
    for sc in summary["scorecards"]:
        fp_min = sc.get("fill_price_min")
        fp_max = sc.get("fill_price_max")
        fp_s = f"${fp_min:>8.2f}..${fp_max:<8.2f}" if fp_min is not None and fp_max is not None else f"{'—':>22s}"
        print(
            f" {sc['bot_id']:25s} {sc['verdict']:18s} {sc['n_records']:>5d}  {fp_s}  {sc['rationale'][:60]}",
        )
        if sc.get("flags"):
            for f in sc["flags"]:
                print(f"     ↳ {f}")
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
    if summary["verdict_counts"].get("FLAGGED", 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
