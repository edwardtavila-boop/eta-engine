"""Replay shadow signals into counterfactual paper outcomes.

This script intentionally does not touch broker routing. It answers one narrow
question for the operator: when a strategy fired in shadow/paper-eval mode,
what would a simple post-signal 1R stop/target have done on local bars?

The resulting artifact is useful for retune triage, but it is not promotion
proof. Promotion still requires broker-backed closed trades.
"""
# ruff: noqa: ANN401  -- shadow-signal JSON and BarData-like rows are intentionally dynamic.

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

DEFAULT_LOOKAHEAD_BARS = 12
DEFAULT_TARGET_R = 1.0
DEFAULT_STOP_R = 1.0
DEFAULT_TIMEFRAME = "5m"
DEFAULT_MAX_SIGNALS = 1000
DEFAULT_SINCE_DAYS = 14
DEFAULT_OUT = workspace_roots.ETA_JARVIS_SHADOW_SIGNAL_OUTCOMES_PATH
DEFAULT_FILTERED_OUT = DEFAULT_OUT.with_name("shadow_signal_outcomes_filtered_latest.json")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _bar_ts(bar: Any) -> datetime | None:
    if isinstance(bar, dict):
        return _parse_dt(bar.get("timestamp") or bar.get("ts") or bar.get("time"))
    return _parse_dt(getattr(bar, "timestamp", None) or getattr(bar, "ts", None))


def _bar_value(bar: Any, name: str, default: float = 0.0) -> float:
    if isinstance(bar, dict):
        return _as_float(bar.get(name), default)
    return _as_float(getattr(bar, name, default), default)


def _signal_ts(signal: dict[str, Any]) -> datetime | None:
    extra = _as_dict(signal.get("extra"))
    return _parse_dt(extra.get("bar_ts") or signal.get("bar_ts") or signal.get("ts"))


def _normalise_side(value: Any) -> str:
    side = str(value or "").strip().upper()
    if side in {"BUY", "LONG"}:
        return "BUY"
    if side in {"SELL", "SHORT"}:
        return "SELL"
    return ""


def _normalise_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _sorted_bars(bars: Sequence[Any]) -> list[Any]:
    return sorted(
        (bar for bar in bars if _bar_ts(bar) is not None),
        key=lambda bar: _bar_ts(bar) or datetime.min.replace(tzinfo=UTC),
    )


def _entry_index(bars: Sequence[Any], signal_time: datetime) -> int | None:
    for idx, bar in enumerate(bars):
        ts = _bar_ts(bar)
        if ts is not None and ts >= signal_time:
            return idx
    return None


def _explicit_entry_price(signal: dict[str, Any]) -> float:
    extra = _as_dict(signal.get("extra"))
    return _as_float(extra.get("entry_price") or extra.get("price") or signal.get("entry_price"))


def _entry_price(signal: dict[str, Any], bars: Sequence[Any], entry_idx: int) -> float:
    explicit = _explicit_entry_price(signal)
    if explicit > 0:
        return explicit
    return _bar_value(bars[entry_idx], "close")


def _estimated_risk_price(signal: dict[str, Any], bars: Sequence[Any], entry_idx: int, entry_price: float) -> float:
    extra = _as_dict(signal.get("extra"))
    explicit = _as_float(extra.get("risk_price") or extra.get("stop_distance") or signal.get("risk_price"))
    if explicit > 0:
        return explicit

    start = max(0, entry_idx - 13)
    ranges = [
        max(0.0, _bar_value(bar, "high") - _bar_value(bar, "low"))
        for bar in bars[start : entry_idx + 1]
    ]
    atr = sum(ranges) / len(ranges) if ranges else 0.0
    fallback = abs(entry_price) * 0.001
    return max(atr, fallback, 0.01)


