"""MNQ session latency scorecard backed by canonical runtime telemetry.

This is the current replacement for the historical ``session_scorecard_mnq.py``
surface. The old scorecard depended on bespoke session-latency artifacts that
no longer ship. The replacement uses the two canonical latency signals we do
have:

* live MNQ trade closes from the closed-trade ledger
* confirmed ``FILL_AGE_EXCEEDED`` alerts from ``alerts_log.jsonl``

Classification is intentionally conservative:

* GREEN  -> no structured >1-bar latency and no confirmed fill-age alerts
* YELLOW -> any structured >1-bar latency OR 1 confirmed alert
* RED    -> any structured >2-bar latency OR 2+ confirmed alerts

The scorecard summarizes realized PnL, win rate, and average R for the same
window so operators can review fill-age stress and live performance together.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eta_engine.scripts import closed_trade_ledger, workspace_roots

FILL_AGE_EVENT = "FILL_AGE_EXCEEDED"
DEFAULT_HOURS = 24
RED_ALERT_THRESHOLD = 2
YELLOW_ALERT_THRESHOLD = 1
MNQ_BAR_SECONDS = 300.0
YELLOW_LATENCY_BARS = 1.0
RED_LATENCY_BARS = 2.0

MODE_TO_DATA_SOURCES: dict[str, frozenset[str]] = {
    "live": frozenset(
        {
            closed_trade_ledger.DATA_SOURCE_LIVE,
            closed_trade_ledger.DATA_SOURCE_LIVE_UNVERIFIED,
        }
    ),
    "paper": frozenset({closed_trade_ledger.DATA_SOURCE_PAPER}),
    "all": closed_trade_ledger.DEFAULT_OPERATOR_DATA_SOURCES,
}


def _parse_ts(value: Any) -> datetime | None:  # noqa: ANN401
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _as_float(value: Any, default: float = 0.0) -> float:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbol_root(symbol: str) -> str:
    s = symbol.upper().lstrip("/").rstrip("0123456789")
    for suffix in ("USDT", "USD"):
        if s.endswith(suffix):
            return s[: -len(suffix)] or s
    return s


def _record_matches_mnq(row: dict[str, Any]) -> bool:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    symbol = str(row.get("symbol") or extra.get("symbol") or "")
    return _symbol_root(symbol) == "MNQ"


def _record_realized_pnl(row: dict[str, Any]) -> float:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return _as_float(row.get("realized_pnl", extra.get("realized_pnl")))


def _record_realized_r(row: dict[str, Any]) -> float:
    return _as_float(row.get("realized_r"))


def _record_extra(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("extra")
    return value if isinstance(value, dict) else {}


def _record_entry_fill_age_s(row: dict[str, Any]) -> float | None:
    extra = _record_extra(row)
    raw = extra.get("entry_fill_age_s", row.get("entry_fill_age_s"))
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _record_entry_fill_latency_source(row: dict[str, Any]) -> str:
    extra = _record_extra(row)
    return str(extra.get("entry_fill_latency_source", row.get("entry_fill_latency_source")) or "").strip()


def _record_entry_fill_age_precision(row: dict[str, Any]) -> str:
    extra = _record_extra(row)
    return str(extra.get("entry_fill_age_precision", row.get("entry_fill_age_precision")) or "").strip()


def _record_fill_to_adopt_delay_s(row: dict[str, Any]) -> float | None:
    extra = _record_extra(row)
    raw = extra.get("fill_to_adopt_delay_s", row.get("fill_to_adopt_delay_s"))
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _load_alert_rows(path: Path, *, cutoff: datetime) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    malformed = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        ts = _parse_ts(payload.get("ts"))
        if ts is None or ts < cutoff:
            continue
        rows.append(payload)
    return rows, malformed


def _is_fill_age_exceeded(record: dict[str, Any]) -> bool:
    try:
        text = json.dumps(record, sort_keys=True, default=str).upper()
    except (TypeError, ValueError):
        text = str(record).upper()
    return FILL_AGE_EVENT in text


def _classify(
    fill_age_exceeded_count: int,
    *,
    over_1_bar_count: int,
    over_2_bar_count: int,
) -> tuple[str, int]:
    alert_exit_code = 0
    if fill_age_exceeded_count >= RED_ALERT_THRESHOLD:
        alert_exit_code = 2
    elif fill_age_exceeded_count >= YELLOW_ALERT_THRESHOLD:
        alert_exit_code = 1

    latency_exit_code = 0
    if over_2_bar_count > 0:
        latency_exit_code = 2
    elif over_1_bar_count > 0:
        latency_exit_code = 1

    exit_code = max(alert_exit_code, latency_exit_code)
    return ("GREEN", "YELLOW", "RED")[exit_code], exit_code


def build_scorecard(
    *,
    hours: int = DEFAULT_HOURS,
    mode: str = "live",
    alerts_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], int]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = now_utc - timedelta(hours=hours)
    data_sources = MODE_TO_DATA_SOURCES[mode]
    alert_path = alerts_path or workspace_roots.default_alerts_log_path()

    close_rows = closed_trade_ledger.load_close_records(
        since_days=max(1, math.ceil(hours / 24)),
        data_sources=data_sources,
    )
    recent_mnq = [
        row
        for row in close_rows
        if _record_matches_mnq(row) and ((_parse_ts(row.get("ts")) or cutoff) >= cutoff)
    ]
    realized_rs = [_record_realized_r(row) for row in recent_mnq]
    realized_pnls = [_record_realized_pnl(row) for row in recent_mnq]
    close_count = len(recent_mnq)
    wins = sum(1 for value in realized_rs if value > 0)
    avg_r = round(sum(realized_rs) / close_count, 4) if close_count else 0.0
    win_rate = round(wins / close_count, 4) if close_count else 0.0
    realized_pnl = round(sum(realized_pnls), 2)
    bot_ids = sorted({str(row.get("bot_id") or "").strip() for row in recent_mnq if str(row.get("bot_id") or "").strip()})
    telemetry_rows: list[dict[str, Any]] = []
    over_1_bar_count = 0
    over_2_bar_count = 0
    max_entry_fill_age_s = 0.0
    for row in recent_mnq:
        entry_fill_age_s = _record_entry_fill_age_s(row)
        if entry_fill_age_s is None:
            continue
        entry_fill_age_bars = entry_fill_age_s / MNQ_BAR_SECONDS
        max_entry_fill_age_s = max(max_entry_fill_age_s, entry_fill_age_s)
        telemetry_rows.append(
            {
                "ts": row.get("ts") or "",
                "bot_id": str(row.get("bot_id") or ""),
                "signal_id": str(row.get("signal_id") or ""),
                "entry_fill_age_s": round(entry_fill_age_s, 1),
                "entry_fill_age_bars": round(entry_fill_age_bars, 3),
                "entry_fill_latency_source": _record_entry_fill_latency_source(row),
                "entry_fill_age_precision": _record_entry_fill_age_precision(row),
                "fill_to_adopt_delay_s": _record_fill_to_adopt_delay_s(row),
            }
        )
        if entry_fill_age_bars > YELLOW_LATENCY_BARS:
            over_1_bar_count += 1
        if entry_fill_age_bars > RED_LATENCY_BARS:
            over_2_bar_count += 1

    alert_rows, malformed_alert_lines = _load_alert_rows(alert_path, cutoff=cutoff)
    fill_age_alerts = [row for row in alert_rows if _is_fill_age_exceeded(row)]
    status, exit_code = _classify(
        len(fill_age_alerts),
        over_1_bar_count=over_1_bar_count,
        over_2_bar_count=over_2_bar_count,
    )

    summary = {
        "status": status,
        "window_hours": hours,
        "mode": mode,
        "window_start": cutoff.isoformat(),
        "window_end": now_utc.isoformat(),
        "alerts_path": str(alert_path),
        "malformed_alert_lines": malformed_alert_lines,
        "fill_age_exceeded_count": len(fill_age_alerts),
        "recent_fill_age_alerts": fill_age_alerts[-5:],
        "latency_telemetry_close_count": len(telemetry_rows),
        "over_1_bar_count": over_1_bar_count,
        "over_2_bar_count": over_2_bar_count,
        "max_entry_fill_age_s": round(max_entry_fill_age_s, 1),
        "max_entry_fill_age_bars": round(max_entry_fill_age_s / MNQ_BAR_SECONDS, 3) if max_entry_fill_age_s else 0.0,
        "recent_slow_fills": telemetry_rows[-5:],
        "close_count": close_count,
        "bot_ids": bot_ids,
        "realized_pnl": realized_pnl,
        "avg_r": avg_r,
        "win_rate": win_rate,
        "note": (
            "Uses structured entry_fill_age_s telemetry from canonical MNQ closes plus "
            "confirmed FILL_AGE_EXCEEDED alerts as a hard corroborating backstop."
        ),
    }
    return summary, exit_code


def _print_human(summary: dict[str, Any]) -> None:
    print(
        "mnq-latency-scorecard: "
        f"{summary['status']} -- {summary['fill_age_exceeded_count']} confirmed "
        f"{FILL_AGE_EVENT} alert(s) in {summary['window_hours']}h; "
        f">1bar={summary['over_1_bar_count']} >2bar={summary['over_2_bar_count']} "
        f"closes={summary['close_count']} pnl=${summary['realized_pnl']:+.2f} "
        f"avgR={summary['avg_r']:+.3f} win_rate={summary['win_rate']:.1%}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS, help="window size in hours")
    parser.add_argument("--mode", choices=sorted(MODE_TO_DATA_SOURCES), default="live")
    parser.add_argument(
        "--alerts",
        type=Path,
        default=None,
        help="alerts JSONL path (default: canonical runtime alerts log with legacy fallback)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON summary")
    parser.add_argument(
        "--journal",
        default=None,
        help="deprecated compatibility flag from session_scorecard_mnq.py; ignored",
    )
    parser.add_argument(
        "--paper-baseline",
        default=None,
        help="deprecated compatibility flag from session_scorecard_mnq.py; ignored",
    )
    args = parser.parse_args(argv)

    summary, exit_code = build_scorecard(hours=args.hours, mode=args.mode, alerts_path=args.alerts)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_human(summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
