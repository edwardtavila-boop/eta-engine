"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_supervisor_hooks
==========================================================
Drop-in hooks for the live order supervisor to integrate the L2
supercharge observability + gating layer with minimal code surface.

Why this exists
---------------
jarvis_strategy_supervisor.py is 4600+ LOC and stable.  Inline-
modifying it to call trading_gate + emit_signal + emit_fill would
mean editing a working production module.  Instead, this module
exposes three small functions the supervisor imports once and
calls at three explicit points:

    pre_trade_check(...)  → call BEFORE place_order; returns False to BLOCK
    record_signal(...)    → call AFTER place_order returns OPEN/PARTIAL/FILLED
    record_fill(...)      → call when broker reports a terminal fill event

Each function is fail-safe: if it raises, the caller catches and
continues.  Never blocks live trading on observability failure.

Wiring example (3 lines in supervisor):
---------------------------------------
::

    # at top of supervisor module
    from eta_engine.scripts import l2_supervisor_hooks as l2hooks

    # before _venue.place_order(...)
    if not l2hooks.pre_trade_check(bot, rec):
        _rollback_recorded_entry("blocked_by_trading_gate")
        return None

    # after place_order returns success (OPEN/PARTIAL/FILLED)
    l2hooks.record_signal(bot, rec, _result)

    # in the broker's executionEvent handler (whenever a fill arrives)
    l2hooks.record_fill(signal_id, broker_exec_id, exit_reason,
                         side, actual_fill_price, qty_filled,
                         commission_usd)

Why these names exactly
-----------------------
- ``pre_trade_check``: positive form — returns True to ALLOW.  When
  False, supervisor MUST roll back the simulated entry.
- ``record_signal``: present-tense imperative — emits to
  l2_signal_log.jsonl.  Triggered exactly once per accepted entry.
- ``record_fill``: same pattern for fills.  Triggered exactly once
  per terminal broker event (TARGET / STOP / TIMEOUT / CANCEL).

