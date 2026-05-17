"""Daily fleet-wide audit for the fractional-qty winner-loser asymmetry bug.

Discovered wave-25l/m on mes_sweep_reclaim_v2: cum_R is positive but
cum_USD is negative because winners cluster at qty<1 (wider stops,
higher hit rate) while losers cluster at qty=1.0 (tighter stops,
lower hit rate). Same pattern found on mnq_futures_sage (which was
about to be promoted to EVAL_LIVE) and mnq_anchor_sweep.

This audit runs daily, scans every bot's trade-close stream, and
flags bots whose:

  * win_rate_at_qty_less_than_1 exceeds win_rate_at_qty_equals_1 by
    >= 10 percentage points, AND
  * sample size is >= 50 trades at each qty band

Output goes to ``var/eta_engine/state/diamond_qty_asymmetry_latest.json``
so the prop_launch_check / wave-25 status surfaces can read it. The
operator should NOT promote a flagged bot to EVAL_LIVE without first
investigating the signal-conditions difference between qty bands.

Designed for: ``-m eta_engine.scripts.diamond_qty_asymmetry_audit``
"""
# ruff: noqa: T201
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import workspace_roots

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

OUT_LATEST = workspace_roots.ETA_DIAMOND_QTY_ASYMMETRY_PATH

MIN_SAMPLE_PER_BAND = 50
ASYMMETRY_THRESHOLD_PP = 10.0
QTY_FRACTIONAL_UPPER = 1.0  # qty < 1.0 = fractional
EXCLUDED_TEST_BOTS = frozenset({"t1", "propagate_bot", "t2", "t3", "test_bot", "fake_bot"})


def _load_records() -> dict[str, list[dict]]:
    """Read every record from canonical + legacy; group by bot_id, drop test fixtures."""
    canonical = workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH
    legacy = workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH
    by_bot: dict[str, list[dict]] = defaultdict(list)
    for p in (canonical, legacy):
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bid = str(rec.get("bot_id") or "")
                if not bid or bid in EXCLUDED_TEST_BOTS:
                    continue
                by_bot[bid].append(rec)
    return by_bot


def _analyze_bot(rows: list[dict]) -> dict:
    wins_q1 = losses_q1 = 0
    wins_qf = losses_qf = 0
    sum_r = sum_usd = 0.0
    sum_usd_q1 = sum_usd_qf = 0.0
    for r in rows:
        extra = r.get("extra") or {}
        try:
            rv = float(r.get("realized_r") or 0)
            qty = float(extra.get("qty") or 0) if isinstance(extra, dict) else 0.0
            usd = r.get("realized_pnl") or (extra.get("realized_pnl") if isinstance(extra, dict) else None)
            usd_f = float(usd) if usd is not None else 0.0
        except (TypeError, ValueError):
            continue
        sum_r += rv
        sum_usd += usd_f
        is_win = rv > 0
        is_loss = rv < 0
        qty_full = qty >= QTY_FRACTIONAL_UPPER
        qty_frac = 0 < qty < QTY_FRACTIONAL_UPPER
        if qty_full:
            sum_usd_q1 += usd_f
        if qty_frac:
            sum_usd_qf += usd_f
        if is_win and qty_full:
            wins_q1 += 1
        elif is_win and qty_frac:
            wins_qf += 1
        elif is_loss and qty_full:
            losses_q1 += 1
        elif is_loss and qty_frac:
            losses_qf += 1
    total_q1 = wins_q1 + losses_q1
    total_qf = wins_qf + losses_qf
    wr_q1 = (wins_q1 / total_q1 * 100) if total_q1 else None
    wr_qf = (wins_qf / total_qf * 100) if total_qf else None
    asymmetry_pp = None
    flag = "OK"
    if wr_q1 is not None and wr_qf is not None and total_q1 >= MIN_SAMPLE_PER_BAND and total_qf >= MIN_SAMPLE_PER_BAND:
        asymmetry_pp = round(wr_qf - wr_q1, 1)
        if asymmetry_pp >= ASYMMETRY_THRESHOLD_PP:
            flag = "ASYMMETRY_BUG"
    return {
        "n_total": len(rows),
        "n_qty_full": total_q1,
        "n_qty_frac": total_qf,
        "wr_qty_full_pct": round(wr_q1, 2) if wr_q1 is not None else None,
        "wr_qty_frac_pct": round(wr_qf, 2) if wr_qf is not None else None,
        "asymmetry_pp": asymmetry_pp,
        "cum_r": round(sum_r, 4),
        "cum_usd": round(sum_usd, 2),
        "cum_usd_qty_full": round(sum_usd_q1, 2),
        "cum_usd_qty_frac": round(sum_usd_qf, 2),
        "flag": flag,
    }


def run() -> dict:
    by_bot = _load_records()
    statuses: dict[str, dict] = {}
    flagged: list[str] = []
    for bid in sorted(by_bot):
        s = _analyze_bot(by_bot[bid])
        statuses[bid] = s
        if s["flag"] == "ASYMMETRY_BUG":
            flagged.append(bid)
    return {
        "ts": datetime.now(UTC).isoformat(),
        "min_sample_per_band": MIN_SAMPLE_PER_BAND,
        "asymmetry_threshold_pp": ASYMMETRY_THRESHOLD_PP,
        "n_bots_analyzed": len(statuses),
        "n_flagged": len(flagged),
        "flagged_bots": flagged,
        "per_bot": statuses,
    }


def _write(report: dict) -> Path:
    OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_LATEST.with_suffix(OUT_LATEST.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    tmp.replace(OUT_LATEST)
    return OUT_LATEST


def _print(report: dict) -> None:
    print()
    print("=" * 90)
    print(f"  QTY ASYMMETRY AUDIT  ({report['ts']})")
    print("=" * 90)
    print(f"  bots analyzed: {report['n_bots_analyzed']}")
    print(f"  flagged: {report['n_flagged']}")
    print(f"  flagged_bots: {report['flagged_bots']}")
    print()
    print(f"  {'bot_id':<30} {'flag':<14} {'wr@q=1':>8} {'wr@q<1':>8} {'dpp':>6} {'cum_R':>8} {'cum_USD':>10}")
    print("  " + "-" * 85)
    for bid, s in sorted(report["per_bot"].items()):
        if s["flag"] == "ASYMMETRY_BUG" or s["n_total"] >= 100:
            wr1 = f"{s['wr_qty_full_pct']:.1f}%" if s["wr_qty_full_pct"] is not None else "n/a"
            wrf = f"{s['wr_qty_frac_pct']:.1f}%" if s["wr_qty_frac_pct"] is not None else "n/a"
            asym = f"{s['asymmetry_pp']:+.1f}" if s["asymmetry_pp"] is not None else "n/a"
            print(f"  {bid:<30} {s['flag']:<14} {wr1:>8} {wrf:>8} {asym:>6} {s['cum_r']:>+7.1f}R {s['cum_usd']:>+9.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    report = run()
    if not args.no_write:
        _write(report)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
