"""EVOLUTIONARY TRADING ALGO  //  scripts.supervisor_session_wiring
=====================================================================
Glue between the JARVIS strategy supervisor's per-tick loop and the
``core.session_gate.SessionGate`` policy authority.

Why this module exists
----------------------
Until 2026-05-07 the supervisor never imported ``SessionGate`` at all.
The ``enable_session_gate: True`` flag in registry ``extras["edge_config"]``
was consumed only by ``EdgeAmplifier.session_phase_allows`` (a strategy-
mode-vs-phase blocker) — NOT by ``core.session_gate.SessionGate`` (the
EoD + news + RTH enforcer). Likewise ``daily_loss_limit_pct`` in
registry ``extras`` was only read by ``preflight_bot_promotion`` as
documentation; nothing enforced it at runtime.

The supervisor file (``jarvis_strategy_supervisor.py``) already weighs
in north of 4 kLoC. Rather than bloat it further with gate construction
and PnL-anchor logic, we keep the policy/state helpers here and expose
a small, easily-tested surface:

* :class:`BotSessionState` — per-bot rolling state (gate handle, daily
  PnL anchor, daily-loss-halt flag).
* :func:`build_session_gate` — builds a ``SessionGate`` from a bot's
  registry assignment, applying crypto vs futures defaults.
* :func:`evaluate_pre_entry_gate` — single-call check the supervisor
  fires before every ``submit_entry``. Returns
  ``(allowed: bool, reason: str)`` where ``reason`` is empty when
  allowed.
* :func:`should_flatten_now` — pass-through to the gate's EoD check.
* :func:`update_daily_loss_anchor` — refreshes the PnL anchor whenever
  the ET session date rolls; idempotent within a session.
* :func:`enforce_daily_loss_cap` — compares current realized PnL since
  anchor against the bot's registry-defined cap and updates the halt
  flag; returns ``(halted: bool, headline_pct: float)``.

Multi-asset awareness
---------------------
The gate config is derived from the bot's assignment ``extras``:

  * Crypto bots (``BTC``, ``ETH``, etc.) get a 24/7 RTH window
    (00:00-23:59 local) and EoD cutoff disabled (cutoff == RTH end);
    news-blackout still applies via the shared ``EventsCalendar``.
  * Futures bots default to CME RTH (08:30-15:00 CT) with EoD cutoff
    at 15:59 CT — matching the existing ``SessionGateConfig`` defaults.
  * ``enable_session_gate=False`` (or missing edge_config) returns
    ``None`` from ``build_session_gate`` so the supervisor falls back
    to legacy "always-on" behaviour for that bot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from eta_engine.core.session_gate import (
    REASON_ALLOWED,
    SessionGate,
    SessionGateConfig,
)

if TYPE_CHECKING:
    from eta_engine.core.events_calendar import EventsCalendar

log = logging.getLogger(__name__)


# Daily PnL anchors are taken at session-start in this timezone. ET is
# the operator's reporting tz for "the trading day". CT remains the gate
# tz for futures RTH; using ET vs CT for the daily anchor only matters
# at the rollover boundary, where ET-midnight is one hour earlier and
# matches the futures globex daily reset more closely than CT-midnight.
_DAILY_ANCHOR_TZ = "America/New_York"


__all__ = [
    "BotSessionState",
    "build_session_gate",
    "current_session_date",
    "enforce_daily_loss_cap",
    "evaluate_pre_entry_gate",
    "extract_daily_loss_limit_pct",
    "extract_gate_flags",
    "should_flatten_now",
    "update_daily_loss_anchor",
]


@dataclass
class BotSessionState:
    """Per-bot session-aware state owned by the supervisor.

    Attached to each ``BotInstance`` once at load time. The gate handle
    is built lazily from registry extras; the daily anchor refreshes
    on every ET-date rollover. ``halted_until_session_date`` is set to
    the session date the cap was breached on, so the next session-date
    rollover automatically clears the halt.
    """

    gate: SessionGate | None = None
    daily_pnl_anchor: float = 0.0
    daily_session_date: str = ""
    halted_until_session_date: str = ""
    # Diagnostic: last recorded daily-loss percentage relative to the
    # configured starting cash. Visible for unit tests + future
    # heartbeat enrichment.
    last_daily_loss_pct: float = 0.0


def extract_gate_flags(extras: dict[str, Any] | None) -> dict[str, Any]:
    """Pluck session-related flags out of the ``edge_config`` blob.

    Mirrors the lookup performed by ``EdgeAmplifier``: the flags live
    in ``extras["edge_config"]`` rather than at the top level. Missing
    blob -> empty dict (caller treats that as legacy behaviour).
    """
    if not isinstance(extras, dict):
        return {}
    edge_config = extras.get("edge_config")
    if not isinstance(edge_config, dict):
        return {}
    return {
        "enable_session_gate": bool(edge_config.get("enable_session_gate", False)),
        "is_crypto": bool(edge_config.get("is_crypto", False)),
        "strategy_mode": str(edge_config.get("strategy_mode", "")),
    }


def extract_daily_loss_limit_pct(
    extras: dict[str, Any] | None, default: float = 2.5,
) -> float:
    """Pull ``daily_loss_limit_pct`` from registry extras with a safe default.

    Falls back to ``default`` (matching ``BotConfig.daily_loss_cap_pct``'s
    own default) when the key is missing, the value is non-numeric, or
    the value is non-positive.
    """
    if not isinstance(extras, dict):
        return default
    raw = extras.get("daily_loss_limit_pct", default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val <= 0:
        return default
    return val


def build_session_gate(
    *,
    symbol: str,
    extras: dict[str, Any] | None,
    calendar: EventsCalendar | None = None,
) -> SessionGate | None:
    """Construct a ``SessionGate`` from a bot's registry assignment.

    Returns ``None`` when the bot's edge_config either is missing or
    has ``enable_session_gate=False`` — the supervisor treats that as
    "no gate, legacy behaviour" rather than building a permissive
    pass-through gate. This keeps the policy explicit: a gate object
    means the operator opted that bot into RTH/EoD/news enforcement.
    """
    flags = extract_gate_flags(extras)
    if not flags.get("enable_session_gate"):
        return None
    is_crypto = bool(flags.get("is_crypto"))

    if is_crypto:
        # Crypto bots trade 24/7. We still build a gate so the news
        # blackout fires (CPI / FOMC affect crypto via cross-correlation
        # with risk assets). Use UTC with a full-day window and put the
        # EoD cutoff at the very end so should_flatten_eod never fires
        # spuriously — crypto bots flatten on PnL/trailing logic, not
        # a wall-clock cutoff.
        cfg = SessionGateConfig(
            timezone_name="UTC",
            rth_start_local=time(0, 0),
            rth_end_local=time(23, 59, 59),
            eod_cutoff_local=time(23, 59, 59),
            block_entries_during_news=True,
        )
    else:
        # Futures bots: default CME RTH (08:30-15:00 CT) with 15:59
        # cutoff. Note the cutoff sits OUTSIDE the RTH window in the
        # default config, so "outside_rth" wins over "eod_cutoff" for
        # late bars; that's deliberate and matches the existing
        # session_gate test suite. We extend the RTH end to 16:00 CT
        # so the 15:59 cutoff has a chance to be the dominant reason
        # for the final minute of the session — operators reviewing
        # logs can distinguish "supervisor refused at the cutoff"
        # from "we missed the close entirely".
        cfg = SessionGateConfig(
            timezone_name="America/Chicago",
            rth_start_local=time(8, 30),
            rth_end_local=time(16, 0),
            eod_cutoff_local=time(15, 59),
            block_entries_during_news=True,
        )

    log.debug(
        "session_gate built: symbol=%s is_crypto=%s tz=%s rth=%s-%s eod=%s",
        symbol, is_crypto, cfg.timezone_name,
        cfg.rth_start_local, cfg.rth_end_local, cfg.eod_cutoff_local,
    )
    return SessionGate(config=cfg, calendar=calendar)


def evaluate_pre_entry_gate(
    state: BotSessionState | None,
    *,
    now: datetime,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for the supervisor's pre-entry hook.

    ``state=None`` (or ``state.gate=None``) returns ``(True, "")`` —
    legacy behaviour for bots that haven't opted into the session gate.
    """
    if state is None or state.gate is None:
        return True, ""
    allowed, reason = state.gate.entries_allowed(now)
    if allowed and reason == REASON_ALLOWED:
        return True, ""
    return False, reason


