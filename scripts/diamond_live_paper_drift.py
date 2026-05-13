"""Live-vs-paper drift detector — original wave-21 deferred item.

When the operator runs live + paper in parallel (wave-25 lifecycle),
we should be ABLE to compare a live fill to its paper-projected fill
and quantify the drift. Significant drift catches:

  * Slippage anomalies (live worse than paper-sim expected)
  * Sizing-translation bugs (live qty != paper qty for same signal)
  * Fill-model errors (paper fills at touch, live fills at next bar)
  * Symbol-mapping bugs (live routes MNQ1, paper routes MNQ — different
    months can have meaningfully different prices)

Detection method
----------------

For every live trade-close record, look for a paper trade-close from
the SAME ``signal_id`` (the bot-emitted signal identifier is the join
key). Compare:

  * fill_price
  * realized_pnl
  * realized_r
  * qty

Per-pair drift is summarized as a fraction of the paper value, with
absolute deltas in dollars/R for forensics.

Aggregate drift is bucketed by severity:

  * OK    — < 5% drift on all 4 dimensions
  * WARN  — 5-15% drift on any one dimension
  * BAD   — >15% drift on any dimension OR sign disagreement

Output
------

* ``var/eta_engine/state/diamond_live_paper_drift_latest.json``
* stdout summary

Note
----

The current operator-mode default sources are ``live + paper +
live_unverified``. The drift detector compares ``live`` vs ``paper``
EXPLICITLY — ``live_unverified`` records (canonical-path, no tag)
are excluded because we don't know which side they're on.

This is a SKELETON: until the operator promotes at least one bot to
EVAL_LIVE and live fills land, this script reports n_pairs=0 and
exits cleanly. Wired into the daily cron so the moment live data
appears, drift starts being tracked automatically.
"""
# ruff: noqa: T201
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_live_paper_drift_latest.json"

# Drift thresholds (fraction of paper value)
DRIFT_WARN = 0.05
DRIFT_BAD = 0.15


@dataclass
class PairDrift:
    signal_id: str
    bot_id: str
    fill_price_drift_pct: float | None
    realized_pnl_drift_pct: float | None
    realized_r_drift_pct: float | None
    qty_drift_pct: float | None
    severity: str  # OK / WARN / BAD
    notes: list[str] = field(default_factory=list)


def _safe_float(v: Any) -> float | None:  # noqa: ANN401
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_fields(rec: dict[str, Any]) -> dict[str, float | None]:
    extra = rec.get("extra") or {}
    return {
        "fill_price": _safe_float(extra.get("fill_price")) if isinstance(extra, dict) else None,
        "realized_pnl": _safe_float(rec.get("realized_pnl"))
        or _safe_float(extra.get("realized_pnl") if isinstance(extra, dict) else None),
        "realized_r": _safe_float(rec.get("realized_r")),
        "qty": _safe_float(extra.get("qty")) if isinstance(extra, dict) else None,
    }


def _pct_drift(live: float | None, paper: float | None) -> float | None:
    """Return (live - paper) / abs(paper) as a fraction. None if either is None or paper is 0."""
    if live is None or paper is None or paper == 0:
        return None
    return (live - paper) / abs(paper)


def _classify(d: PairDrift) -> str:
    drifts = [
        d.fill_price_drift_pct,
        d.realized_pnl_drift_pct,
        d.realized_r_drift_pct,
        d.qty_drift_pct,
    ]
    drifts = [abs(x) for x in drifts if x is not None]
    if not drifts:
        return "OK"
    max_drift = max(drifts)
    if max_drift > DRIFT_BAD:
        return "BAD"
    if max_drift > DRIFT_WARN:
        return "WARN"
    return "OK"


