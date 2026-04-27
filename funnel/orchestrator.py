"""
EVOLUTIONARY TRADING ALGO  //  funnel.orchestrator
======================================
Coordinates equity monitor → sweep engine → allocator → transfer executor.
Single tick: push equities → detect sweeps → queue transfers → execute.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from eta_engine.core.sweep_engine import SweepConfig, SweepResult, check_sweep
from eta_engine.funnel.transfer import (
    TransferRequest,
    TransferResult,
    execute_transfer,
)

if TYPE_CHECKING:
    from eta_engine.funnel.equity_monitor import EquityMonitor
    from eta_engine.funnel.waterfall import LayerId
    from eta_engine.strategies.portfolio_rebalancer import RebalancePlan

# Allocator signature: splits a single USD amount into (asset -> usd) buckets.
AllocatorFn = Callable[[float], dict[str, float]]


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class FunnelTickResult(BaseModel):
    """Output of a single orchestrator tick."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sweeps_triggered: list[SweepResult] = Field(default_factory=list)
    transfers_queued: list[TransferRequest] = Field(default_factory=list)
    total_swept_usd: float = 0.0
    total_to_stake_usd: float = 0.0
    total_to_cold_usd: float = 0.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

TransferFn = Callable[[TransferRequest], Awaitable[TransferResult]]


class FunnelOrchestrator:
    """Glues the funnel pieces together into one callable tick."""

    def __init__(
        self,
        equity_monitor: EquityMonitor,
        sweep_configs: dict[str, SweepConfig],
        allocator: AllocatorFn,
        transfer_executor: TransferFn | None = None,
        cold_wallet_address: str = "cold_wallet",
    ) -> None:
        self.equity_monitor = equity_monitor
        self.sweep_configs = sweep_configs
        self.allocator = allocator
        self.transfer_executor: TransferFn = transfer_executor or execute_transfer
        self.cold_wallet_address = cold_wallet_address

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def tick(self, current_equities: dict[str, float]) -> FunnelTickResult:
        """Sample equities, detect sweeps, queue transfers. No execution."""
        result = FunnelTickResult()

        for bot, equity in current_equities.items():
            if bot in self.equity_monitor._bots:
                bot_state = self.equity_monitor._bots[bot]
                pnl = equity - bot_state.current_equity
                self.equity_monitor.update(bot, equity, pnl)

            cfg = self.sweep_configs.get(bot)
            if cfg is None:
                continue

            sweep = check_sweep(equity, cfg)
            if not sweep.action_required:
                continue

            result.sweeps_triggered.append(sweep)
            result.total_swept_usd += sweep.excess_usd
            result.total_to_stake_usd += sweep.to_stake
            result.total_to_cold_usd += sweep.to_reserve

            # 60% → staking allocator (split across yield sources)
            stake_alloc = self.allocator(sweep.to_stake)
            for asset, usd in stake_alloc.items():
                if usd <= 0.0:
                    continue
                result.transfers_queued.append(
                    TransferRequest(
                        from_bot=bot,
                        to_bot=f"staking_{asset}",
                        amount_usd=round(usd, 2),
                        reason=f"Sweep {bot}: {asset} stake allocation",
                    )
                )

            # 30% → reinvest back into the bot
            if sweep.to_reinvest > 0.0:
                result.transfers_queued.append(
                    TransferRequest(
                        from_bot=bot,
                        to_bot=bot,
                        amount_usd=round(sweep.to_reinvest, 2),
                        reason=f"Sweep {bot}: reinvest",
                    )
                )

            # 10% → cold reserve
            if sweep.to_reserve > 0.0:
                result.transfers_queued.append(
                    TransferRequest(
                        from_bot=bot,
                        to_bot="cold_wallet",
                        amount_usd=round(sweep.to_reserve, 2),
                        reason=f"Sweep {bot}: cold reserve",
                        requires_approval=True,
                    )
                )

        result.total_swept_usd = round(result.total_swept_usd, 2)
        result.total_to_stake_usd = round(result.total_to_stake_usd, 2)
        result.total_to_cold_usd = round(result.total_to_cold_usd, 2)
        return result

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute_tick(self, tick_result: FunnelTickResult) -> list[TransferResult]:
        """Fire every queued transfer through the executor."""
        out: list[TransferResult] = []
        for req in tick_result.transfers_queued:
            out.append(await self.transfer_executor(req))
        return out

    # ------------------------------------------------------------------
    # Rebalance channel (regime_allocator -> portfolio_rebalancer bridge)
    # ------------------------------------------------------------------

    async def execute_rebalance(
        self,
        rebalance_plan: RebalancePlan,
        layer_to_bot: dict[LayerId, str],
    ) -> list[TransferResult]:
        """Route a :class:`RebalancePlan` through the transfer executor.

        The rebalance channel is deliberately separate from the
        profit-sweep channel handled by :meth:`tick` / :meth:`execute_tick`:
          * The sweep channel moves *realized profit* (60% stake / 30%
            reinvest / 10% cold) and is triggered by bot-level equity
            crossing a sweep threshold.
          * The rebalance channel moves *position-level capital* between
            layers when the regime_allocator's target weights drift away
            from the funnel's actual per-layer equity.

        Each sweep in ``rebalance_plan.sweeps`` becomes one
        :class:`TransferRequest`. Sweeps whose source or destination
        layer is missing from ``layer_to_bot`` are skipped silently
        (inspect ``len(results) == len(plan.sweeps)`` to detect gaps).

        Parameters
        ----------
        rebalance_plan:
            Output of :func:`strategies.portfolio_rebalancer.plan_rebalance`.
        layer_to_bot:
            Map from :class:`LayerId` to bot-name string. Example::

                {LayerId.LAYER_1_MNQ: "mnq_apex",
                 LayerId.LAYER_2_BTC: "btc_core",
                 LayerId.LAYER_3_PERPS: "perps_heat",
                 LayerId.LAYER_4_STAKING: "staking_main"}

        Returns
        -------
        list[TransferResult] -- one entry per *executed* request
        (skipped sweeps produce no entry).
        """
        # Local import avoids a runtime cycle; portfolio_rebalancer
        # imports funnel.transfer which this module also imports.
        from eta_engine.strategies.portfolio_rebalancer import (
            rebalance_plan_to_transfers,
        )

        requests = rebalance_plan_to_transfers(rebalance_plan, layer_to_bot)
        results: list[TransferResult] = []
        for req in requests:
            results.append(await self.transfer_executor(req))
        return results
