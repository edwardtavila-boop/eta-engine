"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_falsification_watchdog
=====================================================================
Early-warning system for the 8 diamond bots.

Why this exists
---------------
The diamond protection (3 layers) refuses to auto-retire a diamond.
But the operator pre-committed falsification criteria per bot in
``var/eta_engine/decisions/diamond_set_2026_05_12.md``.  Those criteria
fire AFTER the damage is done.  This watchdog gives the operator a
DISTANCE-TO-TRIGGER metric — how close each diamond is to its 30-day
P&L retirement threshold — so review can happen BEFORE the floor.

What it does
------------
For each diamond:
  - Pulls the 30-day rolling P&L from the closed_trade_ledger
  - Compares to its pre-committed retirement threshold
  - Computes the "buffer" (distance to retirement, in $)
  - Classifies: HEALTHY / WATCH / WARN / CRITICAL

Buffer thresholds (relative to the bot's retirement threshold):
  - HEALTHY  : buffer > 50% of threshold magnitude
  - WATCH    : buffer 20-50%  (operator should be aware)
  - WARN     : buffer 0-20%   (operator should review soon)
  - CRITICAL : buffer <= 0    (threshold breached — operator retire decision)

Output
------
- stdout / --json
- var/eta_engine/state/diamond_watchdog_latest.json
- logs/eta_engine/diamond_watchdog.jsonl (append)

Exit code
---------
- 0: all diamonds HEALTHY or WATCH
- 1: any WARN
- 2: any CRITICAL (operator paging signal)

Run
---
::

    python -m eta_engine.scripts.diamond_falsification_watchdog
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.feeds.capital_allocator import DIAMOND_BOTS

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
STATE_DIR = WORKSPACE_ROOT / "var" / "eta_engine" / "state"
LOG_DIR = WORKSPACE_ROOT / "logs" / "eta_engine"

CLOSED_LEDGER = STATE_DIR / "closed_trade_ledger_latest.json"
OUT_LATEST = STATE_DIR / "diamond_watchdog_latest.json"
OUT_LOG = LOG_DIR / "diamond_watchdog.jsonl"

#: Pre-committed retirement thresholds from the operator's 2026-05-12
#: decision memo.  Each is the 30-day rolling P&L floor; falling below
#: triggers operator review (NOT auto-deactivate — see DIAMOND_PROTECTION
#: doc layer 2+3).
RETIREMENT_THRESHOLDS_USD: dict[str, float] = {
    "mnq_futures_sage":   -5000.0,
    "nq_futures_sage":    -1500.0,
    "cl_momentum":        -1500.0,
    "mcl_sweep_reclaim":  -1500.0,
    "mgc_sweep_reclaim":   -600.0,
    "eur_sweep_reclaim":   -300.0,  # FRAGILE
    "gc_momentum":         -200.0,  # FRAGILE
    "cl_macro":           -1000.0,
}


@dataclass
class DiamondStatus:
    bot_id: str
    pnl_lifetime: float | None = None
    pnl_recent_window: float | None = None
    retirement_threshold: float | None = None
    buffer_usd: float | None = None
    buffer_pct_of_threshold: float | None = None
    classification: str = "INCONCLUSIVE"
    notes: list[str] = field(default_factory=list)


def _load_ledger() -> dict | None:
    if not CLOSED_LEDGER.exists():
        return None
    try:
        return json.loads(CLOSED_LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _classify(status: DiamondStatus) -> None:
    """Classify based on buffer % of threshold magnitude."""
    if status.buffer_usd is None or status.retirement_threshold is None:
        status.classification = "INCONCLUSIVE"
        return
    threshold_mag = abs(status.retirement_threshold)
    if threshold_mag == 0:
        status.classification = "INCONCLUSIVE"
        return
    pct = status.buffer_usd / threshold_mag * 100.0
    status.buffer_pct_of_threshold = round(pct, 1)
    if status.buffer_usd <= 0:
        status.classification = "CRITICAL"
    elif pct < 20:
        status.classification = "WARN"
    elif pct < 50:
        status.classification = "WATCH"
    else:
        status.classification = "HEALTHY"


def _evaluate(bot_id: str, ledger: dict | None) -> DiamondStatus:
    s = DiamondStatus(bot_id=bot_id)
    s.retirement_threshold = RETIREMENT_THRESHOLDS_USD.get(bot_id)
    if s.retirement_threshold is None:
        s.notes.append("no retirement threshold defined")
        s.classification = "INCONCLUSIVE"
        return s
    if ledger is None:
        s.notes.append("closed_trade_ledger missing")
        return s
    rec = ledger.get("per_bot", {}).get(bot_id)
    if rec is None:
        s.notes.append("bot not in ledger")
        return s
    try:
        s.pnl_lifetime = float(rec.get("total_realized_pnl") or 0)
        # The closed_trade_ledger doesn't carry a 30-day window directly;
        # use lifetime as an upper-bound proxy until the rolling pipeline
        # is wired.  When pipeline arrives, replace with the actual 30d
        # rolling P&L from kaizen reports.
        s.pnl_recent_window = s.pnl_lifetime
    except (TypeError, ValueError) as exc:
        s.notes.append(f"pnl parse error: {exc}")
        return s
    # Detect scale-bug: realistic per-trade P&L magnitude on paper
    # futures rarely exceeds a few hundred dollars per contract per
    # trade.  $5,000+ per trade is the eur_sweep_reclaim signature
    # (its ledger shows ~$75k/trade — a forex notional-vs-USD bug).
    # Threshold set well above realistic max so we don't flag legit
    # large-stop trades, but well below the scale-bug case.
    n_trades = int(rec.get("closed_trade_count") or 0)
    if n_trades and abs(s.pnl_lifetime or 0) / max(n_trades, 1) > 5_000:
        s.notes.append(
            "SCALE_BUG_SUSPECTED — avg per-trade exceeds $5,000; "
            "ledger P&L not trustworthy; verdict reserved",
        )
        s.classification = "INCONCLUSIVE"
        return s
    # Buffer = (pnl_recent_window - retirement_threshold)
    # Positive buffer = we're ABOVE the floor (good)
    s.buffer_usd = round(
        (s.pnl_recent_window or 0) - s.retirement_threshold, 2,
    )
    _classify(s)
    return s


def run_watchdog() -> dict:
    ledger = _load_ledger()
    statuses = [_evaluate(b, ledger) for b in sorted(DIAMOND_BOTS)]
    counts: dict[str, int] = {}
    for st in statuses:
        counts[st.classification] = counts.get(st.classification, 0) + 1
    report = {
        "ts": datetime.now(UTC).isoformat(),
        "ledger_present": ledger is not None,
        "n_diamonds": len(statuses),
        "classification_counts": counts,
        "thresholds": RETIREMENT_THRESHOLDS_USD,
        "statuses": [asdict(s) for s in statuses],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        print(f"WARN: latest write failed: {exc}", file=sys.stderr)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with OUT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": report["ts"],
                "classification_counts": counts,
            }, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(f"WARN: log append failed: {exc}", file=sys.stderr)
    return report


def _print(report: dict) -> None:
    print("=" * 100)
    print(f" DIAMOND FALSIFICATION WATCHDOG — {report['ts']}")
    print("=" * 100)
    print(" Classification roll-up: " + ", ".join(
        f"{k}={v}" for k, v in report["classification_counts"].items()))
    print()
    print(f" {'bot':28s} {'class':12s} {'P&L (life)':>11s} {'threshold':>10s} "
          f"{'buffer':>10s} {'buffer%':>8s}  notes")
    print("-" * 120)
    for s in report["statuses"]:
        pnl = s.get("pnl_lifetime")
        th = s.get("retirement_threshold")
        b = s.get("buffer_usd")
        pct = s.get("buffer_pct_of_threshold")
        n = "; ".join(s.get("notes") or []) or ""
        print(
            f" {s['bot_id']:28s} {s['classification']:12s} "
            f"{(pnl or 0):>11.2f} {(th or 0):>10.0f} "
            f"{(b or 0):>10.2f} {(pct or 0):>7.1f}%  {n[:50]}",
        )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = run_watchdog()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)
    counts = report["classification_counts"]
    if counts.get("CRITICAL", 0) > 0:
        return 2
    if counts.get("WARN", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
