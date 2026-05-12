"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_sizing_audit
=============================================================
Per-diamond USD-per-R sizing audit — the formal version of the
wave-8 sizing-kaizen forensic.

Why this exists
---------------
The wave-7 dual-basis watchdog surfaces bots whose USD verdict and R
verdict disagree.  Disagreement means SIZING failure (the strategy is
working in R-multiples but per-trade dollar amounts exceed the
operator's USD retirement floor).  Wave-8 fixed cl_momentum and
gc_momentum manually after a one-shot Python forensic.

This script codifies that forensic so:
  - Future operators can re-run it any time without ad-hoc SQL
  - The same logic applies to all current + future diamonds
  - The verdict feeds an alert pipeline (sizing-breach as a leading
    indicator of USD-CRITICAL classification)

What it computes
----------------
For each diamond with at least MIN_TRADES_FOR_VERDICT real-PnL trades:

  - n_trades_with_pnl: rows with both ``realized_r`` and ``realized_pnl``
                       (or extra.realized_pnl) populated
  - cum_r:             sum of realized_r
  - cum_usd:           sum of realized_pnl
  - usd_per_r_avg:     mean of (pnl / r) per trade (ignoring r near zero)
  - usd_per_r_std:     stddev of usd_per_r samples
  - usd_per_r_max:     worst single-trade dollar-per-R (loss magnitude)
  - n_stopouts_to_breach: |USD_floor| / usd_per_r_max — how many full-R
                          stopouts in a row would hit the watchdog floor
  - verdict:
      SIZING_OK         — n_stopouts_to_breach >= 4 (plenty of room)
      SIZING_TIGHT      — 2 <= n_stopouts_to_breach < 4 (operator-aware)
      SIZING_FRAGILE    — 1 <= n_stopouts_to_breach < 2 (single-trade-fragile)
      SIZING_BREACHED   — n_stopouts_to_breach < 1 (one stopout breaches)

Output
------
- stdout report (human-readable per-bot scorecard)
- ``var/eta_engine/state/diamond_sizing_audit_latest.json`` receipt
- exit 0 = no SIZING_BREACHED diamonds; exit 2 = at least one breached

Usage
-----

::

    python -m eta_engine.scripts.diamond_sizing_audit
    python -m eta_engine.scripts.diamond_sizing_audit --json
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
TRADE_CLOSES_CANONICAL = (
    WORKSPACE_ROOT / "var" / "eta_engine" / "state"
    / "jarvis_intel" / "trade_closes.jsonl"
)
TRADE_CLOSES_LEGACY = (
    WORKSPACE_ROOT / "eta_engine" / "state"  # HISTORICAL-PATH-OK
    / "jarvis_intel" / "trade_closes.jsonl"
)
OUT_LATEST = (
    WORKSPACE_ROOT / "var" / "eta_engine" / "state"
    / "diamond_sizing_audit_latest.json"
)

#: Below this trade count we can't trust the $/R statistics yet.
MIN_TRADES_FOR_VERDICT = 5

#: Trades with |realized_r| smaller than this would make pnl/r explode.
#: We exclude these from the per-trade ratio calculation but keep them
#: in the cumulative totals.
MIN_ABS_R_FOR_RATIO = 0.01


@dataclass
class SizingScorecard:
    bot_id: str
    n_trades_total: int = 0
    n_trades_with_pnl: int = 0
    cum_r: float = 0.0
    cum_usd: float = 0.0
    usd_per_r_avg: float | None = None
    usd_per_r_std: float | None = None
    usd_per_r_max_abs: float | None = None
    avg_qty: float | None = None
    threshold_usd: float | None = None
    n_stopouts_to_breach: float | None = None
    verdict: str = "INSUFFICIENT_DATA"
    rationale: str = ""
    notes: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# IO
# ────────────────────────────────────────────────────────────────────


def _read_trades_dual_source() -> list[dict[str, Any]]:
    """Dual-source dedup'd read — same pattern as kelly_optimizer +
    diamond_promotion_gate. Dedup key: (signal_id, bot_id, ts, realized_r)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path in (TRADE_CLOSES_CANONICAL, TRADE_CLOSES_LEGACY):
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = "|".join([
                    str(rec.get("signal_id") or ""),
                    str(rec.get("bot_id") or ""),
                    str(rec.get("ts") or ""),
                    str(rec.get("realized_r") or ""),
                ])
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
    return out


def _extract_pnl_qty(rec: dict[str, Any]) -> tuple[float | None, float | None]:
    """Pull (realized_pnl, qty) honouring both top-level and `extra` nests.
    Diamond_data_sanitizer-quarantined rows return None for pnl so they're
    excluded from sizing stats (the original USD was scale-bug poisoned)."""
    extra = rec.get("extra") or {}
    pnl = None
    qty = None
    if isinstance(extra, dict):
        pnl = extra.get("realized_pnl")
        qty = extra.get("qty")
    if pnl is None:
        pnl = rec.get("realized_pnl")
    # Quarantined sanitizer rows store the original poisoned PnL but set
    # the live realized_pnl to 0.0 — exclude these (their dollar magnitude
    # is meaningless; the original poison wasn't real).
    if rec.get("_sanitizer_quarantined"):
        pnl = None
    try:
        pnl = float(pnl) if pnl is not None else None
    except (TypeError, ValueError):
        pnl = None
    try:
        qty = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty = None
    if pnl == 0.0:
        # Zero PnL adds no information to $/R statistics.
        pnl = None
    return pnl, qty


# ────────────────────────────────────────────────────────────────────
# Sizing math
# ────────────────────────────────────────────────────────────────────


def _classify_sizing(
    usd_per_r_max_abs: float | None,
    threshold_usd: float | None,
) -> tuple[str, float | None]:
    """Return (verdict, n_stopouts_to_breach)."""
    if usd_per_r_max_abs is None or usd_per_r_max_abs <= 0:
        return "INSUFFICIENT_DATA", None
    if threshold_usd is None:
        return "INSUFFICIENT_DATA", None
    floor_mag = abs(threshold_usd)
    n_breach = floor_mag / usd_per_r_max_abs
    if n_breach < 1.0:
        return "SIZING_BREACHED", round(n_breach, 2)
    if n_breach < 2.0:
        return "SIZING_FRAGILE", round(n_breach, 2)
    if n_breach < 4.0:
        return "SIZING_TIGHT", round(n_breach, 2)
    return "SIZING_OK", round(n_breach, 2)


def _score_bot(bot_id: str, trades: list[dict[str, Any]],
               threshold_usd: float | None) -> SizingScorecard:
    sc = SizingScorecard(
        bot_id=bot_id,
        n_trades_total=len(trades),
        threshold_usd=threshold_usd,
    )

    per_trade: list[tuple[float, float, float | None]] = []  # (r, pnl, qty)
    for rec in trades:
        r = rec.get("realized_r")
        try:
            r_val = float(r) if r is not None else None
        except (TypeError, ValueError):
            r_val = None
        if r_val is None or abs(r_val) < MIN_ABS_R_FOR_RATIO:
            continue
        pnl, qty = _extract_pnl_qty(rec)
        if pnl is None:
            continue
        per_trade.append((r_val, pnl, qty))

    sc.n_trades_with_pnl = len(per_trade)
    if sc.n_trades_with_pnl < MIN_TRADES_FOR_VERDICT:
        sc.verdict = "INSUFFICIENT_DATA"
        sc.rationale = (
            f"{sc.n_trades_with_pnl} trades with usable R+PnL "
            f"(need >= {MIN_TRADES_FOR_VERDICT})"
        )
        if sc.n_trades_with_pnl > 0:
            sc.cum_r = round(sum(r for r, _, _ in per_trade), 4)
            sc.cum_usd = round(sum(p for _, p, _ in per_trade), 2)
        return sc

    sc.cum_r = round(sum(r for r, _, _ in per_trade), 4)
    sc.cum_usd = round(sum(p for _, p, _ in per_trade), 2)

    usd_per_r_samples = [p / r for r, p, _ in per_trade]
    avg = sum(usd_per_r_samples) / len(usd_per_r_samples)
    sc.usd_per_r_avg = round(avg, 2)
    if len(usd_per_r_samples) > 1:
        var = sum((x - avg) ** 2 for x in usd_per_r_samples) / (
            len(usd_per_r_samples) - 1
        )
        sc.usd_per_r_std = round(var ** 0.5, 2)
    else:
        sc.usd_per_r_std = 0.0

    # For verdict, use the WORST single-trade USD-per-R magnitude.
    # That's the "if one trade stops out, how much does it cost?" answer
    # — which is what the watchdog floor cares about.
    sc.usd_per_r_max_abs = round(max(abs(x) for x in usd_per_r_samples), 2)

    qtys = [q for _, _, q in per_trade if q is not None]
    if qtys:
        sc.avg_qty = round(sum(qtys) / len(qtys), 3)

    sc.verdict, sc.n_stopouts_to_breach = _classify_sizing(
        sc.usd_per_r_max_abs, threshold_usd,
    )

    floor_str = f"${threshold_usd:.0f}" if threshold_usd is not None else "n/a"
    sc.rationale = (
        f"worst-trade $/R={sc.usd_per_r_max_abs}, floor={floor_str}, "
        f"stopouts_to_breach={sc.n_stopouts_to_breach}"
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
    from eta_engine.scripts.diamond_falsification_watchdog import (  # noqa: PLC0415
        RETIREMENT_THRESHOLDS_USD,
    )

    trades = _read_trades_dual_source()
    by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        bot_id = t.get("bot_id") or ""
        if bot_id in DIAMOND_BOTS:
            by_bot[bot_id].append(t)

    scorecards: list[SizingScorecard] = []
    for bot_id in sorted(DIAMOND_BOTS):
        sc = _score_bot(
            bot_id,
            by_bot.get(bot_id, []),
            RETIREMENT_THRESHOLDS_USD.get(bot_id),
        )
        scorecards.append(sc)

    counts: dict[str, int] = defaultdict(int)
    for sc in scorecards:
        counts[sc.verdict] += 1

    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_diamonds": len(scorecards),
        "verdict_counts": dict(counts),
        "min_trades_for_verdict": MIN_TRADES_FOR_VERDICT,
        "min_abs_r_for_ratio": MIN_ABS_R_FOR_RATIO,
        "thresholds_usd": dict(RETIREMENT_THRESHOLDS_USD),
        "statuses": [asdict(sc) for sc in scorecards],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict[str, Any]) -> None:
    print("=" * 120)
    print(
        f" DIAMOND SIZING AUDIT  ({summary['ts']})  "
        + ", ".join(f"{k}={v}" for k, v in summary["verdict_counts"].items()),
    )
    print("=" * 120)
    print(
        f" {'bot':25s} {'verdict':18s} {'n':>5s} "
        f"{'cum_R':>8s} {'cum_USD':>10s} "
        f"{'$/R_avg':>9s} {'$/R_worst':>10s} {'floor':>8s} "
        f"{'stopouts':>9s}",
    )
    print("-" * 120)
    for sc in summary["statuses"]:
        avg = sc.get("usd_per_r_avg")
        worst = sc.get("usd_per_r_max_abs")
        thr = sc.get("threshold_usd")
        stopouts = sc.get("n_stopouts_to_breach")
        avg_s = f"{avg:>9.1f}" if avg is not None else f"{'—':>9s}"
        worst_s = f"{worst:>10.1f}" if worst is not None else f"{'—':>10s}"
        thr_s = f"{thr:>8.0f}" if thr is not None else f"{'—':>8s}"
        stopouts_s = (
            f"{stopouts:>9.2f}" if stopouts is not None else f"{'—':>9s}"
        )
        print(
            f" {sc['bot_id']:25s} {sc['verdict']:18s} "
            f"{sc['n_trades_with_pnl']:>5d} "
            f"{sc['cum_r']:>+8.2f} {sc['cum_usd']:>+10.0f} "
            f"{avg_s} {worst_s} {thr_s} {stopouts_s}",
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
    # Exit 2 if any diamond is SIZING_BREACHED (alert-able signal).
    if summary["verdict_counts"].get("SIZING_BREACHED", 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