def compute_drift() -> dict[str, Any]:
    """Read live + paper trade closes; join by signal_id; report per-pair + aggregate drift."""
    from eta_engine.scripts.closed_trade_ledger import (  # noqa: PLC0415
        DATA_SOURCE_LIVE,
        DATA_SOURCE_PAPER,
        load_close_records,
    )

    live_rows = load_close_records(data_sources=frozenset({DATA_SOURCE_LIVE}))
    paper_rows = load_close_records(data_sources=frozenset({DATA_SOURCE_PAPER}))

    paper_by_signal: dict[str, dict[str, Any]] = {}
    for r in paper_rows:
        sid = str(r.get("signal_id") or "")
        if sid and sid not in paper_by_signal:
            paper_by_signal[sid] = r

    pairs: list[PairDrift] = []
    unmatched_live: list[str] = []
    for r in live_rows:
        sid = str(r.get("signal_id") or "")
        if not sid:
            continue
        paper = paper_by_signal.get(sid)
        if paper is None:
            unmatched_live.append(sid)
            continue
        lf = _extract_fields(r)
        pf = _extract_fields(paper)
        d = PairDrift(
            signal_id=sid,
            bot_id=str(r.get("bot_id") or ""),
            fill_price_drift_pct=_pct_drift(lf["fill_price"], pf["fill_price"]),
            realized_pnl_drift_pct=_pct_drift(lf["realized_pnl"], pf["realized_pnl"]),
            realized_r_drift_pct=_pct_drift(lf["realized_r"], pf["realized_r"]),
            qty_drift_pct=_pct_drift(lf["qty"], pf["qty"]),
            severity="?",
        )
        d.severity = _classify(d)
        pairs.append(d)

    # Aggregate
    severity_counts = defaultdict(int)
    for p in pairs:
        severity_counts[p.severity] += 1

    # Per-bot drift summary
    per_bot: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "n_pairs": 0,
            "n_ok": 0,
            "n_warn": 0,
            "n_bad": 0,
            "max_drift_seen": 0.0,
        },
    )
    for p in pairs:
        b = per_bot[p.bot_id]
        b["n_pairs"] += 1
        if p.severity == "OK":
            b["n_ok"] += 1
        elif p.severity == "WARN":
            b["n_warn"] += 1
        elif p.severity == "BAD":
            b["n_bad"] += 1
        for v in (p.fill_price_drift_pct, p.realized_pnl_drift_pct, p.realized_r_drift_pct, p.qty_drift_pct):
            if v is not None and abs(v) > b["max_drift_seen"]:
                b["max_drift_seen"] = abs(v)

    return {
        "ts": datetime.now(UTC).isoformat(),
        "n_live_records": len(live_rows),
        "n_paper_records": len(paper_rows),
        "n_pairs_matched": len(pairs),
        "n_unmatched_live": len(unmatched_live),
        "severity_counts": dict(severity_counts),
        "per_bot": {k: v for k, v in sorted(per_bot.items())},
        "pairs": [
            {
                "signal_id": p.signal_id,
                "bot_id": p.bot_id,
                "fill_price_drift_pct": p.fill_price_drift_pct,
                "realized_pnl_drift_pct": p.realized_pnl_drift_pct,
                "realized_r_drift_pct": p.realized_r_drift_pct,
                "qty_drift_pct": p.qty_drift_pct,
                "severity": p.severity,
            }
            for p in pairs[:100]  # cap raw pair list at 100 for receipt size
        ],
    }


def _write_receipt(report: dict[str, Any], path: Path = OUT_LATEST) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def _print_table(report: dict[str, Any]) -> None:
    print()
    print("=" * 80)
    print(f"  LIVE-vs-PAPER DRIFT  ({report['ts']})")
    print("=" * 80)
    print(f"  live records: {report['n_live_records']}")
    print(f"  paper records: {report['n_paper_records']}")
    print(f"  pairs matched: {report['n_pairs_matched']}")
    print(f"  unmatched live (paper not found): {report['n_unmatched_live']}")
    if not report["n_pairs_matched"]:
        print()
        print("  No matched pairs yet — skeleton is wired but waiting for live fills.")
        print("  Once a bot is in EVAL_LIVE and live trades close, drift starts surfacing here.")
        return
    print()
    print(f"  severity: {report['severity_counts']}")
    print()
    if report["per_bot"]:
        print("  per-bot summary:")
        print(f"    {'bot_id':<32} {'pairs':>5} {'OK':>4} {'WARN':>5} {'BAD':>4} {'max_drift':>11}")
        for bot_id, stats in report["per_bot"].items():
            print(
                f"    {bot_id:<32} {stats['n_pairs']:>5} {stats['n_ok']:>4} "
                f"{stats['n_warn']:>5} {stats['n_bad']:>4} {stats['max_drift_seen']:>10.1%}",
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    report = compute_drift()
    if not args.no_write:
        _write_receipt(report)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