def _explicit_planned_prices(
    signal: dict[str, Any],
    *,
    side: str,
    entry_price: float,
    target_r: float,
    stop_r: float,
) -> tuple[float, float, float] | None:
    extra = _as_dict(signal.get("extra"))
    stop = _as_float(extra.get("stop_price") or signal.get("stop_price"))
    target = _as_float(extra.get("target_price") or signal.get("target_price"))
    risk = _as_float(extra.get("risk_price") or extra.get("stop_distance") or signal.get("risk_price"))
    if risk <= 0 and stop > 0:
        risk = abs(stop - entry_price)
    if risk <= 0:
        return None

    if stop <= 0:
        stop_delta = risk * max(0.01, float(stop_r))
        stop = entry_price - stop_delta if side == "BUY" else entry_price + stop_delta
    if target <= 0:
        target_delta = risk * max(0.01, float(target_r))
        target = entry_price + target_delta if side == "BUY" else entry_price - target_delta

    if side == "BUY":
        if not (stop < entry_price < target):
            return None
    elif not (target < entry_price < stop):
        return None
    return stop, target, risk


def replay_signal(
    signal: dict[str, Any],
    bars: Sequence[Any],
    *,
    lookahead_bars: int = DEFAULT_LOOKAHEAD_BARS,
    target_r: float = DEFAULT_TARGET_R,
    stop_r: float = DEFAULT_STOP_R,
) -> dict[str, Any]:
    """Replay one signal against future bars using a conservative 1R model."""
    bot_id = str(signal.get("bot_id") or "unknown")
    symbol = _normalise_symbol(signal.get("symbol"))
    side = _normalise_side(signal.get("side"))
    signal_time = _signal_ts(signal)
    if not symbol or not side or signal_time is None:
        return {
            "bot_id": bot_id,
            "symbol": symbol,
            "signal_id": signal.get("signal_id") or "",
            "status": "SKIPPED_BAD_SIGNAL",
            "reason": "missing symbol, side, or timestamp",
            "broker_backed": False,
            "promotion_proof": False,
        }

    ordered = _sorted_bars(bars)
    if not ordered:
        return {
            "bot_id": bot_id,
            "symbol": symbol,
            "signal_id": signal.get("signal_id") or "",
            "status": "MISSING_BARS",
            "broker_backed": False,
            "promotion_proof": False,
        }
    entry_idx = _entry_index(ordered, signal_time)
    if entry_idx is None:
        return {
            "bot_id": bot_id,
            "symbol": symbol,
            "signal_id": signal.get("signal_id") or "",
            "signal_ts": signal_time.isoformat(),
            "status": "NO_BAR_AFTER_SIGNAL",
            "broker_backed": False,
            "promotion_proof": False,
        }

    entry = _entry_price(signal, ordered, entry_idx)
    if entry <= 0:
        return {
            "bot_id": bot_id,
            "symbol": symbol,
            "side": side,
            "signal_id": signal.get("signal_id") or "",
            "signal_ts": signal_time.isoformat(),
            "status": "MISSING_SIGNAL_CONTEXT",
            "reason": "missing planned entry context and bar close",
            "broker_backed": False,
            "promotion_proof": False,
        }
    planned = _explicit_planned_prices(
        signal,
        side=side,
        entry_price=entry,
        target_r=target_r,
        stop_r=stop_r,
    )
    if planned is None:
        risk = _estimated_risk_price(signal, ordered, entry_idx, entry)
        target_delta = risk * max(0.01, float(target_r))
        stop_delta = risk * max(0.01, float(stop_r))
        if side == "BUY":
            stop_price = entry - stop_delta
            target_price = entry + target_delta
        else:
            stop_price = entry + stop_delta
            target_price = entry - target_delta
    else:
        stop_price, target_price, risk = planned

    future = ordered[entry_idx + 1 : entry_idx + 1 + max(1, int(lookahead_bars))]
    if not future:
        return {
            "bot_id": bot_id,
            "symbol": symbol,
            "signal_id": signal.get("signal_id") or "",
            "signal_ts": signal_time.isoformat(),
            "status": "INSUFFICIENT_FUTURE_BARS",
            "broker_backed": False,
            "promotion_proof": False,
        }

    exit_reason = "TIMEOUT"
    exit_price = _bar_value(future[-1], "close")
    exit_ts = _bar_ts(future[-1])

    if side == "BUY":
        for bar in future:
            high = _bar_value(bar, "high")
            low = _bar_value(bar, "low")
            # Conservative intrabar ordering: if both touch, count the stop.
            if low <= stop_price:
                exit_reason = "STOP"
                exit_price = stop_price
                exit_ts = _bar_ts(bar)
                break
            if high >= target_price:
                exit_reason = "TARGET"
                exit_price = target_price
                exit_ts = _bar_ts(bar)
                break
        realized_r = (exit_price - entry) / risk
        mfe_r = (max(_bar_value(bar, "high") for bar in future) - entry) / risk
        mae_r = (min(_bar_value(bar, "low") for bar in future) - entry) / risk
    else:
        for bar in future:
            high = _bar_value(bar, "high")
            low = _bar_value(bar, "low")
            if high >= stop_price:
                exit_reason = "STOP"
                exit_price = stop_price
                exit_ts = _bar_ts(bar)
                break
            if low <= target_price:
                exit_reason = "TARGET"
                exit_price = target_price
                exit_ts = _bar_ts(bar)
                break
        realized_r = (entry - exit_price) / risk
        mfe_r = (entry - min(_bar_value(bar, "low") for bar in future)) / risk
        mae_r = (entry - max(_bar_value(bar, "high") for bar in future)) / risk

    return {
        "bot_id": bot_id,
        "symbol": symbol,
        "side": side,
        "signal_id": signal.get("signal_id") or "",
        "signal_ts": signal_time.isoformat(),
        "entry_ts": (_bar_ts(ordered[entry_idx]) or signal_time).isoformat(),
        "exit_ts": exit_ts.isoformat() if exit_ts else "",
        "entry_price": round(entry, 6),
        "exit_price": round(exit_price, 6),
        "risk_price": round(risk, 6),
        "stop_price": round(stop_price, 6),
        "target_price": round(target_price, 6),
        "exit_reason": exit_reason,
        "realized_r": round(realized_r, 4),
        "mfe_r": round(mfe_r, 4),
        "mae_r": round(mae_r, 4),
        "status": "EVALUATED",
        "broker_backed": False,
        "promotion_proof": False,
    }


