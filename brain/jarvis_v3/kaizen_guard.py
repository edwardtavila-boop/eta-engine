"""Kaizen Guard — safety net for the autonomous improvement engine.

Hardens the KaizenEngine with:

  * Change caps — max parameter changes per cycle, per day, per instrument
  * Rollback — auto-revert changes that degrade PnL within N trades
  * Circuit breaker — pause kaizen when drawdown exceeds threshold
  * Drift freeze — prevent changing a parameter too frequently
  * Audit — every guard action logged for post-mortem

Usage:
    guard = KaizenGuard(max_changes_per_cycle=5, max_daily_changes=20)
    if guard.admit(change, current_drawdown=150, daily_changes_applied=18):
        kaizen_engine.apply(change)
    else:
        logger.warning("kaizen change blocked by guard")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("kaizen_guard")


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    rule: str = ""
    cooldown_seconds: int = 0


@dataclass
class GuardStatus:
    active: bool
    circuit_breaker_tripped: bool
    circuit_reason: str = ""
    circuit_until: str = ""
    changes_today: int = 0
    max_daily: int = 20
    drawdown_current: float = 0.0
    drawdown_limit: float = 0.0
    rollbacks_total: int = 0
    blocked_total: int = 0


class KaizenGuard:
    """Safety net for autonomous parameter changes.

    Prevents the kaizen engine from:
      - Changing too many things at once (rate limit)
      - Changing the same parameter too frequently (cooldown)
      - Shipping changes during drawdown crisis (circuit breaker)
      - Degrading PnL without rollback (change tracking)
    """

    # Default thresholds
    MAX_CHANGES_PER_CYCLE = 5
    MAX_DAILY_CHANGES = 20
    MAX_PER_INSTRUMENT_PER_CYCLE = 3
    PARAMETER_COOLDOWN_SECONDS = 3600  # 1 hour
    DRAWDOWN_CIRCUIT_BREAKER_RATIO = 0.70  # 70% of max daily loss = pause
    DEGRADATION_TRADES_FOR_ROLLBACK = 10

    def __init__(
        self,
        *,
        max_changes_per_cycle: int = MAX_CHANGES_PER_CYCLE,
        max_daily_changes: int = MAX_DAILY_CHANGES,
        max_per_instrument: int = MAX_PER_INSTRUMENT_PER_CYCLE,
        parameter_cooldown_seconds: int = PARAMETER_COOLDOWN_SECONDS,
        dd_circuit_breaker_ratio: float = DRAWDOWN_CIRCUIT_BREAKER_RATIO,
        max_daily_loss: float = 150.0,
        state_dir: Path | str | None = None,
    ) -> None:
        self.max_changes_per_cycle = max_changes_per_cycle
        self.max_daily_changes = max_daily_changes
        self.max_per_instrument = max_per_instrument
        self.parameter_cooldown_s = parameter_cooldown_seconds
        self.dd_ratio = dd_circuit_breaker_ratio
        self.max_daily_loss = max_daily_loss

        self._state_dir = Path(state_dir) if state_dir else Path("state/kaizen")
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # Runtime state
        self._changes_today = 0
        self._today = ""
        self._changes_this_cycle = 0
        self._per_instrument: dict[str, int] = {}
        self._last_changed: dict[str, str] = {}  # parameter -> iso ts
        self._circuit_tripped = False
        self._circuit_reason = ""
        self._circuit_until = ""
        self._rollbacks: list[dict[str, Any]] = []
        self._blocked_count = 0
        self._loaded_today()

    # ── Main gate ───────────────────────────────────────────

    def admit(
        self,
        parameter: str,
        *,
        instrument: str = "",
        current_drawdown: float = 0.0,
        daily_changes: int | None = None,
        change_type: str = "parameter",
    ) -> GuardDecision:
        """Gate: should we allow this parameter change?

        Returns GuardDecision with allowed=True/False and reason.
        """
        self._reset_daily_if_new_day()

        # Rule 1: Circuit breaker
        if self._circuit_tripped:
            now = datetime.now(UTC)
            if self._circuit_until:
                try:
                    until = datetime.fromisoformat(self._circuit_until)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=UTC)
                    if now < until:
                        return self._deny(
                            f"circuit breaker active until {self._circuit_until}: {self._circuit_reason}",
                            "circuit_breaker",
                        )
                except (ValueError, TypeError):
                    pass

        # Rule 2: Drawdown gate
        dd_limit = self.max_daily_loss * self.dd_ratio
        if current_drawdown > dd_limit:
            self._circuit_tripped = True
            self._circuit_reason = f"drawdown ${current_drawdown:.0f} > limit ${dd_limit:.0f}"
            self._circuit_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            return self._deny(
                f"drawdown circuit breaker: ${current_drawdown:.0f} > ${dd_limit:.0f}",
                "dd_circuit_breaker",
            )

        # Rule 3: Cycle cap
        if self._changes_this_cycle >= self.max_changes_per_cycle:
            return self._deny(
                f"cycle cap reached ({self.max_changes_per_cycle} changes)",
                "cycle_cap",
            )

        # Rule 4: Daily cap
        daily = daily_changes if daily_changes is not None else self._changes_today
        if daily >= self.max_daily_changes:
            return self._deny(
                f"daily cap reached ({self.max_daily_changes} changes)",
                "daily_cap",
            )

        # Rule 5: Per-instrument cap
        if instrument:
            inst_count = self._per_instrument.get(instrument, 0)
            if inst_count >= self.max_per_instrument:
                return self._deny(
                    f"instrument cap for {instrument} ({self.max_per_instrument} changes)",
                    "instrument_cap",
                )

        # Rule 6: Parameter cooldown
        last_ts = self._last_changed.get(parameter)
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                elapsed = (datetime.now(UTC) - last_dt).total_seconds()
                if elapsed < self.parameter_cooldown_s:
                    remaining = self.parameter_cooldown_s - elapsed
                    return self._deny(
                        f"cooldown active for {parameter} ({remaining:.0f}s remaining)",
                        "parameter_cooldown",
                        cooldown_seconds=int(remaining),
                    )
            except (ValueError, TypeError):
                pass

        # ALLOW
        self._changes_this_cycle += 1
        self._changes_today += 1
        self._last_changed[parameter] = datetime.now(UTC).isoformat()
        if instrument:
            self._per_instrument[instrument] = self._per_instrument.get(instrument, 0) + 1
        return GuardDecision(allowed=True, reason="all gates passed")

    # ── Rollback ────────────────────────────────────────────

    def should_rollback(
        self,
        parameter: str,
        pre_change_sharpe: float,
        post_change_sharpe: float,
        trades_since_change: int,
    ) -> bool:
        """Return True if the change degraded performance enough to rollback."""
        if trades_since_change < self.DEGRADATION_TRADES_FOR_ROLLBACK:
            return False
        degradation = pre_change_sharpe - post_change_sharpe
        if degradation > 0.1:
            self._rollbacks.append(
                {
                    "parameter": parameter,
                    "ts": datetime.now(UTC).isoformat(),
                    "pre_sharpe": pre_change_sharpe,
                    "post_sharpe": post_change_sharpe,
                    "trades_since": trades_since_change,
                }
            )
            logger.warning(
                "ROLLBACK: %s degraded Sharpe %.3f -> %.3f after %d trades",
                parameter,
                pre_change_sharpe,
                post_change_sharpe,
                trades_since_change,
            )
            return True
        return False

    # ── Cycle management ────────────────────────────────────

    def reset_cycle(self) -> None:
        self._changes_this_cycle = 0
        self._per_instrument = {}

    def reset_circuit(self) -> None:
        self._circuit_tripped = False
        self._circuit_reason = ""
        self._circuit_until = ""

    def status(self) -> GuardStatus:
        self._reset_daily_if_new_day()
        return GuardStatus(
            active=not self._circuit_tripped,
            circuit_breaker_tripped=self._circuit_tripped,
            circuit_reason=self._circuit_reason,
            circuit_until=self._circuit_until,
            changes_today=self._changes_today,
            max_daily=self.max_daily_changes,
            drawdown_current=0.0,
            drawdown_limit=self.max_daily_loss * self.dd_ratio,
            rollbacks_total=len(self._rollbacks),
            blocked_total=self._blocked_count,
        )

    def save_state(self) -> None:
        state = {
            "changes_today": self._changes_today,
            "today": self._today,
            "last_changed": self._last_changed,
            "circuit_tripped": self._circuit_tripped,
            "circuit_reason": self._circuit_reason,
            "circuit_until": self._circuit_until,
            "rollbacks": self._rollbacks[-20:],
            "blocked_count": self._blocked_count,
        }
        path = self._state_dir / "guard_state.json"
        path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    def load_state(self) -> None:
        path = self._state_dir / "guard_state.json"
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text())
            self._today = state.get("today", "")
            self._changes_today = state.get("changes_today", 0)
            self._last_changed = state.get("last_changed", {})
            self._circuit_tripped = state.get("circuit_tripped", False)
            self._circuit_reason = state.get("circuit_reason", "")
            self._circuit_until = state.get("circuit_until", "")
            self._rollbacks = state.get("rollbacks", [])
            self._blocked_count = state.get("blocked_count", 0)
        except (OSError, json.JSONDecodeError):
            pass

    # ── Internal ────────────────────────────────────────────

    def _deny(self, reason: str, rule: str, cooldown_seconds: int = 0) -> GuardDecision:
        self._blocked_count += 1
        logger.debug("kaizen guard BLOCKED: %s (%s)", reason, rule)
        return GuardDecision(allowed=False, reason=reason, rule=rule, cooldown_seconds=cooldown_seconds)

    def _reset_daily_if_new_day(self) -> None:
        today = datetime.now(UTC).strftime("%Y%m%d")
        if today != self._today:
            self._today = today
            self._changes_today = 0

    def _loaded_today(self) -> None:
        self._reset_daily_if_new_day()
