"""EVOLUTIONARY TRADING ALGO // safety.live_gate.

Operator-controlled live-trading gate. Permissive by default so paper
/ test paths route freely; raises :class:`LiveTradingDisabled` the
moment a firm-halt or explicit-disable signal is set.

The gate reads three environment signals:

* ``FIRM_HALTED`` -- master halt. When ``true`` (or ``1``), every
  call raises. Set this from the kill-switch latch / firm-gate
  daemon.
* ``APEX_LIVE_TRADING_DISABLED`` -- operator opt-out. Same semantics
  as ``FIRM_HALTED`` but scoped to live-trading specifically (e.g.
  during a maintenance window).
* ``APEX_LIVE_KILL_REASON`` -- optional human-readable reason
  surfaced in the raised exception when either flag is set. If
  unset, the exception falls back to a generic message.

Paper venues that route through ``ibkr.IbkrClientPortalVenue`` /
``tastytrade.TastytradeVenue`` etc. call this gate before every
``place_order`` so a halt instantly stops new orders without each
venue having to re-implement the check.
"""

from __future__ import annotations

import os


class LiveTradingDisabled(RuntimeError):
    """Raised when an operator gate refuses a live order.

    The exception's ``.reason`` attribute carries a stable code
    (``"firm_halted"`` / ``"live_disabled"``) so callers can tell
    why the order was blocked without parsing the message string.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _is_truthy_env(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def assert_live_allowed() -> None:
    """Pass through when no kill signal is set; raise otherwise.

    Reads :data:`FIRM_HALTED` and :data:`APEX_LIVE_TRADING_DISABLED`
    from the environment on every call so an operator can flip the
    halt bit at runtime without restarting the bot.
    """
    if _is_truthy_env("FIRM_HALTED"):
        reason = os.environ.get("APEX_LIVE_KILL_REASON") or "firm halted"
        raise LiveTradingDisabled(
            f"live order blocked: FIRM_HALTED=true ({reason})",
            reason="firm_halted",
        )
    if _is_truthy_env("APEX_LIVE_TRADING_DISABLED"):
        reason = (
            os.environ.get("APEX_LIVE_KILL_REASON")
            or "live trading explicitly disabled"
        )
        raise LiveTradingDisabled(
            f"live order blocked: APEX_LIVE_TRADING_DISABLED=true ({reason})",
            reason="live_disabled",
        )


__all__ = ["LiveTradingDisabled", "assert_live_allowed"]