def _empty_bot_stats(bot_id: str) -> dict[str, Any]:
    return {
        "bot_id": bot_id,
        "shadow_signal_count": 0,
        "evaluated_count": 0,
        "wins": 0,
        "losses": 0,
        "flats": 0,
        "missing_bars": 0,
        "missing_context": 0,
        "insufficient_future_bars": 0,
        "skipped_bad_signals": 0,
        "total_r": 0.0,
        "avg_r": 0.0,
        "win_rate_pct": 0.0,
        "profit_factor": 0.0,
        "latest_signal_ts": "",
        "latest_evaluated_ts": "",
        "verdict": "NO_EVALUATED_SIGNALS",
        "broker_backed": False,
        "promotion_proof": False,
    }


def _finalise_stats(stats: dict[str, Any], positive_r: float, negative_r: float) -> None:
    evaluated = int(stats["evaluated_count"])
    wins = int(stats["wins"])
    stats["total_r"] = round(float(stats["total_r"]), 4)
    stats["avg_r"] = round(float(stats["total_r"]) / evaluated, 4) if evaluated else 0.0
    stats["win_rate_pct"] = round((wins / evaluated) * 100.0, 2) if evaluated else 0.0
    if negative_r < 0:
        stats["profit_factor"] = round(positive_r / abs(negative_r), 4)
    else:
        stats["profit_factor"] = round(positive_r, 4) if positive_r > 0 else 0.0

    if evaluated <= 0:
        stats["verdict"] = "NO_EVALUATED_SIGNALS"
    elif evaluated < 30:
        stats["verdict"] = "SMALL_SAMPLE_COUNTERFACTUAL"
    elif stats["avg_r"] > 0 and stats["profit_factor"] >= 1.1:
        stats["verdict"] = "POSITIVE_COUNTERFACTUAL_EDGE"
    elif stats["avg_r"] <= 0 or stats["profit_factor"] < 1.0:
        stats["verdict"] = "WEAK_OR_NEGATIVE_COUNTERFACTUAL"
    else:
        stats["verdict"] = "MIXED_COUNTERFACTUAL"


