"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_prop_drawdown_guard
====================================================================
Prop-fund $50K account drawdown + consistency guard.

Operator design (2026-05-12)
----------------------------
The PROP_READY top-3 run 24/7 on a $50K prop account.  Prop firms
enforce strict drawdown and consistency rules; violating them voids
the account.  This guard ENFORCES those rules at the operator-tool
layer so the supervisor can read a single signal (HALT / WATCH / OK)
before placing each next order.

Defaults are set for a typical $50K eval/funded account
(BluSky / Apex / Topstep style):

  - Daily trailing drawdown:  $1,500 (3% of $50K)
  - Static account drawdown:  $2,500 (5% of $50K)
  - Profit target (eval):     $3,000 (6% of $50K) [informational]
  - Consistency rule:         no single day's profit > 30% of total
                              accumulated profit (eval pass requirement)

Three signals
-------------
For each evaluation pass:

  HALT      — daily DD breached OR static DD breached
              The supervisor MUST stop all entries; existing positions
              should flatten per the operator's flat-on-halt rule.
  WATCH     — DD buffer < 25% of max OR consistency ratio > 25%
              Approaching limits; supervisor should reduce position
              size by half or skip marginal-confluence entries.
  OK        — comfortable headroom on all guards.

The signal is recomputed each time the daily P&L is updated by the
ledger refresh (every 15 min via the LedgerEvery15Min cron task).

What this does NOT do
---------------------
- Does NOT mutate any code or auto-flatten positions.  It writes a
  receipt the supervisor reads.  The supervisor (or a downstream
  emergency-flat task) is responsible for acting on HALT.
- Does NOT compute the prop allocation between bots.  That's
  diamond_prop_allocator.py's job.

Output
------
- stdout: per-rule status with USD buffers
- ``var/eta_engine/state/diamond_prop_drawdown_guard_latest.json``
- exit 0 if signal == OK
- exit 1 if signal == WATCH
- exit 2 if signal == HALT

Run
---
::

    python -m eta_engine.scripts.diamond_prop_drawdown_guard
    python -m eta_engine.scripts.diamond_prop_drawdown_guard --json
    python -m eta_engine.scripts.diamond_prop_drawdown_guard --account-size 100000
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
TRADE_CLOSES_CANONICAL = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"
TRADE_CLOSES_LEGACY = (
    WORKSPACE_ROOT
    / "eta_engine"
    / "state"  # HISTORICAL-PATH-OK
    / "jarvis_intel"
    / "trade_closes.jsonl"
)
OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_prop_drawdown_guard_latest.json"
#: Alerts log feeds the dashboard's daily summary + operator alerts.
ALERTS_LOG = WORKSPACE_ROOT / "logs" / "eta_engine" / "alerts_log.jsonl"
#: Halt flag file — supervisor reads this BEFORE every prop-fund entry.
#: When the file exists, prop-fund entries are BLOCKED. The flag is
#: re-emitted every cron cycle while signal=HALT; cleared otherwise.
PROP_HALT_FLAG_PATH = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "prop_halt_active.flag"
#: WATCH flag — supervisor halves position size while present.
PROP_WATCH_FLAG_PATH = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "prop_watch_active.flag"

#: Default prop account configuration ($50K eval/funded — typical
#: BluSky / Apex / Topstep style).
DEFAULT_ACCOUNT_SIZE = 50_000.0
DEFAULT_DAILY_DD_PCT = 0.03  # 3% = $1,500 on $50K
DEFAULT_STATIC_DD_PCT = 0.05  # 5% = $2,500 on $50K
DEFAULT_PROFIT_TARGET_PCT = 0.06  # 6% = $3,000 on $50K
DEFAULT_CONSISTENCY_PCT = 0.30  # max single-day profit ≤ 30% of total

#: WATCH band: when buffer drops below this fraction of max DD,
#: signal WATCH so the supervisor de-risks before the HALT threshold.
WATCH_BUFFER_FRACTION = 0.25  # WATCH when remaining DD headroom < 25%

