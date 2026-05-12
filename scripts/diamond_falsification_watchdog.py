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
ALERTS_LOG = LOG_DIR / "alerts_log.jsonl"

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
    # Promoted 2026-05-12 from canonical-data kaizen analysis.
    # m2k_sweep_reclaim: n=1151 cum_r=+533R wr=70% across all 4 sessions.
    # Conservative threshold: -$800 ≈ 2.5σ below the +0.46R/trade baseline
    # over a typical 50-trade 30-day window. Tighten after live observation.
    "m2k_sweep_reclaim":  -800.0,
}

#: R-multiple retirement thresholds — complementary to USD basis.
#:
#: WHY this exists (wave-7 kaizen, 2026-05-12):
#: The USD ledger has documented partial breakage: eur_sweep_reclaim is
#: all-quarantined ($0 USD, +129R R-truth), and mnq/nq_futures_sage have
#: pre-rollout records missing realized_pnl entirely ($0 USD, +0.85R/+0.82R
#: R-truth). Classifying purely by USD gives misleading INCONCLUSIVE/
#: CRITICAL flags for bots whose strategy edge is fine but whose dollar
#: accounting hit a data quality issue.
#:
#: R-multiples are dimension-free and immune to position-sizing artifacts.
#: The watchdog now classifies by BOTH bases and reports the worst of the
#: two as the canonical verdict. If USD says CRITICAL but R says HEALTHY,
#: the operator sees both and can investigate sizing vs strategy.
#:
#: Threshold setting — rough rule:
#:   - Strong proven diamonds (>+30R lifetime):    -10R or -20R
#:   - Marginal-large-sample (e.g. mnq/nq sage):    -1R
#:   - Small-sample bots (n<20):                    -3R
RETIREMENT_THRESHOLDS_R: dict[str, float] = {
    "mnq_futures_sage":  -1.0,   # marginal edge; tight R-floor
    "nq_futures_sage":   -1.0,
    "cl_momentum":       -5.0,   # small-sample bleeders
    "mcl_sweep_reclaim": -3.0,
    "mgc_sweep_reclaim": -5.0,
    "eur_sweep_reclaim": -10.0,  # strong proven diamond
    "gc_momentum":       -3.0,   # small sample, generous
    "cl_macro":          -2.0,
    "m2k_sweep_reclaim": -20.0,  # strongest evidence in fleet
}


@dataclass
class DiamondStatus:
    bot_id: str
    pnl_lifetime: float | None = None
    pnl_recent_window: float | None = None
    retirement_threshold: float | None = None
    buffer_usd: float | None = None
    buffer_pct_of_threshold: float | None = None
    # ── Wave-7 R-multiple basis ───────────────────────────────────────
    cumulative_r: float | None = None
    retirement_threshold_r: float | None = None
    buffer_r: float | None = None
    classification_usd: str = "INCONCLUSIVE"
    classification_r: str = "INCONCLUSIVE"
    classification: str = "INCONCLUSIVE"  # worst-of-both (canonical)
    notes: list[str] = field(default_factory=list)


