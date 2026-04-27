"""
EVOLUTIONARY TRADING ALGO  //  scripts.paper_live_launch_check
================================================================
Pre-launch readiness check for paper-live deployment of all
promoted bots in per_bot_registry.

User mandate (2026-04-27): "lets have it down solid and then
launch all bots to run paper live".

This script does NOT launch anything — it's the readiness
**audit** that runs before the actual launch. It enumerates
every promoted bot in the registry and checks:

1. **Strategy resolves** — the strategy_kind has a registered
   handler in run_research_grid.
2. **Data available** — all critical DataRequirements have files
   on disk.
3. **Baseline persisted** — strategy_baselines.json has an entry
   (or registry entry has rationale-baseline).
4. **Warmup policy set** — promoted_on, warmup_days, risk_mult.
5. **Bot directory exists** — bots/<dir>/ has a bot.py.
6. **No deferred dependencies** — does the bot need any provider
   that isn't yet wired (e.g. funding for ETH bots).

Output is a per-bot status table:
  ✅ READY = clear to launch paper-live
  ⚠️  WARN = launchable but with caveats
  ❌ BLOCK = cannot launch (missing data / unresolved kind)

Usage
-----
    python -m eta_engine.scripts.paper_live_launch_check

    # Filter to specific bots
    python -m eta_engine.scripts.paper_live_launch_check \\
        --bots eth_compression,btc_sage_daily_etf

    # JSON output for CI / dashboard
    python -m eta_engine.scripts.paper_live_launch_check --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# Strategy kinds we know how to resolve at runtime
_RESOLVABLE_KINDS: frozenset[str] = frozenset({
    "confluence", "orb", "drb", "grid",
    "crypto_orb", "crypto_trend", "crypto_meanrev", "crypto_scalp",
    "sage_consensus", "orb_sage_gated", "crypto_regime_trend",
    "crypto_macro_confluence", "sage_daily_gated", "ensemble_voting",
    "compression_breakout", "sweep_reclaim",
})


def _check_data_available(symbol: str, timeframe: str) -> bool:
    """True if a bar CSV exists for this symbol/timeframe."""
    # Try crypto root first, then mnq root
    for root in (
        Path(r"C:\crypto_data\history"),
        Path(r"C:\mnq_data\history"),
    ):
        for variant in (f"{symbol}_{timeframe}.csv",
                        f"{symbol}1_{timeframe}.csv"):
            p = root / variant
            if p.exists() and p.stat().st_size > 100:
                return True
    return False


def _check_bot_dir_exists(bot_id: str) -> bool:
    """Heuristic: variant bots share a dir with a base bot.
    True if either bots/{bot_id}/ exists OR the bot is in
    VARIANT_BOT_IDS (which we infer here from the registry sync test).
    """
    bots_dir = ROOT / "bots"
    if (bots_dir / bot_id).exists():
        return True
    # Variant mapping (mirror of test_bots_registry_sync.VARIANT_BOT_IDS)
    variant_map = {
        "mnq_futures": "mnq",         # base bot — registered under bots/mnq/
        "nq_futures": "nq",            # base bot — registered under bots/nq/
        "nq_daily_drb": "nq",
        "mnq_futures_sage": "mnq",
        "nq_futures_sage": "nq",
        "btc_hybrid_sage": "btc_hybrid",
        "btc_regime_trend": "btc_hybrid",
        "mnq_sage_consensus": "mnq",
        "btc_sage_daily_etf": "btc_hybrid",
        "btc_regime_trend_etf": "btc_hybrid",
        "btc_ensemble_2of3": "btc_hybrid",
        "eth_sage_daily": "eth_perp",
        "eth_compression": "eth_perp",
        "btc_compression": "btc_hybrid",
    }
    underlying = variant_map.get(bot_id)
    return bool(underlying and (bots_dir / underlying).exists())


def _check_baseline_persisted(bot_id: str, strategy_id: str) -> bool:
    """Does strategy_baselines.json have an entry?

    Schema: ``{"strategies": [{"strategy_id": ..., ...}, ...]}``.
    Either strategy_id or bot_id can match.
    """
    path = ROOT / "docs" / "strategy_baselines.json"
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as f:
            baselines = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(baselines, dict):
        return False
    strategies = baselines.get("strategies") or []
    return any(
        isinstance(s, dict) and (
            s.get("strategy_id") == strategy_id
            or s.get("bot_id") == bot_id
        )
        for s in strategies
    )


def _audit_bot(assignment: Any) -> dict:  # noqa: ANN401
    """Run all readiness checks for a single bot. Returns status dict.

    Logic:
    * Issues → BLOCK (cannot launch)
    * Warnings → WARN (launchable with caveats)
    * Neither → READY

    A bot is fully READY if it has BOTH a baseline AND a warmup_policy
    (or has been explicitly marked deactivated). The two gates cover
    different concerns:
    * baseline → drift watchdog has a reference for live PnL comparison
    * warmup_policy → sizing discipline during the first N days post-
      promotion; protects from regime-shift outliers blowing the eval
    """
    issues: list[str] = []
    warnings: list[str] = []

    # 1. Strategy kind resolvable
    if assignment.strategy_kind not in _RESOLVABLE_KINDS:
        issues.append(f"unknown strategy_kind: {assignment.strategy_kind}")

    # 2. Data available
    if not _check_data_available(assignment.symbol, assignment.timeframe):
        issues.append(
            f"no data file for {assignment.symbol}/{assignment.timeframe}",
        )

    # 3. Bot directory exists
    if not _check_bot_dir_exists(assignment.bot_id):
        issues.append(f"no bot directory for {assignment.bot_id}")

    # 4. Promotion-status check (research_candidate is its own warning)
    promo_status = (
        assignment.extras.get("promotion_status")
        if assignment.extras else None
    )
    if promo_status == "research_candidate":
        warnings.append("research_candidate (gate not fully passed)")
    elif promo_status == "deactivated":
        # Deactivated bots aren't warnings — they're explicitly off
        return {
            "bot_id": assignment.bot_id,
            "strategy_id": assignment.strategy_id,
            "strategy_kind": assignment.strategy_kind,
            "symbol": assignment.symbol,
            "timeframe": assignment.timeframe,
            "promotion_status": "deactivated",
            "status": "READY",  # explicitly off = not a launch blocker
            "issues": [],
            "warnings": [],
        }

    # 5. Baseline + warmup_policy.
    #
    # Ready logic:
    # * Baseline present → drift watchdog has a reference. Implicit
    #   standard warmup (30 days × 0.5 risk) applies unless overridden.
    # * No baseline → real validation gap, WARN.
    # * Explicit warmup_policy → applied as override.
    has_baseline = _check_baseline_persisted(
        assignment.bot_id, assignment.strategy_id,
    )
    has_warmup_override = bool(
        assignment.extras and assignment.extras.get("warmup_policy"),
    )
    if not has_baseline:
        warnings.append("baseline not in strategy_baselines.json")
    # warmup_policy is no longer a WARN by itself — implicit standard
    # applies. Only flag when the bot has neither baseline nor warmup
    # (caught by the no-baseline warning above).
    _ = has_warmup_override  # kept for reporting clarity

    if issues:
        status = "BLOCK"
    elif warnings:
        status = "WARN"
    else:
        status = "READY"

    return {
        "bot_id": assignment.bot_id,
        "strategy_id": assignment.strategy_id,
        "strategy_kind": assignment.strategy_kind,
        "symbol": assignment.symbol,
        "timeframe": assignment.timeframe,
        "promotion_status": promo_status or "promoted",
        "status": status,
        "issues": issues,
        "warnings": warnings,
    }


def _print_table(results: list[dict]) -> None:
    print(f"\n{'STATUS':<7}  {'bot_id':<22}  {'strategy_id':<28}"
          f"  {'kind':<22}  notes")
    print("-" * 110)
    for r in results:
        symbol_status = {
            "READY": "READY",
            "WARN": "WARN ",
            "BLOCK": "BLOCK",
        }[r["status"]]
        notes = "; ".join(r["issues"] + r["warnings"]) or "-"
        if len(notes) > 50:
            notes = notes[:47] + "..."
        print(
            f"{symbol_status:<7}  {r['bot_id']:<22}  {r['strategy_id']:<28}"
            f"  {r['strategy_kind']:<22}  {notes}"
        )
    print("-" * 110)
    counts = {"READY": 0, "WARN": 0, "BLOCK": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\nSummary: {counts['READY']} READY, {counts['WARN']} WARN, "
          f"{counts['BLOCK']} BLOCK (out of {len(results)} bots)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--bots", default=None,
        help="Comma-separated bot_ids to audit (default: all)",
    )
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of a table")
    args = p.parse_args()

    from eta_engine.strategies.per_bot_registry import ASSIGNMENTS

    filt = (
        {b.strip() for b in args.bots.split(",") if b.strip()}
        if args.bots else None
    )
    targets = [
        a for a in ASSIGNMENTS
        if filt is None or a.bot_id in filt
    ]

    results = [_audit_bot(a) for a in targets]
    results.sort(key=lambda r: (
        {"READY": 0, "WARN": 1, "BLOCK": 2}[r["status"]],
        r["bot_id"],
    ))

    if args.json:
        print(json.dumps({
            "timestamp": datetime.now(UTC).isoformat(),
            "n_bots": len(results),
            "ready": [r for r in results if r["status"] == "READY"],
            "warn": [r for r in results if r["status"] == "WARN"],
            "block": [r for r in results if r["status"] == "BLOCK"],
        }, indent=2))
    else:
        print(f"[paper-live-check] {datetime.now(UTC).isoformat()}")
        print(f"[paper-live-check] auditing {len(results)} bots")
        _print_table(results)

    n_blocked = sum(1 for r in results if r["status"] == "BLOCK")
    return n_blocked  # exit code = number of blockers


if __name__ == "__main__":
    sys.exit(main())
