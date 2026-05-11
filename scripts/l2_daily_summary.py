"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_daily_summary
=======================================================
One-line-per-strategy daily status report — operator's morning
review without grep-ing 12 JSONL files.

Why this exists
---------------
The L2 stack writes to:
  - l2_backtest_runs.jsonl
  - l2_sweep_runs.jsonl
  - l2_promotion_decisions.jsonl
  - l2_fill_audit.jsonl
  - l2_drift_monitor.jsonl
  - l2_calibration.jsonl
  - l2_risk_metrics.jsonl
  - l2_correlation.jsonl
  - l2_universe_audit.jsonl
  - l2_heartbeat.jsonl
  - l2_reconciliation.jsonl
  - capture_health.jsonl
  - alerts_log.jsonl

The operator can't read all of these every morning.  This module
synthesizes the latest entry from each into a one-line per-strategy
status, plus a top-level OVERALL verdict.

Output
------
- Text summary to stdout
- JSON output for cron/email integration
- Optional Slack-formatted block

Run
---
::

    python -m eta_engine.scripts.l2_daily_summary
    python -m eta_engine.scripts.l2_daily_summary --json
    python -m eta_engine.scripts.l2_daily_summary --slack
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import contextlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DAILY_SUMMARY_LOG = LOG_DIR / "l2_daily_summary.jsonl"


@dataclass
class StrategyLine:
    bot_id: str
    strategy_id: str
    symbol: str
    promotion_status: str
    recommended_status: str
    latest_sharpe: float | None
    latest_n_trades: int | None
    drift_verdict: str | None
    calibration_brier: float | None
    fill_audit_verdict: str | None
    notes: list[str] = field(default_factory=list)


@dataclass
class DailySummary:
    ts: str
    overall_verdict: str  # GREEN | YELLOW | RED
    n_strategies: int
    n_alerts_last_24h: int
    capture_health: str | None  # GREEN | YELLOW | RED | NEVER
    heartbeat_n_alive: int
    heartbeat_n_total: int
    strategies: list[StrategyLine] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)


