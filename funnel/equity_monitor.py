"""
EVOLUTIONARY TRADING ALGO  //  equity_monitor
=================================
Real-time equity tracking across the full bot fleet.
One eye on every account. Always.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BotEquity(BaseModel):
    """Snapshot of a single bot's equity state."""

    bot_name: str
    current_equity: float = 0.0
    peak_equity: float = 0.0
    baseline_usd: float = 0.0
    excess_usd: float = 0.0
    todays_pnl: float = 0.0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PortfolioState(BaseModel):
    """Aggregated view across every active bot."""

    bots: dict[str, BotEquity] = Field(default_factory=dict)
    total_equity: float = 0.0
    total_excess: float = 0.0
    total_pnl_today: float = 0.0
    correlation_matrix: dict[str, dict[str, float]] | None = None
    snapshot_ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class EquityMonitor:
    """Tracks equity across the bot fleet in real time."""

    def __init__(self) -> None:
        self._bots: dict[str, BotEquity] = {}

    # ── Registration ──

    def register_bot(self, name: str, baseline: float) -> None:
        """Register a bot with its baseline (starting) capital."""
        if baseline <= 0:
            raise ValueError(f"Baseline must be positive, got {baseline}")
        self._bots[name] = BotEquity(
            bot_name=name,
            current_equity=baseline,
            peak_equity=baseline,
            baseline_usd=baseline,
        )

    # ── Updates ──

    def update(self, name: str, equity: float, pnl: float) -> None:
        """Push a new equity + PnL reading for a bot."""
        if name not in self._bots:
            raise KeyError(f"Bot '{name}' not registered")
        bot = self._bots[name]
        bot.current_equity = equity
        bot.todays_pnl = pnl
        bot.peak_equity = max(bot.peak_equity, equity)
        bot.excess_usd = max(equity - bot.baseline_usd, 0.0)
        bot.updated_at = datetime.now(UTC)

    # ── Queries ──

    def get_portfolio_state(self) -> PortfolioState:
        """Aggregate all bots into a single portfolio snapshot."""
        total_eq = sum(b.current_equity for b in self._bots.values())
        total_ex = sum(b.excess_usd for b in self._bots.values())
        total_pnl = sum(b.todays_pnl for b in self._bots.values())
        return PortfolioState(
            bots=dict(self._bots),
            total_equity=round(total_eq, 2),
            total_excess=round(total_ex, 2),
            total_pnl_today=round(total_pnl, 2),
        )

    # ── Kill Switch ──

    def check_global_kill(self, max_portfolio_dd_pct: float = 15.0) -> bool:
        """True if portfolio drawdown from aggregate peak breaches threshold.

        Drawdown is calculated as:
            dd% = (sum_peaks - sum_current) / sum_peaks * 100
        """
        sum_peaks = sum(b.peak_equity for b in self._bots.values())
        if sum_peaks <= 0:
            return False
        sum_current = sum(b.current_equity for b in self._bots.values())
        dd_pct = (sum_peaks - sum_current) / sum_peaks * 100.0
        return dd_pct >= max_portfolio_dd_pct
