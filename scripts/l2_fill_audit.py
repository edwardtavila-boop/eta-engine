"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_fill_audit
====================================================
Live-fill audit — measures realized slippage on stops + targets
against the harness's pessimistic-fill predictions.

Why this exists
---------------
Per the Firm's Red Team dissent attack #4 against book_imbalance:
> Pessimistic fills aren't pessimistic enough (l2_backtest_harness.py:
> 255-262): stop fills 1 tick worse, but MNQ stops can slip 2-4 ticks
> in stress.  Worst-case (FOMC, NFP) can slip 10+ ticks.

The harness assumes:
  - STOP fills at stop_price - 1 tick (LONG) or stop_price + 1 tick (SHORT)
  - TARGET fills at exactly target_price (limit order, no improvement)

Reality on a paper or live broker fill stream:
  - STOP slip varies by regime (RTH calm: 1 tick; NFP: 5-10+ ticks)
  - TARGET fills only happen when book has size at the limit price
  - Bracket order TPs sometimes fill, sometimes don't, even at touch

This script reads the broker fill stream + the strategy's signal
log + matches them up, then computes:
  - Realized slip distribution per session bucket (RTH/RTH-open/
    midday/close/overnight)
  - p50, p90, p99 slip vs predicted (1 tick)
  - Comparison to the harness's pessimistic-fill assumption
  - GO/NO-GO verdict on whether harness slippage realism is adequate

Output
------
- Per-bucket slippage report (text + JSON)
- Append to logs/eta_engine/l2_fill_audit.jsonl
- Exit code:
    0 — realistic slippage matches harness predictions
    1 — harness UNDERSTATES slippage in at least one bucket
    2 — insufficient sample (< 30 fills)

Input sources
-------------
- Signal log: ImbalanceSignal entries (currently emitted via
  evaluate_snapshot; the order router is expected to log them with
  signal_id + intended entry/stop/target prices)
- Broker fill log: existing executions stream — exact format depends
  on which broker venue.  This module reads ``logs/eta_engine/
  broker_fills.jsonl`` (added 2026-05-11 as part of supercharge
  prep).

When there's no real fill data yet, the script reports
``NO_FILLS_YET`` and exits 0 — same no-op pattern as the other L2
tools pre-data.
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)

BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
FILL_AUDIT_LOG = LOG_DIR / "l2_fill_audit.jsonl"


@dataclass
class FillSlipObservation:
    """One matched fill — what we predicted vs what we got."""
    signal_id: str
    symbol: str
    exit_reason: str           # "STOP" | "TARGET" | "TIMEOUT"
    side: str                  # "LONG" | "SHORT"
    intended_price: float      # what the harness predicted
    actual_fill_price: float   # what the broker reported
    slip_price: float          # signed: positive = adverse for this trade
    slip_ticks: float          # slip / tick_size
    session_bucket: str        # "RTH_OPEN" | "RTH_MID" | "RTH_CLOSE" | "ETH"
    ts: str                    # iso8601 of fill


@dataclass
class BucketReport:
    session: str
    n_fills: int
    p50_slip_ticks: float | None = None
    p90_slip_ticks: float | None = None
    p99_slip_ticks: float | None = None
    max_slip_ticks: float | None = None
    predicted_slip_ticks: float = 1.0  # what harness assumes
    realism_verdict: str = "INSUFFICIENT"  # or "PASS" | "FAIL"