def should_flatten_now(
    state: BotSessionState | None,
    *,
    now: datetime,
) -> tuple[bool, str]:
    """Return ``(flatten, reason)`` for the supervisor's per-tick check.

    ``state=None`` -> ``(False, "")`` so legacy bots are never force-
    flattened by this hook. Crypto bots see ``False`` here too (their
    cutoff is 23:59:59 — should_flatten_eod won't fire in practice).
    """
    if state is None or state.gate is None:
        return False, ""
    return state.gate.should_flatten_eod(now)


def current_session_date(now: datetime, *, tz_name: str = _DAILY_ANCHOR_TZ) -> str:
    """ISO-format ``YYYY-MM-DD`` date in the daily-anchor timezone."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(ZoneInfo(tz_name)).date().isoformat()


def update_daily_loss_anchor(
    state: BotSessionState,
    *,
    realized_pnl: float,
    now: datetime,
) -> bool:
    """Roll the daily PnL anchor when the session date changes.

    Returns ``True`` if the anchor was rolled (new session detected);
    ``False`` when we're still inside the same session as the prior
    call. When the anchor rolls, ``halted_until_session_date`` is
    cleared automatically as long as it points at a prior date.
    """
    new_date = current_session_date(now)
    if new_date == state.daily_session_date:
        return False
    state.daily_session_date = new_date
    state.daily_pnl_anchor = float(realized_pnl)
    # Halt clears at the next session boundary regardless of the
    # specific date that tripped it; comparing dates is just a
    # belt-and-braces check.
    if state.halted_until_session_date and state.halted_until_session_date != new_date:
        log.info(
            "daily_loss halt cleared at session rollover: prior=%s new=%s",
            state.halted_until_session_date, new_date,
        )
        state.halted_until_session_date = ""
    state.last_daily_loss_pct = 0.0
    return True


def enforce_daily_loss_cap(
    state: BotSessionState,
    *,
    realized_pnl: float,
    starting_cash: float,
    daily_loss_limit_pct: float,
    now: datetime,
) -> tuple[bool, float]:
    """Check current realized PnL against the configured daily floor.

    Returns ``(halted, current_loss_pct)`` where ``halted`` is True
    iff the bot has crossed the floor and is now blocked from new
    entries until the next session. ``current_loss_pct`` is the
    instantaneous loss percentage relative to ``starting_cash`` (zero
    when the session is in profit).

    Uses ``starting_cash`` as the reference equity because that's the
    same reference ``BaseBot.check_risk()`` uses on the live bot side
    (``starting_capital_usd``). Keeping the two paths consistent
    avoids the lab-vs-runtime drift the risk-execution review flagged.
    """
    if starting_cash <= 0:
        return state.halted_until_session_date != "", 0.0
    if daily_loss_limit_pct <= 0:
        return state.halted_until_session_date != "", 0.0

    update_daily_loss_anchor(state, realized_pnl=realized_pnl, now=now)
    session_pnl = float(realized_pnl) - state.daily_pnl_anchor
    if session_pnl >= 0:
        state.last_daily_loss_pct = 0.0
        return state.halted_until_session_date != "", 0.0

    loss_pct = abs(session_pnl) / starting_cash * 100.0
    state.last_daily_loss_pct = loss_pct
    if loss_pct >= daily_loss_limit_pct:
        if not state.halted_until_session_date:
            log.warning(
                "daily_loss_cap breached: loss=%.3f%% limit=%.3f%% (session=%s)",
                loss_pct, daily_loss_limit_pct, state.daily_session_date,
            )
        state.halted_until_session_date = state.daily_session_date
        return True, loss_pct
    return state.halted_until_session_date != "", loss_pct
