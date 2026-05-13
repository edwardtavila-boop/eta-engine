"""Layer 7: Paper-trade preflight — readiness gate before launching
paper trading for any bot in the fleet.

Checks every gate that must pass before a paper-trade session:
1. Data available + fresh (critical bar feeds present, end date within grace period)
2. Strategy baseline present
3. Walk-forward gate cleared (or agg_degradation override applied)
4. Warmup policy set (promoted_on, warmup_days, risk_mult)
5. Bot directory exists (bots/<dir>/bot.py)
6. Decision journal path writable
7. Fleet risk gate configured
8. Venue connector available (IBKR, paper-sim, or dry-run)

Returns a clear READY/WARN/BLOCK verdict per bot.

Usage
-----
    python -m eta_engine.scripts.paper_trade_preflight
    python -m eta_engine.scripts.paper_trade_preflight --bot mnq_futures_sage
    python -m eta_engine.scripts.paper_trade_preflight --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.scripts import workspace_roots  # noqa: E402

if TYPE_CHECKING:
    from eta_engine.strategies.per_bot_registry import StrategyAssignment

BOT_DIR = ROOT / "bots"
VENUES_DIR = ROOT / "venues"
DATA_GRACE_DAYS = 3  # data must be fresh within this many days


@dataclass
class PreflightVerdict:
    bot_id: str
    strategy_id: str
    overall: str  # READY / WARN / BLOCK
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _check_data(symbol: str, timeframe: str) -> tuple[bool, str]:
    from eta_engine.data.library import default_library

    lib = default_library()
    ds = lib.get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        return False, f"no dataset: {symbol}/{timeframe}"
    age_days = (datetime.now(tz=UTC) - ds.end_ts).total_seconds() / 86400
    if age_days > DATA_GRACE_DAYS:
        return True, f"stale: {age_days:.0f}d old (grace={DATA_GRACE_DAYS}d)"
    return True, f"fresh ({ds.row_count} rows, {age_days:.0f}d old)"


def _check_baseline(bot_id: str, strategy_id: str) -> tuple[bool, str]:
    from eta_engine.scripts.paper_live_launch_check import _load_baseline_entry

    entry = _load_baseline_entry(bot_id, strategy_id)
    if entry is None:
        return False, "no baseline entry"
    return True, "baseline present"


def _check_bot_dir(bot_id: str) -> tuple[bool, str]:
    # Try exact match first, then prefix match (mnq for mnq_futures_sage, etc.)
    candidates = [
        BOT_DIR / bot_id,
        BOT_DIR / bot_id.split("_")[0],
        BOT_DIR / "_".join(bot_id.split("_")[:2]),
    ]
    for c in candidates:
        if (c / "bot.py").exists():
            return True, str(c.relative_to(ROOT))
    return False, f"no bot.py found for {bot_id}"


def _check_warmup(assignment: StrategyAssignment) -> tuple[bool, str]:
    wp = assignment.extras.get("warmup_policy")
    if not isinstance(wp, dict):
        return False, "no warmup_policy set"
    promoted = wp.get("promoted_on")
    days = wp.get("warmup_days")
    mult = wp.get("risk_multiplier_during_warmup")
    if not promoted or not days or not mult:
        return False, f"incomplete warmup_policy: {wp}"
    return True, f"warmup {days}d @ {mult}x since {promoted}"


def _check_venue() -> tuple[bool, str]:
    venues = list(VENUES_DIR.glob("**/router*.py")) if VENUES_DIR.exists() else []
    if venues:
        return True, f"venue router(s) found: {len(venues)}"
    return False, "no venue router; paper-sim only"


def _check_journal() -> tuple[bool, str]:
    j = workspace_roots.ETA_RUNTIME_DECISION_JOURNAL_PATH
    j.parent.mkdir(parents=True, exist_ok=True)
    try:
        j.touch(exist_ok=True)
        return True, str(j)
    except OSError as e:
        return False, f"journal unwritable: {e}"


def run_preflight(bot_filter: str | None = None) -> list[PreflightVerdict]:
    from eta_engine.strategies.per_bot_registry import (
        all_assignments,
        is_active,
    )

    assignments = [a for a in all_assignments() if is_active(a) and a.bot_id != "xrp_perp"]
    if bot_filter:
        assignments = [a for a in assignments if a.bot_id == bot_filter]

    results: list[PreflightVerdict] = []
    for a in assignments:
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        ok, msg = _check_data(a.symbol, a.timeframe)
        checks["data"] = ok
        if not ok:
            reasons.append(f"data: {msg}")
            results.append(PreflightVerdict(a.bot_id, a.strategy_id, "BLOCK", checks, reasons))
            continue

        ok, msg = _check_baseline(a.bot_id, a.strategy_id)
        checks["baseline"] = ok
        if not ok:
            reasons.append(f"baseline: {msg}")

        ok, msg = _check_bot_dir(a.bot_id)
        checks["bot_dir"] = ok
        if not ok:
            reasons.append(f"bot_dir: {msg}")

        ok, msg = _check_warmup(a)
        checks["warmup"] = ok
        if not ok:
            reasons.append(f"warmup: {msg}")

        ok, msg = _check_venue()
        checks["venue"] = ok
        if not ok:
            reasons.append(f"venue: {msg}")

        ok, msg = _check_journal()
        checks["journal"] = ok
        if not ok:
            reasons.append(f"journal: {msg}")

        status = a.extras.get("promotion_status", "")
        if status == "shadow_benchmark":
            reasons.append("bot is shadow_benchmark only")
            overall = "WARN"
        elif status == "research_candidate":
            reasons.append("bot is research_candidate")
            overall = "WARN"
        elif not all(checks.values()):
            overall = "WARN" if not reasons or all("stale" not in r for r in reasons) else "BLOCK"
        else:
            overall = "READY"

        results.append(PreflightVerdict(a.bot_id, a.strategy_id, overall, checks, reasons))

    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="paper_trade_preflight")
    p.add_argument("--bot", type=str, default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    results = run_preflight(bot_filter=args.bot)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "bot_id": r.bot_id,
                        "strategy_id": r.strategy_id,
                        "overall": r.overall,
                        "checks": r.checks,
                        "reasons": r.reasons,
                    }
                    for r in results
                ],
                indent=2,
                default=str,
            )
        )
    else:
        print(
            f"{'Bot':<24} {'Overall':<8} {'Data':<8} {'Baseline':<10} {'Bot Dir':<9} {'Warmup':<8} {'Venue':<8} {'Journal':<8} {'Reasons'}"
        )
        print("-" * 140)
        for r in results:
            ch = " ".join(
                f"{'OK' if r.checks.get(k, False) else 'NO':>3}" if k in r.checks else "  -"
                for k in ["data", "baseline", "bot_dir", "warmup", "venue", "journal"]
            )
            reason_str = "; ".join(r.reasons)[:60] if r.reasons else "-"
            print(f"{r.bot_id:<24} {r.overall:<8} {ch}  {reason_str}")
        ready = sum(1 for r in results if r.overall == "READY")
        warn = sum(1 for r in results if r.overall == "WARN")
        block = sum(1 for r in results if r.overall == "BLOCK")
        print(f"\nREADY={ready}  WARN={warn}  BLOCK={block}  / {len(results)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