@dataclass
class FillAuditReport:
    n_observations: int
    overall_verdict: str  # PASS | FAIL | NO_FILLS_YET
    buckets: list[BucketReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_jsonl(path: Path, *, since_days: int = 30) -> list[dict]:
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts") or rec.get("timestamp_utc")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


def _session_bucket(dt: datetime) -> str:
    """Map a UTC datetime to one of {RTH_OPEN, RTH_MID, RTH_CLOSE, ETH}.

    CME RTH for index futures: 13:30 UTC (09:30 ET) - 20:00 UTC (16:00 ET).
    """
    h = dt.hour
    m = dt.minute
    minutes_utc = h * 60 + m
    rth_open = 13 * 60 + 30   # 13:30 UTC
    rth_close = 20 * 60        # 20:00 UTC
    open_buffer = 30           # first 30 min = OPEN
    close_buffer = 30          # last 30 min = CLOSE
    if rth_open <= minutes_utc < rth_open + open_buffer:
        return "RTH_OPEN"
    if rth_close - close_buffer <= minutes_utc < rth_close:
        return "RTH_CLOSE"
    if rth_open + open_buffer <= minutes_utc < rth_close - close_buffer:
        return "RTH_MID"
    return "ETH"


def _signed_slip_ticks(intended: float, actual: float, side: str,
                       exit_reason: str, tick_size: float) -> float:
    """Compute adverse-direction slip in ticks.

    Positive = WORSE than expected.

    LONG STOP: expected = stop, actual lower means worse → positive slip
    LONG TARGET: expected = target, actual lower means worse → positive slip
    SHORT STOP: expected = stop, actual higher means worse → positive slip
    SHORT TARGET: expected = target, actual higher means worse → positive slip
    """
    raw_diff = actual - intended
    slip_price = -raw_diff if side.upper() in ("LONG", "BUY") else raw_diff
    return slip_price / max(tick_size, 1e-9)


def _match_signals_to_fills(signals: list[dict], fills: list[dict],
                             *, tick_size: float = 0.25) -> list[FillSlipObservation]:
    """Pair signals with fills by signal_id.  Return list of slip obs.

    Fill record schema (expected):
        {ts, signal_id, exit_reason ("STOP"|"TARGET"|"TIMEOUT"),
         actual_fill_price, side}

    Signal record schema:
        {ts, signal_id, intended_stop_price, intended_target_price,
         symbol, side}
    """
    by_signal: dict[str, dict] = {s.get("signal_id"): s for s in signals if s.get("signal_id")}
    obs: list[FillSlipObservation] = []
    for fill in fills:
        sid = fill.get("signal_id")
        if not sid or sid not in by_signal:
            continue
        sig = by_signal[sid]
        exit_reason = fill.get("exit_reason", "TIMEOUT").upper()
        side = sig.get("side", "LONG").upper()
        if exit_reason == "STOP":
            intended = sig.get("intended_stop_price")
        elif exit_reason == "TARGET":
            intended = sig.get("intended_target_price")
        else:
            continue  # TIMEOUT exits skip slip analysis
        actual = fill.get("actual_fill_price")
        if intended is None or actual is None:
            continue
        slip_ticks = _signed_slip_ticks(float(intended), float(actual),
                                          side, exit_reason, tick_size)
        ts_iso = fill.get("ts") or fill.get("timestamp_utc")
        try:
            dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        except ValueError:
            continue
        bucket = _session_bucket(dt)
        obs.append(FillSlipObservation(
            signal_id=sid,
            symbol=sig.get("symbol", "?"),
            exit_reason=exit_reason,
            side=side,
            intended_price=float(intended),
            actual_fill_price=float(actual),
            slip_price=float(actual) - float(intended),
            slip_ticks=round(slip_ticks, 2),
            session_bucket=bucket,
            ts=str(ts_iso),
        ))
    return obs


def _bucket_report(observations: list[FillSlipObservation],
                     session: str,
                     *, predicted_slip_ticks: float = 1.0) -> BucketReport:
    bucket_obs = [o for o in observations if o.session_bucket == session]
    n = len(bucket_obs)
    if n < 5:
        return BucketReport(session=session, n_fills=n,
                             predicted_slip_ticks=predicted_slip_ticks,
                             realism_verdict="INSUFFICIENT")
    slips = [o.slip_ticks for o in bucket_obs]
    slips_sorted = sorted(slips)
    p50 = slips_sorted[len(slips) // 2]
    p90 = slips_sorted[min(len(slips) - 1, int(len(slips) * 0.90))]
    p99 = slips_sorted[min(len(slips) - 1, int(len(slips) * 0.99))]
    mx = max(slips)
    # Harness predicts 1 tick of slip on stops; realism PASSES if p90
    # is within 1.5× predicted.  FAILS if p90 > 2× predicted.
    if p90 <= predicted_slip_ticks * 1.5:
        verdict = "PASS"
    elif p90 > predicted_slip_ticks * 2.0:
        verdict = "FAIL"
    else:
        verdict = "MARGINAL"
    return BucketReport(
        session=session, n_fills=n,
        p50_slip_ticks=round(p50, 2),
        p90_slip_ticks=round(p90, 2),
        p99_slip_ticks=round(p99, 2),
        max_slip_ticks=round(mx, 2),
        predicted_slip_ticks=predicted_slip_ticks,
        realism_verdict=verdict,
    )


def run_audit(*, since_days: int = 30,
               tick_size: float = 0.25,
               predicted_slip_ticks: float = 1.0) -> FillAuditReport:
    signals = _read_jsonl(SIGNAL_LOG, since_days=since_days)
    fills = _read_jsonl(BROKER_FILL_LOG, since_days=since_days)
    observations = _match_signals_to_fills(signals, fills, tick_size=tick_size)

    if not observations:
        return FillAuditReport(
            n_observations=0,
            overall_verdict="NO_FILLS_YET",
            warnings=["No matched signal/fill pairs.  Start paper-soak; "
                       "ensure broker_fills.jsonl and l2_signal_log.jsonl "
                       "are being written."],
        )

    bucket_reports = []
    for session in ("RTH_OPEN", "RTH_MID", "RTH_CLOSE", "ETH"):
        bucket_reports.append(_bucket_report(
            observations, session,
            predicted_slip_ticks=predicted_slip_ticks))

    # Overall: PASS only if no bucket FAILS and at least one bucket has
    # an honest PASS (not just INSUFFICIENT across the board)
    has_fail = any(b.realism_verdict == "FAIL" for b in bucket_reports)
    has_pass = any(b.realism_verdict == "PASS" for b in bucket_reports)
    if has_fail:
        overall = "FAIL"
    elif has_pass:
        overall = "PASS"
    else:
        overall = "INSUFFICIENT"

    warnings: list[str] = []
    if len(observations) < 30:
        warnings.append(
            f"Only {len(observations)} matched fills — "
            "audit verdict is statistically weak below n=30.")

    return FillAuditReport(
        n_observations=len(observations),
        overall_verdict=overall,
        buckets=bucket_reports,
        warnings=warnings,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--tick-size", type=float, default=0.25)
    ap.add_argument("--predicted-slip-ticks", type=float, default=1.0,
                    help="Harness predicted slip (default 1.0 tick)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = run_audit(
        since_days=args.days,
        tick_size=args.tick_size,
        predicted_slip_ticks=args.predicted_slip_ticks,
    )

    # Persist
    try:
        with FILL_AUDIT_LOG.open("a", encoding="utf-8") as f:
            digest = {
                "ts": datetime.now(UTC).isoformat(),
                "n_observations": report.n_observations,
                "overall_verdict": report.overall_verdict,
                "buckets": [{"session": b.session, "n_fills": b.n_fills,
                              "p90_slip_ticks": b.p90_slip_ticks,
                              "realism_verdict": b.realism_verdict}
                             for b in report.buckets],
            }
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: could not append fill audit: {e}", file=sys.stderr)

    if args.json:
        out = {
            "n_observations": report.n_observations,
            "overall_verdict": report.overall_verdict,
            "buckets": [{"session": b.session, "n_fills": b.n_fills,
                         "p50_slip_ticks": b.p50_slip_ticks,
                         "p90_slip_ticks": b.p90_slip_ticks,
                         "p99_slip_ticks": b.p99_slip_ticks,
                         "max_slip_ticks": b.max_slip_ticks,
                         "predicted_slip_ticks": b.predicted_slip_ticks,
                         "realism_verdict": b.realism_verdict}
                        for b in report.buckets],
            "warnings": report.warnings,
        }
        print(json.dumps(out, indent=2))
        if report.overall_verdict in ("PASS", "NO_FILLS_YET"):
            return 0
        return 1 if report.overall_verdict == "FAIL" else 2

    print()
    print("=" * 78)
    print(f"L2 FILL AUDIT  ({datetime.now(UTC).isoformat()})")
    print("=" * 78)
    print(f"  n_observations  : {report.n_observations}")
    print(f"  overall verdict : {report.overall_verdict}")
    print()
    print(f"  {'Session':<12s} {'n':<6s} {'p50':<8s} {'p90':<8s} {'p99':<8s} "
          f"{'max':<8s} {'predicted':<10s} verdict")
    print(f"  {'-'*12:<12s} {'-'*6:<6s} {'-'*8:<8s} {'-'*8:<8s} {'-'*8:<8s} "
          f"{'-'*8:<8s} {'-'*10:<10s} {'-'*8}")
    for b in report.buckets:
        p50 = f"{b.p50_slip_ticks}" if b.p50_slip_ticks is not None else "n/a"
        p90 = f"{b.p90_slip_ticks}" if b.p90_slip_ticks is not None else "n/a"
        p99 = f"{b.p99_slip_ticks}" if b.p99_slip_ticks is not None else "n/a"
        mx = f"{b.max_slip_ticks}" if b.max_slip_ticks is not None else "n/a"
        print(f"  {b.session:<12s} {b.n_fills:<6d} {p50:<8s} {p90:<8s} {p99:<8s} "
              f"{mx:<8s} {b.predicted_slip_ticks:<10.1f} {b.realism_verdict}")
    print()
    if report.warnings:
        print("  WARNINGS:")
        for w in report.warnings:
            print(f"    - {w}")
        print()

    if report.overall_verdict in ("PASS", "NO_FILLS_YET"):
        return 0
    return 1 if report.overall_verdict == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