#: Only PROP_READY bots count toward the prop-account DD ledger.
#: Other diamonds (TIER_DIAMOND but not PROP_READY) trade paper-only.


@dataclass
class GuardCheck:
    name: str
    limit_usd: float
    used_usd: float
    buffer_usd: float
    buffer_pct_of_limit: float
    status: str = "OK"  # OK / WATCH / HALT
    rationale: str = ""


@dataclass
class GuardReceipt:
    ts: str
    account_size: float
    prop_ready_bots: list[str] = field(default_factory=list)
    daily_pnl_usd: float = 0.0
    total_pnl_usd: float = 0.0
    today_iso: str = ""
    consistency_ratio: float | None = None
    daily_dd_check: GuardCheck | None = None
    static_dd_check: GuardCheck | None = None
    consistency_check: GuardCheck | None = None
    signal: str = "OK"  # OK / WATCH / HALT
    rationale: str = ""


# ────────────────────────────────────────────────────────────────────
# IO
# ────────────────────────────────────────────────────────────────────


def _load_prop_ready_bots() -> list[str]:
    sys.path.insert(0, str(WORKSPACE_ROOT))
    try:
        from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
            load_prop_ready_bots,
        )

        return sorted(load_prop_ready_bots())
    except ImportError:
        return []


def _read_trades_dual_source() -> list[dict[str, Any]]:
    """Same dual-source dedup pattern as the other diamond audits."""
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
                key = "|".join(
                    [
                        str(rec.get("signal_id") or ""),
                        str(rec.get("bot_id") or ""),
                        str(rec.get("ts") or ""),
                        str(rec.get("realized_r") or ""),
                    ]
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
    return out


# ────────────────────────────────────────────────────────────────────
# Math
# ────────────────────────────────────────────────────────────────────


def _today_utc_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _aggregate_pnl(
    trades: list[dict[str, Any]],
    prop_ready_bots: list[str],
    today_iso: str,
) -> tuple[float, float, dict[str, float]]:
    """Sum realized_pnl across PROP_READY bots.  Returns
    (today_pnl_usd, total_pnl_usd, per_day_pnl_dict)."""
    prop_set = set(prop_ready_bots)
    per_day: dict[str, float] = defaultdict(float)
    total = 0.0
    for t in trades:
        bid = t.get("bot_id")
        if bid not in prop_set:
            continue
        # Use sanitizer-aware PnL extraction (skip quarantined records)
        if t.get("_sanitizer_quarantined"):
            continue
        extra = t.get("extra") or {}
        if not isinstance(extra, dict):
            continue
        pnl = extra.get("realized_pnl")
        if pnl is None:
            continue
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        ts = t.get("ts") or ""
        if not isinstance(ts, str) or len(ts) < 10:
            continue
        day = ts[:10]
        per_day[day] += pnl_f
        total += pnl_f
    return per_day.get(today_iso, 0.0), total, dict(per_day)


def _check(name: str, used: float, limit: float) -> GuardCheck:
    """Build a single-rule check.  `used` is the negative-magnitude
    drawdown amount (positive number = how much we've drawn down)."""
    used_pos = max(used, 0.0)
    buffer = max(limit - used_pos, 0.0)
    buffer_pct = (buffer / limit * 100.0) if limit > 0 else 0.0
    chk = GuardCheck(
        name=name,
        limit_usd=round(limit, 2),
        used_usd=round(used_pos, 2),
        buffer_usd=round(buffer, 2),
        buffer_pct_of_limit=round(buffer_pct, 2),
    )
    if used_pos >= limit:
        chk.status = "HALT"
        chk.rationale = f"BREACHED ${used_pos:.2f} >= ${limit:.2f} limit — supervisor must HALT entries"
    elif buffer_pct < WATCH_BUFFER_FRACTION * 100.0:
        chk.status = "WATCH"
        chk.rationale = (
            f"buffer ${buffer:.2f} < {WATCH_BUFFER_FRACTION * 100:.0f}% of ${limit:.2f} limit — de-risk recommended"
        )
    else:
        chk.status = "OK"
        chk.rationale = f"buffer ${buffer:.2f} ({buffer_pct:.1f}% of ${limit:.0f} limit)"
    return chk


def _worst_signal(*signals: str) -> str:
    """Return the most severe signal: HALT > WATCH > OK."""
    rank = {"HALT": 2, "WATCH": 1, "OK": 0}
    return max(signals, key=lambda s: rank.get(s, 0))


def compute_guard(
    trades: list[dict[str, Any]],
    prop_ready_bots: list[str],
    account_size: float = DEFAULT_ACCOUNT_SIZE,
    daily_dd_pct: float = DEFAULT_DAILY_DD_PCT,
    static_dd_pct: float = DEFAULT_STATIC_DD_PCT,
    consistency_pct: float = DEFAULT_CONSISTENCY_PCT,
) -> GuardReceipt:
    """Build the guard receipt against the latest ledger + PROP_READY set."""
    today = _today_utc_iso()
    receipt = GuardReceipt(
        ts=datetime.now(UTC).isoformat(),
        account_size=account_size,
        prop_ready_bots=prop_ready_bots,
        today_iso=today,
    )

    if not prop_ready_bots:
        receipt.signal = "OK"  # no live bots = no exposure = OK
        receipt.rationale = "no PROP_READY bots — guard idle"
        return receipt

    today_pnl, total_pnl, per_day = _aggregate_pnl(
        trades,
        prop_ready_bots,
        today,
    )
    receipt.daily_pnl_usd = round(today_pnl, 2)
    receipt.total_pnl_usd = round(total_pnl, 2)

    # ── Daily DD ──────────────────────────────────────────────────
    daily_limit = account_size * daily_dd_pct
    receipt.daily_dd_check = _check(
        "daily_drawdown",
        used=-today_pnl,  # negative pnl = drawdown
        limit=daily_limit,
    )

    # ── Static account DD ─────────────────────────────────────────
    static_limit = account_size * static_dd_pct
    receipt.static_dd_check = _check(
        "static_drawdown",
        used=-total_pnl,  # cumulative loss
        limit=static_limit,
    )

    # ── Consistency rule ──────────────────────────────────────────
    # No single day's profit may exceed consistency_pct of total profit
    # at evaluation time.  We compute the ratio of the BEST day to the
    # total; if it exceeds consistency_pct, the eval would fail.
    consistency_check = GuardCheck(
        name="consistency",
        limit_usd=round(consistency_pct * 100, 2),  # the % itself
        used_usd=0.0,
        buffer_usd=0.0,
        buffer_pct_of_limit=100.0,
    )
    if total_pnl > 0 and per_day:
        best_day_pnl = max((p for p in per_day.values() if p > 0), default=0.0)
        ratio = best_day_pnl / total_pnl if total_pnl > 0 else 0.0
        receipt.consistency_ratio = round(ratio, 4)
        consistency_check.used_usd = round(ratio * 100, 2)
        consistency_check.buffer_usd = round(
            max((consistency_pct - ratio) * 100, 0.0),
            2,
        )
        consistency_check.buffer_pct_of_limit = round(
            max((consistency_pct - ratio) / consistency_pct * 100, 0.0),
            2,
        )
        if ratio >= consistency_pct:
            consistency_check.status = "HALT"
            consistency_check.rationale = (
                f"BREACHED — best day ratio {ratio:.2%} >= {consistency_pct:.0%} limit (eval would fail)"
            )
        elif (consistency_pct - ratio) / consistency_pct < WATCH_BUFFER_FRACTION:
            consistency_check.status = "WATCH"
            consistency_check.rationale = (
                f"approaching limit — best day {ratio:.2%}, "
                f"{(consistency_pct - ratio):.2%} buffer to {consistency_pct:.0%}"
            )
        else:
            consistency_check.rationale = f"best day {ratio:.2%} of total profit (limit {consistency_pct:.0%})"
    else:
        consistency_check.rationale = "no positive total P&L yet — consistency rule not in scope"
    receipt.consistency_check = consistency_check

    # ── Worst-of all guards drives the master signal ──────────────
    receipt.signal = _worst_signal(
        receipt.daily_dd_check.status,
        receipt.static_dd_check.status,
        receipt.consistency_check.status,
    )
    if receipt.signal == "HALT":
        breached = [
            c.name
            for c in (
                receipt.daily_dd_check,
                receipt.static_dd_check,
                receipt.consistency_check,
            )
            if c and c.status == "HALT"
        ]
        receipt.rationale = f"HALT — {', '.join(breached)} breached"
    elif receipt.signal == "WATCH":
        watching = [
            c.name
            for c in (
                receipt.daily_dd_check,
                receipt.static_dd_check,
                receipt.consistency_check,
            )
            if c and c.status == "WATCH"
        ]
        receipt.rationale = (
            f"WATCH — {', '.join(watching)} approaching limits; de-risk or skip marginal-confluence entries"
        )
    else:
        receipt.rationale = "OK — all guards have comfortable headroom"
    return receipt


def run(
    account_size: float = DEFAULT_ACCOUNT_SIZE,
    daily_dd_pct: float = DEFAULT_DAILY_DD_PCT,
    static_dd_pct: float = DEFAULT_STATIC_DD_PCT,
    consistency_pct: float = DEFAULT_CONSISTENCY_PCT,
) -> dict[str, Any]:
    prop_ready = _load_prop_ready_bots()
    trades = _read_trades_dual_source()
    receipt = compute_guard(
        trades=trades,
        prop_ready_bots=prop_ready,
        account_size=account_size,
        daily_dd_pct=daily_dd_pct,
        static_dd_pct=static_dd_pct,
        consistency_pct=consistency_pct,
    )
    summary = asdict(receipt)
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)

    # Wave-22: emit flag files + alert events so the supervisor and
    # dashboard pick up HALT/WATCH state without polling the JSON.
    _emit_signal_flags(receipt)
    _fire_alerts(receipt)

    return summary


