"""NQ Futures Bot — ENGINE tier, hybrid from MNQ.

E-mini Nasdaq-100. Tick size $0.25, tick value $5.00, point value $20.00.
All logic is inherited from :class:`eta_engine.bots.mnq.bot.MnqBot` —
the only differences are the instrument point-value (drives position sizing),
tightened entry gates (higher min confluence), and NQ-flavored logging.
"""

from __future__ import annotations

import logging
from typing import Any

from eta_engine.bots.base_bot import (
    BotConfig,
    MarginMode,
    Position,
    RegimeType,
    Tier,
)
from eta_engine.bots.mnq.bot import MnqBot

logger = logging.getLogger(__name__)

NQ_CONFIG = BotConfig(
    name="NQ-Engine",
    symbol="NQ",
    tier=Tier.FUTURES,
    baseline_usd=12000.0,
    starting_capital_usd=12000.0,
    max_leverage=5.0,
    risk_per_trade_pct=1.0,
    daily_loss_cap_pct=2.5,
    max_dd_kill_pct=8.0,
    margin_mode=MarginMode.CROSS,
)

TICK_SIZE: float = 0.25
TICK_VALUE: float = 5.00
POINT_VALUE: float = 20.00


class NqBot(MnqBot):
    """NQ futures bot — hybrid_from_mnq. Same 4 setups, scaled instrument params.

    Inherits all setup logic, signal routing, sizing, and trailing-stop from
    :class:`MnqBot`. Overrides only:

    * ``POINT_VALUE_USD`` — $20 instead of $2 for correct contract sizing.
    * ``evaluate_entry`` — higher confluence minimums because NQ risk per
      contract is 10x MNQ.
    """

    # $20 per NQ point — drives _size_from_signal via MnqBot._size_from_signal.
    POINT_VALUE_USD: float = POINT_VALUE

    def __init__(
        self,
        config: BotConfig | None = None,
        **kwargs: Any,  # noqa: ANN401 - forwards router/session_levels/... to MnqBot
    ) -> None:
        super().__init__(config or NQ_CONFIG, **kwargs)

    # ── NQ-specific lifecycle logging ──

    async def start(self) -> None:
        logger.info(
            "NQ bot starting | capital=$%.2f symbol=%s levels=%d router=%s",
            self.config.starting_capital_usd,
            self._tradovate_symbol,
            len(self._liquidity_levels),
            "yes" if self._router is not None else "no",
        )

    async def stop(self) -> None:
        logger.info("NQ bot stopping | equity=$%.2f pnl=$%.2f", self.state.equity, self.state.todays_pnl)
        self._trailing_peak.clear()

    # ── NQ-specific sizing helper (pre-existing public surface) ──

    def position_size_contracts(self, risk_usd: float, stop_distance_points: float) -> int:
        """Contracts for a given risk in USD and stop distance in NQ points.

        NQ point = $20. Stop of 10 pts = $200 per contract.
        """
        if stop_distance_points <= 0:
            return 0
        dollar_risk_per_contract = stop_distance_points * POINT_VALUE
        contracts = int(risk_usd / dollar_risk_per_contract)
        return max(contracts, 0)

    def evaluate_entry(self, bar: dict[str, Any], confluence_score: float) -> bool:
        """NQ requires higher confluence due to 10x tick value vs MNQ."""
        if confluence_score < 6.0:
            return False
        regime = self.regime_filter(bar)
        if regime == RegimeType.RANGING and confluence_score < 8.0:
            return False
        return self.check_risk() and self.state.trades_today < 4

    def evaluate_exit(self, position: Position) -> bool:
        """NQ uses the same trailing-stop logic as MNQ (inherited)."""
        return super().evaluate_exit(position)