def _load_ledger() -> dict | None:
    if not CLOSED_LEDGER.exists():
        return None
    try:
        return json.loads(CLOSED_LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _classify_buffer(buffer: float | None, threshold: float | None) -> str:
    """Map a (buffer, threshold) pair to a 4-band classification.

    buffer = (current_metric - retirement_threshold).  Threshold is the
    negative floor.  When buffer<=0 the bot is below or at its floor;
    larger positive buffer = more headroom.
    """
    if buffer is None or threshold is None:
        return "INCONCLUSIVE"
    threshold_mag = abs(threshold)
    if threshold_mag == 0:
        return "INCONCLUSIVE"
    if buffer <= 0:
        return "CRITICAL"
    pct = buffer / threshold_mag * 100.0
    if pct < 20:
        return "WARN"
    if pct < 50:
        return "WATCH"
    return "HEALTHY"


def _worst_of(usd_cls: str, r_cls: str) -> str:
    """Return the more-conservative of two classifications.

    Order from worst to best:  CRITICAL > WARN > WATCH > HEALTHY > INCONCLUSIVE.
    INCONCLUSIVE is treated as "no evidence in this basis" — if the OTHER
    basis has a verdict, that wins (e.g., USD=INCONCLUSIVE + R=HEALTHY =>
    HEALTHY, not INCONCLUSIVE).
    """
    rank = {
        "CRITICAL": 4,
        "WARN": 3,
        "WATCH": 2,
        "HEALTHY": 1,
        "INCONCLUSIVE": 0,
    }
    if usd_cls == "INCONCLUSIVE":
        return r_cls
    if r_cls == "INCONCLUSIVE":
        return usd_cls
    return max((usd_cls, r_cls), key=lambda c: rank.get(c, 0))


def _classify(status: DiamondStatus) -> None:
    """Compute the USD, R, and worst-of-both classifications."""
    status.classification_usd = _classify_buffer(
        status.buffer_usd, status.retirement_threshold,
    )
    status.classification_r = _classify_buffer(
        status.buffer_r, status.retirement_threshold_r,
    )
    status.classification = _worst_of(
        status.classification_usd, status.classification_r,
    )
    if status.buffer_usd is not None and status.retirement_threshold:
        status.buffer_pct_of_threshold = round(
            status.buffer_usd / abs(status.retirement_threshold) * 100.0, 1,
        )


def _evaluate(bot_id: str, ledger: dict | None) -> DiamondStatus:
    s = DiamondStatus(bot_id=bot_id)
    s.retirement_threshold = RETIREMENT_THRESHOLDS_USD.get(bot_id)
    s.retirement_threshold_r = RETIREMENT_THRESHOLDS_R.get(bot_id)
    if s.retirement_threshold is None and s.retirement_threshold_r is None:
        s.notes.append("no retirement threshold defined (USD or R)")
        s.classification = "INCONCLUSIVE"
        return s
    if ledger is None:
        s.notes.append("closed_trade_ledger missing")
        return s
    rec = ledger.get("per_bot", {}).get(bot_id)
    if rec is None:
        s.notes.append("bot not in ledger")
        return s

    # ── Parse USD lifetime PnL ────────────────────────────────────────
    try:
        s.pnl_lifetime = float(rec.get("total_realized_pnl") or 0)
        # The closed_trade_ledger doesn't carry a 30-day window directly;
        # use lifetime as an upper-bound proxy until the rolling pipeline
        # is wired.  When pipeline arrives, replace with the actual 30d
        # rolling P&L from kaizen reports.
        s.pnl_recent_window = s.pnl_lifetime
    except (TypeError, ValueError) as exc:
        s.notes.append(f"pnl parse error: {exc}")

    # ── Parse cumulative R-multiple ───────────────────────────────────
    try:
        s.cumulative_r = float(rec.get("cumulative_r") or 0)
    except (TypeError, ValueError) as exc:
        s.notes.append(f"cumulative_r parse error: {exc}")

    # Scale-bug detection (USD basis only — R-multiples are immune).
    # If the USD basis looks scale-bugged, we silently mark USD as
    # INCONCLUSIVE but keep the R-basis verdict.  Pre-wave-7 the entire
    # bot was marked INCONCLUSIVE when USD was suspect; that hid bots
    # whose strategy edge was actually fine but whose dollar tracking
    # tripped on a data quality issue (eur_sweep_reclaim post-sanitizer).
    n_trades = int(rec.get("closed_trade_count") or 0)
    scale_bug_suspected = (
        n_trades > 0
        and s.pnl_lifetime is not None
        and abs(s.pnl_lifetime) / max(n_trades, 1) > 5_000
    )
    if scale_bug_suspected:
        s.notes.append(
            "SCALE_BUG_SUSPECTED — avg per-trade exceeds $5,000; "
            "USD verdict reserved; R-basis verdict still computed",
        )
        # Don't compute USD buffer when scale-bugged; R basis carries us.
    elif s.pnl_recent_window is not None and s.retirement_threshold is not None:
        s.buffer_usd = round(
            s.pnl_recent_window - s.retirement_threshold, 2,
        )

    # R-buffer always computed when R-threshold + cumulative_r are present.
    if s.cumulative_r is not None and s.retirement_threshold_r is not None:
        s.buffer_r = round(s.cumulative_r - s.retirement_threshold_r, 4)

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
        "thresholds_usd": RETIREMENT_THRESHOLDS_USD,
        "thresholds_r": RETIREMENT_THRESHOLDS_R,
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
    # Fire alerts pipeline for CRITICAL classifications so the operator
    # dashboard surfaces them in the 24h alerts panel.
    _fire_alerts_for_critical(statuses)
    return report


def _fire_alerts_for_critical(statuses: list[DiamondStatus]) -> None:
    """Append one alert per CRITICAL diamond to the shared alerts log.

    The dashboard's daily summary counts alerts_log.jsonl entries in the
    last 24h, so writing here surfaces the diamond on the morning report
    even if the operator hasn't run the watchdog directly.

    Best-effort: any I/O failure logs to stderr and continues — the
    primary watchdog report is the authoritative record.
    """
    try:
        critical = [s for s in statuses if s.classification == "CRITICAL"]
        if not critical:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with ALERTS_LOG.open("a", encoding="utf-8") as f:
            for s in critical:
                f.write(json.dumps({
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "ts": datetime.now(UTC).isoformat(),
                    "severity": "RED",
                    "source": "diamond_falsification_watchdog",
                    "alert_id": f"diamond_critical_{s.bot_id}",
                    "bot_id": s.bot_id,
                    "headline": (
                        f"DIAMOND CRITICAL: {s.bot_id} P&L "
                        f"${s.pnl_recent_window or 0:+.2f} below "
                        f"floor ${s.retirement_threshold or 0:.0f} "
                        f"(buffer ${s.buffer_usd or 0:+.2f})"
                    ),
                    "buffer_usd": s.buffer_usd,
                    "buffer_pct_of_threshold": s.buffer_pct_of_threshold,
                    "retirement_threshold": s.retirement_threshold,
                    "pnl_recent_window": s.pnl_recent_window,
                    "next_action": (
                        "Operator review required.  Per "
                        "var/eta_engine/decisions/diamond_set_2026_05_12.md, "
                        "options are (1) retire — remove bot from "
                        "DIAMOND_BOTS and commit, (2) override — write "
                        "exception memo explaining why the floor is "
                        "unrepresentative."
                    ),
                }, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(f"WARN: alert write failed: {exc}", file=sys.stderr)


def _print(report: dict) -> None:
    print("=" * 130)
    print(f" DIAMOND FALSIFICATION WATCHDOG — {report['ts']}")
    print("=" * 130)
    print(" Classification roll-up (worst-of-USD+R): " + ", ".join(
        f"{k}={v}" for k, v in report["classification_counts"].items()))
    print()
    print(
        f" {'bot':25s} {'class':9s} | "
        f"{'usd_cls':10s} {'P&L':>10s} {'thr':>8s} {'buf':>9s} | "
        f"{'r_cls':10s} {'cum_R':>8s} {'thr_R':>7s} {'buf_R':>8s} | "
        f"notes",
    )
    print("-" * 130)
    for s in report["statuses"]:
        usd_cls = s.get("classification_usd") or "—"
        r_cls = s.get("classification_r") or "—"
        pnl = s.get("pnl_lifetime")
        th = s.get("retirement_threshold")
        b = s.get("buffer_usd")
        cum_r = s.get("cumulative_r")
        th_r = s.get("retirement_threshold_r")
        b_r = s.get("buffer_r")
        n = "; ".join(s.get("notes") or []) or ""
        # Format USD values, blanking when None (scale-bug suspect)
        pnl_s = f"{pnl:>10.0f}" if pnl is not None else f"{'—':>10s}"
        th_s = f"{th:>8.0f}" if th is not None else f"{'—':>8s}"
        b_s = f"{b:>9.0f}" if b is not None else f"{'—':>9s}"
        cum_r_s = f"{cum_r:>+8.2f}" if cum_r is not None else f"{'—':>8s}"
        th_r_s = f"{th_r:>+7.1f}" if th_r is not None else f"{'—':>7s}"
        b_r_s = f"{b_r:>+8.2f}" if b_r is not None else f"{'—':>8s}"
        print(
            f" {s['bot_id']:25s} {s['classification']:9s} | "
            f"{usd_cls:10s} {pnl_s} {th_s} {b_s} | "
            f"{r_cls:10s} {cum_r_s} {th_r_s} {b_r_s} | "
            f"{n[:40]}",
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
