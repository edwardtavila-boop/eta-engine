"""Sage backtester (Wave-5 #16, 2026-04-27).

For each closed trade in the journal, replay the sage on the entry bar
and record (sage_conviction, alignment_score, realized_R) tuples. The
output dataset is the foundation for:
  * outcome-learned weight learning (EdgeTracker.observe)
  * sage-edge dashboards
  * v22 promotion-gate scoring

Usage::

    python -m eta_engine.brain.jarvis_v3.sage.backtester \\
        --journal state/burn_in/journal.sqlite \\
        --bars-source state/bars/         \\
        --output state/sage/backtest.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("sage_backtester")


def replay_one_trade(
    *,
    bars_at_entry: list[dict[str, Any]],
    side: str,
    realized_r: float,
    symbol: str = "",
) -> dict[str, Any]:
    """Run the sage on the bars window at entry, return summary dict."""
    from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage

    ctx = MarketContext(bars=bars_at_entry, side=side, symbol=symbol)
    # Don't use cache or apply edge weights during backtest -- we want
    # the deterministic baseline, and we're FEEDING the edge tracker.
    report = consult_sage(ctx, parallel=False, use_cache=False, apply_edge_weights=False)

    # Optionally feed the EdgeTracker so weights learn from each trade
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
        tracker = default_tracker()
        for school_name, verdict in report.per_school.items():
            tracker.observe(
                school=school_name,
                school_bias=verdict.bias.value,
                entry_side=side,
                realized_r=realized_r,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("edge tracker observe failed: %s", exc)

    return {
        "symbol": symbol,
        "side": side,
        "realized_r": realized_r,
        "composite_bias": report.composite_bias.value,
        "conviction": report.conviction,
        "alignment_score": report.alignment_score,
        "consensus_pct": report.consensus_pct,
        "schools_aligned": report.schools_aligned_with_entry,
        "schools_disagree": report.schools_disagreeing_with_entry,
    }


def replay_trades_iter(
    trades: list[dict[str, Any]],
    *,
    bars_lookup: callable | None = None,  # type: ignore[type-arg]
) -> list[dict[str, Any]]:
    """For each trade dict {symbol, side, entry_ts, realized_r}, run the
    sage on bars at entry. ``bars_lookup`` is a callable
    ``(symbol, entry_ts) -> list[bar dict]``.

    Returns a list of summary dicts (one per trade) suitable for analysis
    or dumping to JSON.
    """
    out: list[dict[str, Any]] = []
    for t in trades:
        if bars_lookup is None:
            logger.warning("no bars_lookup provided -- skipping %s", t.get("symbol"))
            continue
        try:
            bars = bars_lookup(t["symbol"], t["entry_ts"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("bars_lookup failed for %s: %s", t.get("symbol"), exc)
            continue
        if not bars or len(bars) < 30:
            continue
        out.append(replay_one_trade(
            bars_at_entry=bars,
            side=t["side"],
            realized_r=float(t["realized_r"]),
            symbol=t["symbol"],
        ))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", type=Path, required=False, default=None,
                   help="Decision journal SQLite file (or JSONL)")
    p.add_argument("--output", type=Path,
                   default=Path("state/sage/backtest.json"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.journal is None or not args.journal.exists():
        logger.warning(
            "no journal at %s -- sage backtester is currently a SCAFFOLD: "
            "wire bars_lookup() to your data source + pass closed-trade list "
            "via replay_trades_iter() programmatically",
            args.journal,
        )
        # Emit a placeholder so the schema is documented
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "ts": datetime.now(UTC).isoformat(),
            "status": "scaffold",
            "n_trades": 0,
            "summary": {},
        }, indent=2), encoding="utf-8")
        return 0

    # Real journal-based replay would go here -- requires journal schema
    # decoding which differs by source (sqlite vs jsonl). Left as wiring
    # for the operator who knows their journal layout.
    logger.info("sage backtester: real journal replay is wiring-pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
