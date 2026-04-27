"""EVOLUTIONARY TRADING ALGO // safety.fleet_risk_gate.

Fleet-level daily-loss aggregator. Tracks running same-day P&L
across every bot in the registry and raises
:class:`FleetRiskBreach` when the aggregate drawdown exceeds the
configured limit.

Why this exists
---------------
Per-bot ``daily_loss_cap_pct`` (set on each :class:`BotConfig`) is
not enough on a correlated fleet. Risk-sage flagged on 2026-04-27
that running btc_hybrid + eth_perp + sol_perp + crypto_seed
simultaneously means a single BTC -2% candle can stop out all four
in lockstep — composing the per-bot 1% caps into a 4% beta-weighted
event in one tick.

This gate watches the *fleet*. Bots register their P&L deltas via
:meth:`record_pnl`; the gate aggregates and surfaces a single
verdict via :meth:`is_tripped`. Adapters / venue clients call
:meth:`require_ok` before submitting an order — the gate raises if
the fleet is over its budget.

Stateful, in-process. The expected lifecycle is one
:class:`FleetRiskGate` per orchestrator process, owned by the firm-
command-center and shared across every bot's adapter via
dependency injection. Persistence is OUT OF SCOPE here — when the
process restarts, the gate resets to zero PnL. That's intentional;
the firm-gate / kill-switch latch handles cross-restart state.

Operator interface
------------------
The gate reads its limits from environment variables, same pattern
as :mod:`safety.position_cap`, so an operator can tighten the cap
without redeploy:

* ``APEX_FLEET_DAILY_LOSS_LIMIT_USD`` -- absolute USD budget. Once
  same-day net realized PnL goes below ``-limit``, the gate trips.
* ``APEX_FLEET_DAILY_LOSS_LIMIT_PCT`` -- alternate spec as a
  fraction of starting equity. Used iff the USD limit is unset.
* ``APEX_FLEET_RISK_DISABLED`` -- truthy-value disables the gate
  entirely (paper / test runs that don't want fleet aggregation).

Defaults: -3.5% of starting equity (matches risk-sage's
recommendation of "Hard fleet cap: 3.5% aggregate daily, enforced
in a new FleetRiskGate upstream of decision_sink.emit").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from threading import Lock

DEFAULT_LIMIT_PCT: float = 0.035  # 3.5% per risk-sage 2026-04-27


class FleetRiskBreach(RuntimeError):  # noqa: N818 -- domain term "Breach" preferred over "Error"
    """Raised when an order would breach the fleet daily-loss limit.

    Carries a structured snapshot of the fleet state at trip time so
    callers can surface it to the operator dashboard without parsing
    the message string.
    """

    def __init__(
        self,
        message: str,
        *,
        net_pnl_usd: float,
        limit_usd: float,
        bot_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.net_pnl_usd = net_pnl_usd
        self.limit_usd = limit_usd
        self.bot_id = bot_id


def _is_truthy_env(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _today_utc() -> date:
    return datetime.now(tz=UTC).date()


@dataclass
class FleetRiskGate:
    """In-process aggregate-PnL watchdog for the bot fleet.

    Construct with the fleet's starting equity (sum of every active
    bot's ``starting_capital_usd``) so the percent-based limit
    converts to a concrete USD threshold. The gate then accumulates
    PnL deltas via :meth:`record_pnl` and refuses orders via
    :meth:`require_ok` once the threshold is breached.

    A new calendar day (UTC) auto-resets the running aggregate on
    the next call to either method, so an overnight reset of all
    bots' state lines up with the gate.
    """

    fleet_starting_equity_usd: float
    #: Override the default 3.5% limit. ``None`` reads
    #: ``APEX_FLEET_DAILY_LOSS_LIMIT_USD`` and
    #: ``APEX_FLEET_DAILY_LOSS_LIMIT_PCT`` from env, falling back to
    #: ``DEFAULT_LIMIT_PCT`` of starting equity.
    limit_usd_override: float | None = None
    #: Read at construction time; subsequent env changes don't take
    #: effect. Set to True to bypass the gate entirely.
    disabled: bool = field(default_factory=lambda: _is_truthy_env("APEX_FLEET_RISK_DISABLED"))

    # --- private state ---
    _today: date = field(default_factory=_today_utc, init=False)
    _net_pnl_usd: float = field(default=0.0, init=False)
    _per_bot_pnl: dict[str, float] = field(default_factory=dict, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.fleet_starting_equity_usd <= 0:
            raise ValueError(
                f"fleet_starting_equity_usd must be positive, "
                f"got {self.fleet_starting_equity_usd}"
            )

    # ── Public API ──

    def limit_usd(self) -> float:
        """Resolve the active USD loss limit.

        Resolution order: explicit override -> env USD -> env PCT ->
        default PCT. Always returned as a positive value (the gate
        compares to ``-limit`` internally).
        """
        if self.limit_usd_override is not None:
            return abs(float(self.limit_usd_override))
        env_usd = os.environ.get("APEX_FLEET_DAILY_LOSS_LIMIT_USD", "").strip()
        if env_usd:
            try:
                return abs(float(env_usd))
            except ValueError:
                pass  # fall through
        env_pct = os.environ.get("APEX_FLEET_DAILY_LOSS_LIMIT_PCT", "").strip()
        if env_pct:
            try:
                return abs(float(env_pct)) * self.fleet_starting_equity_usd
            except ValueError:
                pass  # fall through
        return DEFAULT_LIMIT_PCT * self.fleet_starting_equity_usd

    def record_pnl(self, bot_id: str, delta_usd: float) -> None:
        """Accumulate a realized P&L delta from one bot.

        Positive ``delta_usd`` is a profit; negative is a loss. The
        gate keeps both per-bot and aggregate totals so the operator
        dashboard can attribute the trip to a specific bot.
        Idempotent across bot IDs — same bot can record many deltas
        per day.
        """
        with self._lock:
            self._maybe_rollover()
            self._net_pnl_usd += float(delta_usd)
            self._per_bot_pnl[bot_id] = self._per_bot_pnl.get(bot_id, 0.0) + float(delta_usd)

    def is_tripped(self) -> bool:
        """True iff aggregate same-day P&L is below ``-limit_usd``."""
        if self.disabled:
            return False
        with self._lock:
            self._maybe_rollover()
            return self._net_pnl_usd <= -self.limit_usd()

    def require_ok(self, *, bot_id: str | None = None) -> None:
        """Raise :class:`FleetRiskBreach` iff the gate is tripped.

        Pass ``bot_id`` so the structured exception attributes the
        block to the bot that tried to fire next; useful for
        operator dashboards.
        """
        if self.disabled:
            return
        with self._lock:
            self._maybe_rollover()
            if self._net_pnl_usd <= -self.limit_usd():
                raise FleetRiskBreach(
                    (
                        f"fleet daily-loss limit breached: "
                        f"net_pnl={self._net_pnl_usd:.2f} usd "
                        f"<= -{self.limit_usd():.2f} usd"
                    ),
                    net_pnl_usd=self._net_pnl_usd,
                    limit_usd=self.limit_usd(),
                    bot_id=bot_id,
                )

    def status(self) -> dict[str, object]:
        """Operator-dashboard payload."""
        with self._lock:
            self._maybe_rollover()
            return {
                "today_utc": self._today.isoformat(),
                "net_pnl_usd": round(self._net_pnl_usd, 2),
                "limit_usd": round(self.limit_usd(), 2),
                "tripped": self._net_pnl_usd <= -self.limit_usd(),
                "per_bot_pnl_usd": {k: round(v, 2) for k, v in self._per_bot_pnl.items()},
                "disabled": self.disabled,
                "fleet_starting_equity_usd": round(self.fleet_starting_equity_usd, 2),
            }

    def reset(self) -> None:
        """Manually clear the running aggregate (operator override)."""
        with self._lock:
            self._today = _today_utc()
            self._net_pnl_usd = 0.0
            self._per_bot_pnl.clear()

    # ── Internal ──

    def _maybe_rollover(self) -> None:
        """Reset the aggregate when the UTC calendar day rolls over.

        Caller MUST hold ``self._lock``.
        """
        today = _today_utc()
        if today != self._today:
            self._today = today
            self._net_pnl_usd = 0.0
            self._per_bot_pnl.clear()


# ---------------------------------------------------------------------------
# Process-level singleton + assert helper
# ---------------------------------------------------------------------------
#
# Venue clients call ``assert_fleet_within_budget()`` from their
# ``place_order`` path so the fleet trip threshold is enforced
# upstream of every order submission, regardless of which bot fired
# the order. This mirrors the ``assert_live_allowed()`` pattern in
# safety/live_gate.py — fail-shut when the gate is tripped, no-op
# when no gate has been registered (paper / unit-test paths).

_singleton: FleetRiskGate | None = None


def register_fleet_risk_gate(gate: FleetRiskGate | None) -> None:
    """Register the process-wide gate.

    The orchestrator calls this once at startup with the gate
    instance constructed for the active fleet. Subsequent calls
    overwrite the previous registration; pass ``None`` to clear
    (test resets, fleet shutdown).
    """
    global _singleton
    _singleton = gate


def get_fleet_risk_gate() -> FleetRiskGate | None:
    """Return the process-wide gate (or None if not registered)."""
    return _singleton


def assert_fleet_within_budget(*, bot_id: str | None = None) -> None:
    """Raise :class:`FleetRiskBreach` iff the registered gate is tripped.

    No-op when no gate has been registered — keeps paper /
    unit-test paths frictionless. Venue clients call this from
    ``place_order`` BEFORE submitting orders so a fleet that's
    already over its daily-loss budget is denied new exposure.

    Pass ``bot_id`` so the structured exception attributes the
    block to the bot whose order is being rejected.
    """
    gate = _singleton
    if gate is None:
        return
    gate.require_ok(bot_id=bot_id)
