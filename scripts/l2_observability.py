"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_observability
=======================================================
Shared signal-emit + fill-event writers used by all L2 strategies
and the live order router.

Why this exists
---------------
The l2_fill_audit script reads two JSONL files:
  - logs/eta_engine/l2_signal_log.jsonl  (every signal emitted)
  - logs/eta_engine/broker_fills.jsonl   (every fill received)

Until something writes these files, the audit is dead code.  This
module is the single place that knows the schema; strategies and
the order router import the helpers and emit consistently.

Schema
------
Signal record:
    {
      "ts": ISO8601 UTC,
      "signal_id": str (broker idempotency key),
      "strategy_id": str,
      "bot_id": str,
      "symbol": str,
      "side": "LONG" | "SHORT",
      "entry_price": float,
      "intended_stop_price": float,
      "intended_target_price": float,
      "confidence": float [0.0, 1.0],
      "qty_contracts": int,
      "rationale": str
    }

Fill record:
    {
      "ts": ISO8601 UTC,
      "signal_id": str (matches signal),
      "broker_exec_id": str,
      "exit_reason": "ENTRY" | "TARGET" | "STOP" | "TIMEOUT" | "CANCEL",
      "side": "LONG" | "SHORT" (of the original signal),
      "actual_fill_price": float,
      "qty_filled": int,
      "commission_usd": float,
      "slip_ticks_vs_intended": float | null
    }

Both writers append-only, use the same date rotation pattern as the
capture daemons.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"


def emit_signal(*, signal_id: str, strategy_id: str, bot_id: str,
                symbol: str, side: str,
                entry_price: float, intended_stop_price: float,
                intended_target_price: float,
                confidence: float, qty_contracts: int,
                rationale: str = "",
                ts: datetime | None = None,
                _path: Path | None = None) -> dict:
    """Append a signal-emission record to l2_signal_log.jsonl.

    Returns the record dict (useful for tests and chained logging).
    The ``_path`` argument is for tests; production code uses the
    module-level SIGNAL_LOG path.
    """
    record = {
        "ts": (ts or datetime.now(UTC)).isoformat(),
        "signal_id": signal_id,
        "strategy_id": strategy_id,
        "bot_id": bot_id,
        "symbol": symbol,
        "side": side.upper(),
        "entry_price": round(float(entry_price), 4),
        "intended_stop_price": round(float(intended_stop_price), 4),
        "intended_target_price": round(float(intended_target_price), 4),
        "confidence": round(float(confidence), 3),
        "qty_contracts": int(qty_contracts),
        "rationale": str(rationale)[:200],
    }
    path = _path if _path is not None else SIGNAL_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        # Match the D6 hygiene pattern: surface to stderr, never swallow
        print(f"l2_observability WARN: could not append signal to {path}: {e}",
              file=sys.stderr)
    return record


def emit_fill(*, signal_id: str, broker_exec_id: str,
              exit_reason: str, side: str,
              actual_fill_price: float, qty_filled: int,
              commission_usd: float = 0.0,
              slip_ticks_vs_intended: float | None = None,
              ts: datetime | None = None,
              _path: Path | None = None) -> dict:
    """Append a fill record.  Live order router calls this on every
    broker execution event.

    ``exit_reason`` is the reason the position closed (or ENTRY for
    the entry fill).  ``slip_ticks_vs_intended`` can be left None and
    computed retrospectively by l2_fill_audit.
    """
    record = {
        "ts": (ts or datetime.now(UTC)).isoformat(),
        "signal_id": signal_id,
        "broker_exec_id": broker_exec_id,
        "exit_reason": exit_reason.upper(),
        "side": side.upper(),
        "actual_fill_price": round(float(actual_fill_price), 4),
        "qty_filled": int(qty_filled),
        "commission_usd": round(float(commission_usd), 4),
        "slip_ticks_vs_intended": (round(float(slip_ticks_vs_intended), 2)
                                     if slip_ticks_vs_intended is not None else None),
    }
    path = _path if _path is not None else BROKER_FILL_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"l2_observability WARN: could not append fill to {path}: {e}",
              file=sys.stderr)
    return record


def emit_signal_from_imbalance(signal: object, *, bot_id: str,
                                _path: Path | None = None) -> dict:  # noqa: ANN001
    """Convenience adapter for ImbalanceSignal -> signal log record.
    book_imbalance / footprint / aggressor_flow / microprice all emit
    objects with the same minimal shape (signal_id, side, entry_price,
    stop, target, confidence, qty_contracts, symbol, rationale)."""
    return emit_signal(
        signal_id=getattr(signal, "signal_id", ""),
        strategy_id=getattr(signal, "strategy_id", "unknown"),
        bot_id=bot_id,
        symbol=getattr(signal, "symbol", "?"),
        side=getattr(signal, "side", "?"),
        entry_price=getattr(signal, "entry_price", 0.0),
        intended_stop_price=getattr(signal, "stop", 0.0),
        intended_target_price=getattr(signal, "target", 0.0),
        confidence=getattr(signal, "confidence", 0.0),
        qty_contracts=getattr(signal, "qty_contracts", 1),
        rationale=getattr(signal, "rationale", ""),
        _path=_path,
    )