def build_report(
    *,
    shadow_signals: Iterable[dict[str, Any]],
    bars_by_symbol: dict[str, Sequence[Any]],
    generated_at: datetime | None = None,
    lookahead_bars: int = DEFAULT_LOOKAHEAD_BARS,
    target_r: float = DEFAULT_TARGET_R,
    stop_r: float = DEFAULT_STOP_R,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(UTC)
    generated = generated.astimezone(UTC) if generated.tzinfo else generated.replace(tzinfo=UTC)
    signals = [_as_dict(signal) for signal in shadow_signals]
    per_bot: dict[str, dict[str, Any]] = {}
    risk_totals: dict[str, dict[str, float]] = {}
    outcomes: list[dict[str, Any]] = []

    for signal in signals:
        bot_id = str(signal.get("bot_id") or "unknown")
        symbol = _normalise_symbol(signal.get("symbol"))
        stats = per_bot.setdefault(bot_id, _empty_bot_stats(bot_id))
        totals = risk_totals.setdefault(bot_id, {"positive_r": 0.0, "negative_r": 0.0})
        stats["shadow_signal_count"] += 1
        signal_time = _signal_ts(signal)
        if signal_time is not None:
            stats["latest_signal_ts"] = signal_time.isoformat()

        outcome = replay_signal(
            signal,
            bars_by_symbol.get(symbol, []),
            lookahead_bars=lookahead_bars,
            target_r=target_r,
            stop_r=stop_r,
        )
        status = str(outcome.get("status") or "")
        if status == "EVALUATED":
            realized_r = _as_float(outcome.get("realized_r"))
            stats["evaluated_count"] += 1
            stats["total_r"] += realized_r
            stats["latest_evaluated_ts"] = outcome.get("exit_ts") or ""
            if realized_r > 0:
                stats["wins"] += 1
                totals["positive_r"] += realized_r
            elif realized_r < 0:
                stats["losses"] += 1
                totals["negative_r"] += realized_r
            else:
                stats["flats"] += 1
        elif status in {"MISSING_BARS", "NO_BAR_AFTER_SIGNAL"}:
            stats["missing_bars"] += 1
        elif status == "MISSING_SIGNAL_CONTEXT":
            stats["missing_context"] += 1
        elif status == "INSUFFICIENT_FUTURE_BARS":
            stats["insufficient_future_bars"] += 1
        else:
            stats["skipped_bad_signals"] += 1
        outcomes.append(outcome)

    for bot_id, stats in per_bot.items():
        totals = risk_totals.get(bot_id, {"positive_r": 0.0, "negative_r": 0.0})
        _finalise_stats(stats, totals["positive_r"], totals["negative_r"])

    evaluated_count = sum(int(stats["evaluated_count"]) for stats in per_bot.values())
    if not signals:
        status = "NO_SHADOW_SIGNALS"
    elif evaluated_count <= 0:
        status = "NO_EVALUATED_OUTCOMES"
    elif any(stats.get("verdict") == "POSITIVE_COUNTERFACTUAL_EDGE" for stats in per_bot.values()):
        status = "COUNTERFACTUAL_EDGE_SEEN"
    else:
        status = "COUNTERFACTUAL_REPLAY_COMPLETE"

    return {
        "schema_version": 1,
        "generated_at_utc": generated.isoformat(),
        "kind": "eta_shadow_signal_outcome_audit",
        "summary": {
            "status": status,
            "shadow_signal_count": len(signals),
            "evaluated_count": evaluated_count,
            "bot_count": len(per_bot),
            "lookahead_bars": int(lookahead_bars),
            "target_r": float(target_r),
            "stop_r": float(stop_r),
            "broker_backed": False,
            "promotion_proof": False,
            "truth_note": "Counterfactual shadow replay only; not broker-backed closed-trade PnL.",
        },
        "per_bot": dict(sorted(per_bot.items())),
        "outcomes": outcomes,
    }


def _load_bars_by_symbol(signals: Sequence[dict[str, Any]], *, timeframe: str) -> dict[str, Sequence[Any]]:
    from eta_engine.data.library import default_library  # noqa: PLC0415

    lib = default_library()
    bars_by_symbol: dict[str, Sequence[Any]] = {}
    for symbol in sorted({_normalise_symbol(signal.get("symbol")) for signal in signals if signal.get("symbol")}):
        dataset = lib.get(symbol=symbol, timeframe=timeframe)
        if dataset is None:
            bars_by_symbol[symbol] = []
            continue
        bars_by_symbol[symbol] = lib.load_bars(dataset, require_positive_prices=True)
    return bars_by_symbol


def _current_shadow_signals(
    *,
    bot: str | None,
    symbol: str | None,
    since_days: int | None,
    max_signals: int,
) -> list[dict[str, Any]]:
    from eta_engine.scripts.shadow_signal_logger import read_shadow_signals  # noqa: PLC0415

    since = None
    if since_days is not None and since_days > 0:
        since = datetime.now(UTC) - timedelta(days=since_days)
    rows = read_shadow_signals(bot_filter=bot, since=since, path=workspace_roots.ETA_JARVIS_SHADOW_SIGNALS_PATH)
    if symbol:
        wanted = _normalise_symbol(symbol)
        rows = [row for row in rows if _normalise_symbol(row.get("symbol")) == wanted]
    if max_signals > 0:
        rows = rows[-max_signals:]
    return rows


def write_report(report: dict[str, Any], out_path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(out_path)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return out_path


def _output_path_for_filters(out_path: Path, *, bot: str | None, symbol: str | None) -> Path:
    if Path(out_path) == DEFAULT_OUT and (bot or symbol):
        return DEFAULT_FILTERED_OUT
    return out_path


def _print_human(report: dict[str, Any], out_path: Path | None = None) -> None:
    summary = _as_dict(report.get("summary"))
    print()
    print("EVOLUTIONARY TRADING ALGO -- Shadow Signal Outcome Audit")
    print("=" * 72)
    print(f"status     : {summary.get('status')}")
    print(f"signals    : {summary.get('shadow_signal_count')}")
    print(f"evaluated  : {summary.get('evaluated_count')}")
    print(f"truth      : {summary.get('truth_note')}")
    if out_path is not None:
        print(f"artifact   : {out_path}")
    print("-" * 72)
    for bot_id, stats in _as_dict(report.get("per_bot")).items():
        print(
            f"{bot_id}: {stats.get('verdict')} | n={stats.get('evaluated_count')} | "
            f"avgR={stats.get('avg_r')} | PF={stats.get('profit_factor')}"
        )
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay shadow signals into counterfactual outcomes")
    parser.add_argument("--bot", default=None, help="Optional bot_id filter")
    parser.add_argument("--symbol", default=None, help="Optional symbol filter")
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--lookahead-bars", type=int, default=DEFAULT_LOOKAHEAD_BARS)
    parser.add_argument("--target-r", type=float, default=DEFAULT_TARGET_R)
    parser.add_argument("--stop-r", type=float, default=DEFAULT_STOP_R)
    parser.add_argument("--max-signals", type=int, default=DEFAULT_MAX_SIGNALS)
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    signals = _current_shadow_signals(
        bot=args.bot,
        symbol=args.symbol,
        since_days=args.since_days,
        max_signals=args.max_signals,
    )
    report = build_report(
        shadow_signals=signals,
        bars_by_symbol=_load_bars_by_symbol(signals, timeframe=args.timeframe),
        lookahead_bars=args.lookahead_bars,
        target_r=args.target_r,
        stop_r=args.stop_r,
    )
    selected_out = _output_path_for_filters(args.out, bot=args.bot, symbol=args.symbol)
    out_path = None if args.no_write else write_report(report, selected_out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_human(report, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