Operator can wire these in any order — they don't depend on each
other, and each is independently testable.
"""
from __future__ import annotations

# ruff: noqa: ANN001, ANN401, BLE001
# Args are typed as Any because the supervisor passes its own bot
# and rec dataclasses we don't import here (would create a circular
# dep with the supervisor module).  BLE001: defensive try/except
# wraps every call so observability failures never block trading.
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def pre_trade_check(bot: Any, rec: Any) -> bool:
    """Consult trading_gate.check_pre_trade_gate before order placement.

    Returns:
        True  → ALLOW the order to proceed
        False → BLOCK (disk RED/CRITICAL or capture RED or stale digests)

    The supervisor MUST roll back its simulated entry on False return.

    Defensive: any exception → returns True (don't block trading on
    observability bug).  Logs the exception so we can fix it.
    """
    try:
        from eta_engine.strategies.trading_gate import check_pre_trade_gate
        symbol = getattr(rec, "symbol", None) or getattr(bot, "symbol", None)
        decision = check_pre_trade_gate(symbol)
        if decision.blocked:
            logger.warning(
                "l2_supervisor_hooks BLOCK %s: %s "
                "(disk=%s, capture=%s, disk_age=%s, capture_age=%s)",
                getattr(bot, "bot_id", "?"), decision.reason,
                decision.disk_verdict, decision.capture_verdict,
                decision.disk_age_seconds, decision.capture_age_seconds,
            )
            return False
        return True
    except Exception as e:
        print(f"l2_supervisor_hooks WARN pre_trade_check exception: {e}",
              file=sys.stderr)
        return True  # fail-OPEN on hook failure (don't break trading)


def record_signal(bot: Any, rec: Any, place_order_result: Any | None = None) -> None:
    """Append a signal record to l2_signal_log.jsonl.

    Called after place_order returns a non-rejection status.  The
    place_order_result is optional but recommended — if present, we
    record the broker's ack timestamp instead of "now."

    Defensive: never raises.  Logs and continues on any error.
    """
    try:
        from eta_engine.scripts.l2_observability import emit_signal
        # Pull bracket prices.  The supervisor stores them on `rec` as
        # part of the OrderRequest construction.  Fall back to 0 if
        # missing rather than refuse to log.
        intended_stop = float(getattr(rec, "stop_price", 0)
                                or getattr(rec, "stop", 0) or 0)
        intended_target = float(getattr(rec, "target_price", 0)
                                  or getattr(rec, "target", 0) or 0)
        entry_price = float(getattr(rec, "entry_price", 0)
                              or getattr(rec, "price", 0) or 0)
        signal_id = getattr(rec, "signal_id", "") or getattr(rec, "client_order_id", "")
        if not signal_id:
            logger.debug("record_signal skipped: no signal_id on rec")
            return
        emit_signal(
            signal_id=signal_id,
            strategy_id=getattr(bot, "strategy_id", "unknown"),
            bot_id=getattr(bot, "bot_id", "unknown"),
            symbol=getattr(rec, "symbol", "?"),
            side=str(getattr(rec, "side", "?")),
            entry_price=entry_price,
            intended_stop_price=intended_stop,
            intended_target_price=intended_target,
            confidence=float(getattr(rec, "confidence", 0.0)),
            qty_contracts=int(abs(float(getattr(rec, "qty", 1) or 1))),
            rationale=str(getattr(rec, "rationale", ""))[:200],
        )
    except Exception as e:
        print(f"l2_supervisor_hooks WARN record_signal exception: {e}",
              file=sys.stderr)


def record_fill(signal_id: str, *,
                broker_exec_id: str,
                exit_reason: str,
                side: str,
                actual_fill_price: float,
                qty_filled: int,
                commission_usd: float = 0.0,
                intended_price: float | None = None,
                tick_size: float = 0.25) -> None:
    """Append a fill record to broker_fills.jsonl.

    Called from the broker's executionEvent handler (or equivalent
    callback) whenever a fill arrives.  If ``intended_price`` is
    passed, we compute slip_ticks_vs_intended inline; otherwise
    l2_fill_audit will retro-compute it from the signal log.

    Defensive: never raises.
    """
    try:
        from eta_engine.scripts.l2_observability import emit_fill
        slip_ticks: float | None = None
        if intended_price is not None:
            raw = float(actual_fill_price) - float(intended_price)
            slip_price = -raw if side.upper() in ("LONG", "BUY") else raw
            slip_ticks = round(slip_price / max(tick_size, 1e-9), 2)
        emit_fill(
            signal_id=signal_id,
            broker_exec_id=broker_exec_id,
            exit_reason=exit_reason,
            side=side,
            actual_fill_price=actual_fill_price,
            qty_filled=qty_filled,
            commission_usd=commission_usd,
            slip_ticks_vs_intended=slip_ticks,
        )
    except Exception as e:
        print(f"l2_supervisor_hooks WARN record_fill exception: {e}",
              file=sys.stderr)


# ── Bulk-write convenience for testing or back-fill ───────────────


def record_bulk_fills(fills: list[dict]) -> int:
    """Bulk-write fills to the log.  Useful for back-filling from a
    broker export.  Each dict must have signal_id, exit_reason, side,
    actual_fill_price, qty_filled, broker_exec_id.

    Returns count of records written.
    """
    n = 0
    for f in fills:
        try:
            record_fill(
                signal_id=f["signal_id"],
                broker_exec_id=f.get("broker_exec_id", ""),
                exit_reason=f["exit_reason"],
                side=f["side"],
                actual_fill_price=f["actual_fill_price"],
                qty_filled=f.get("qty_filled", 1),
                commission_usd=f.get("commission_usd", 0.0),
                intended_price=f.get("intended_price"),
                tick_size=f.get("tick_size", 0.25),
            )
            n += 1
        except (KeyError, TypeError) as e:
            print(f"l2_supervisor_hooks WARN bad fill record: {e}",
                  file=sys.stderr)
    return n