# ────────────────────────────────────────────────────────────────────
# Wave-22: flag files + alerts pipeline
# ────────────────────────────────────────────────────────────────────


def _emit_signal_flags(receipt: GuardReceipt) -> None:
    """Write/clear flag files based on the master signal.

    Supervisor checks for these files BEFORE every prop-fund entry:
      - prop_halt_active.flag present  -> reject ALL prop entries
      - prop_watch_active.flag present -> halve qty on prop entries
      - neither present                -> normal sizing

    Idempotent: writes/clears each tick so the FS reflects current state.
    """
    try:
        PROP_HALT_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if receipt.signal == "HALT":
            PROP_HALT_FLAG_PATH.write_text(
                json.dumps(
                    {
                        "ts": receipt.ts,
                        "rationale": receipt.rationale,
                        "prop_ready_bots": receipt.prop_ready_bots,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            if PROP_WATCH_FLAG_PATH.exists():
                PROP_WATCH_FLAG_PATH.unlink()
        elif receipt.signal == "WATCH":
            PROP_WATCH_FLAG_PATH.write_text(
                json.dumps(
                    {
                        "ts": receipt.ts,
                        "rationale": receipt.rationale,
                        "prop_ready_bots": receipt.prop_ready_bots,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            if PROP_HALT_FLAG_PATH.exists():
                PROP_HALT_FLAG_PATH.unlink()
        else:  # OK
            for p in (PROP_HALT_FLAG_PATH, PROP_WATCH_FLAG_PATH):
                if p.exists():
                    p.unlink()
    except OSError as exc:
        print(f"WARN: flag file emit failed: {exc}", file=sys.stderr)


def _fire_alerts(receipt: GuardReceipt) -> None:
    """Append HALT/WATCH events to the shared alerts_log.

    Pattern matches diamond_falsification_watchdog's _fire_alerts_for_critical
    so the dashboard's daily-summary + alerts panel surface prop-guard
    events alongside the existing diamond alerts.

    Idempotent in the sense that a fresh HALT each cycle emits a fresh
    alert; the dashboard de-dupes by alert_id and timestamp window.
    Best-effort: any I/O error is logged and silently continues.
    """
    if receipt.signal == "OK":
        return  # don't spam the log with OK heartbeats
    try:
        ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        severity = "RED" if receipt.signal == "HALT" else "YELLOW"
        alert = {
            "timestamp_utc": receipt.ts,
            "ts": receipt.ts,
            "severity": severity,
            "source": "diamond_prop_drawdown_guard",
            "alert_id": f"prop_guard_{receipt.signal.lower()}",
            "headline": (
                f"PROP GUARD {receipt.signal}: "
                f"daily=${receipt.daily_pnl_usd:+.2f}  "
                f"total=${receipt.total_pnl_usd:+.2f}  "
                f"{receipt.rationale}"
            ),
            "details": {
                "signal": receipt.signal,
                "rationale": receipt.rationale,
                "daily_pnl_usd": receipt.daily_pnl_usd,
                "total_pnl_usd": receipt.total_pnl_usd,
                "consistency_ratio": receipt.consistency_ratio,
                "prop_ready_bots": receipt.prop_ready_bots,
            },
        }
        with ALERTS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(alert, default=str) + "\n")
    except OSError as exc:
        print(f"WARN: alerts log append failed: {exc}", file=sys.stderr)


def _print(summary: dict[str, Any]) -> None:
    print("=" * 100)
    print(
        f" DIAMOND PROP DRAWDOWN GUARD  ({summary['ts']})  "
        f"signal={summary['signal']}  account=${summary['account_size']:,.0f}",
    )
    print("=" * 100)
    print(f" {summary['rationale']}")
    print(f" PROP_READY: {summary.get('prop_ready_bots') or '(none)'}")
    print(
        f" daily_pnl_usd=${summary.get('daily_pnl_usd'):>+,.2f}  total_pnl_usd=${summary.get('total_pnl_usd'):>+,.2f}",
    )
    print()
    for name in ("daily_dd_check", "static_dd_check", "consistency_check"):
        c = summary.get(name)
        if c is None:
            continue
        print(
            f"  [{c['status']:5s}] {c['name']:18s}  "
            f"used=${c['used_usd']:>9,.2f}  "
            f"limit=${c['limit_usd']:>9,.2f}  "
            f"buf=${c['buffer_usd']:>9,.2f} ({c['buffer_pct_of_limit']:.1f}%)",
        )
        if c.get("rationale"):
            print(f"        {c['rationale']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--account-size",
        type=float,
        default=DEFAULT_ACCOUNT_SIZE,
        help="Total prop account size in USD (default 50000)",
    )
    ap.add_argument(
        "--daily-dd-pct",
        type=float,
        default=DEFAULT_DAILY_DD_PCT,
        help="Daily drawdown limit as fraction of account (default 0.03 = 3%%)",
    )
    ap.add_argument(
        "--static-dd-pct",
        type=float,
        default=DEFAULT_STATIC_DD_PCT,
        help="Static drawdown limit as fraction of account (default 0.05 = 5%%)",
    )
    ap.add_argument(
        "--consistency-pct",
        type=float,
        default=DEFAULT_CONSISTENCY_PCT,
        help="Max single-day profit as fraction of total (default 0.30 = 30%%)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run(
        account_size=args.account_size,
        daily_dd_pct=args.daily_dd_pct,
        static_dd_pct=args.static_dd_pct,
        consistency_pct=args.consistency_pct,
    )
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return {"OK": 0, "WATCH": 1, "HALT": 2}.get(summary["signal"], 0)


if __name__ == "__main__":
    raise SystemExit(main())