def _last_jsonl_record(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _filter_jsonl_by_field(path: Path, *, field_name: str,
                              field_value: str) -> dict | None:
    """Get the latest jsonl entry where rec[field_name] == field_value."""
    if not path.exists():
        return None
    latest: dict | None = None
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
                if rec.get(field_name) == field_value:
                    latest = rec
    except OSError:
        return None
    return latest


def build_summary() -> DailySummary:
    from eta_engine.strategies.l2_strategy_registry import (
        L2_STRATEGIES,
    )
    capture_latest = _last_jsonl_record(LOG_DIR / "capture_health.jsonl")
    capture_status = (capture_latest.get("verdict") if capture_latest
                        else "NEVER")

    heartbeat_latest_lines: list[dict] = []
    hb_path = LOG_DIR / "l2_heartbeat.jsonl"
    if hb_path.exists():
        try:
            with hb_path.open("r", encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()][-20:]
            for ln in lines:
                with contextlib.suppress(json.JSONDecodeError):
                    heartbeat_latest_lines.append(json.loads(ln))
        except OSError:
            pass
    n_alive = sum(1 for r in heartbeat_latest_lines if r.get("alive"))
    n_total = len(heartbeat_latest_lines)

    strategies: list[StrategyLine] = []
    n_red = 0
    n_yellow = 0
    headlines: list[str] = []

    for entry in L2_STRATEGIES:
        # Pull the latest promotion decision for this bot
        promo_path = LOG_DIR / "l2_promotion_decisions.jsonl"
        promo = _filter_jsonl_by_field(
            promo_path, field_name="bot_id", field_value=entry.bot_id)
        recommended = promo.get("recommended_status") if promo else entry.promotion_status

        # Latest backtest stats
        bt_path = LOG_DIR / "l2_backtest_runs.jsonl"
        # Match by harness strategy name (strip _v1)
        harness = entry.strategy_id.removesuffix("_v1") \
                    if entry.strategy_id.endswith("_v1") else entry.strategy_id
        bt = None
        if bt_path.exists():
            try:
                with bt_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (rec.get("strategy") == harness
                                and rec.get("symbol") == entry.symbol):
                            bt = rec
            except OSError:
                pass

        # Drift verdict
        drift = _filter_jsonl_by_field(
            LOG_DIR / "l2_drift_monitor.jsonl",
            field_name="strategy", field_value=harness)
        drift_verdict = drift.get("drift_verdict") if drift else None

        # Calibration
        cal = _filter_jsonl_by_field(
            LOG_DIR / "l2_calibration.jsonl",
            field_name="strategy_id", field_value=entry.strategy_id)
        cal_brier = cal.get("brier_score") if cal else None

        # Fill audit (per-strategy not currently aggregated; use overall)
        fa = _last_jsonl_record(LOG_DIR / "l2_fill_audit.jsonl")
        fa_verdict = fa.get("overall_verdict") if fa else None

        line = StrategyLine(
            bot_id=entry.bot_id,
            strategy_id=entry.strategy_id,
            symbol=entry.symbol,
            promotion_status=entry.promotion_status,
            recommended_status=str(recommended),
            latest_sharpe=bt.get("sharpe_proxy") if bt else None,
            latest_n_trades=bt.get("n_trades") if bt else None,
            drift_verdict=drift_verdict,
            calibration_brier=cal_brier,
            fill_audit_verdict=fa_verdict,
        )
        # Classify
        if recommended == "retired":
            n_red += 1
            headlines.append(f"{entry.bot_id}: RETIREMENT TRIGGERED")
        elif drift_verdict in ("DRIFTING", "CRITICAL"):
            n_yellow += 1
            headlines.append(f"{entry.bot_id}: drift = {drift_verdict}")
        elif fa_verdict == "FAIL":
            n_yellow += 1
            headlines.append(f"{entry.bot_id}: fill audit FAIL")
        strategies.append(line)

    # Recent alerts (last 24h)
    alerts_path = LOG_DIR / "alerts_log.jsonl"
    n_alerts = 0
    if alerts_path.exists():
        cutoff = datetime.now(UTC).timestamp() - 24 * 3600
        try:
            with alerts_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp_utc") or rec.get("ts")
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if dt.timestamp() >= cutoff:
                        n_alerts += 1
        except OSError:
            pass

    # Overall verdict
    if n_red > 0:
        overall = "RED"
    elif n_yellow > 0 or n_alerts > 5 or capture_status not in ("GREEN", "NEVER"):
        overall = "YELLOW"
    elif n_alive < n_total:
        overall = "YELLOW"
        headlines.append(f"{n_total - n_alive} daemon(s) not heartbeating")
    else:
        overall = "GREEN"

    return DailySummary(
        ts=datetime.now(UTC).isoformat(),
        overall_verdict=overall,
        n_strategies=len(strategies),
        n_alerts_last_24h=n_alerts,
        capture_health=capture_status,
        heartbeat_n_alive=n_alive,
        heartbeat_n_total=n_total,
        strategies=strategies,
        headlines=headlines,
    )


def format_slack(summary: DailySummary) -> str:
    """Format as Slack message — uses :emoji: + simple formatting."""
    emoji = {"GREEN": ":white_check_mark:", "YELLOW": ":warning:",
              "RED": ":rotating_light:"}.get(summary.overall_verdict, ":question:")
    lines = [
        f"{emoji} *L2 Daily Summary* ({summary.ts})",
        f"   Overall: *{summary.overall_verdict}*",
        f"   Capture: {summary.capture_health}",
        f"   Heartbeats: {summary.heartbeat_n_alive}/{summary.heartbeat_n_total}",
        f"   Alerts 24h: {summary.n_alerts_last_24h}",
        "",
    ]
    if summary.headlines:
        lines.append("*Headlines:*")
        for h in summary.headlines:
            lines.append(f"  • {h}")
        lines.append("")
    lines.append("*Strategies:*")
    for s in summary.strategies:
        rec_emoji = (":soon:" if s.recommended_status != s.promotion_status
                      else ":white_check_mark:")
        sharpe = (f"sharpe={s.latest_sharpe}" if s.latest_sharpe is not None
                    else "(no sharpe yet)")
        lines.append(f"  {rec_emoji} `{s.bot_id}` "
                       f"[{s.promotion_status}→{s.recommended_status}] {sharpe}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--slack", action="store_true")
    args = ap.parse_args()

    summary = build_summary()
    try:
        with DAILY_SUMMARY_LOG.open("a", encoding="utf-8") as f:
            d = asdict(summary)
            d.pop("strategies", None)  # trim per-strategy detail from log
            f.write(json.dumps(d, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: daily summary log write failed: {e}", file=sys.stderr)

    if args.slack:
        print(format_slack(summary))
        return 0
    if args.json:
        print(json.dumps(asdict(summary), indent=2))
        return 0

    print()
    print("=" * 78)
    print(f"L2 DAILY SUMMARY  ({summary.ts})")
    print("=" * 78)
    print(f"  OVERALL          : {summary.overall_verdict}")
    print(f"  capture health   : {summary.capture_health}")
    print(f"  heartbeats       : {summary.heartbeat_n_alive}/"
            f"{summary.heartbeat_n_total} alive")
    print(f"  alerts (last 24h): {summary.n_alerts_last_24h}")
    print()
    if summary.headlines:
        print("  Headlines:")
        for h in summary.headlines:
            print(f"    - {h}")
        print()
    print(f"  {'Bot ID':<35s} {'Status→Rec':<22s} {'Sharpe':<8s} {'n_tr':<6s} {'Drift':<10s}")
    print(f"  {'-'*35:<35s} {'-'*22:<22s} {'-'*8:<8s} {'-'*6:<6s} {'-'*10}")
    for s in summary.strategies:
        sharpe_str = f"{s.latest_sharpe:+.3f}" if s.latest_sharpe is not None else "n/a"
        n_str = str(s.latest_n_trades) if s.latest_n_trades is not None else "n/a"
        drift = s.drift_verdict or "n/a"
        status_rec = f"{s.promotion_status}→{s.recommended_status}"
        print(f"  {s.bot_id:<35s} {status_rec:<22s} {sharpe_str:<8s} {n_str:<6s} {drift:<10s}")
    print()
    return 0 if summary.overall_verdict == "GREEN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
