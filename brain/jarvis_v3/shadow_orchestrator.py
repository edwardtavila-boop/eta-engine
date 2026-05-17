"""Shadow trading orchestrator.

Connects live market data → synthetic signals → ShadowVenue fills →
ShadowPaperTracker → PromotionGate evaluation.

Runs alongside the Jarvis daemon in the same process.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.brain.jarvis_v3.shadow_pipeline import ShadowPipeline
from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

STATE_DIR = workspace_roots.ETA_JARVIS_INTEL_STATE_DIR


@dataclass
class ShadowSignal:
    bot_id: str
    strategy_id: str
    symbol: str
    side: str
    qty: int
    entry_price: float
    stop_price: float
    target_price: float
    regime: str = "unknown"
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(UTC).isoformat()


class ShadowOrchestrator:
    def __init__(
        self,
        pipeline: ShadowPipeline | None = None,
        *,
        state_path: Path = STATE_DIR / "shadow_orchestrator.json",
    ) -> None:
        self.pipeline = pipeline or ShadowPipeline.default()
        self.state_path = state_path
        self._running = False
        self._ticks = 0

    @classmethod
    def default(cls) -> ShadowOrchestrator:
        return cls(pipeline=ShadowPipeline.default())

    @property
    def enabled(self) -> bool:
        return self.pipeline.enabled

    @property
    def total_signals(self) -> int:
        return self._ticks

    async def tick(
        self,
        live_bars: dict[str, dict],
        i: int,
    ) -> list[dict]:
        """Process one tick: generate signals from live data, execute in shadow, evaluate promotions."""
        if not self.enabled:
            return []
        results: list[dict] = []

        # Generate synthetic signals from live market data
        signals = self._generate_signals(live_bars, i)
        for sig in signals:
            self.pipeline.record_fill(
                bot_id=sig.bot_id,
                strategy_id=sig.strategy_id,
                symbol=sig.symbol,
                side=sig.side,
                qty=sig.qty,
                entry_price=sig.entry_price,
                exit_price=sig.target_price if sig.side == "long" else sig.stop_price,
                pnl_r=self._estimate_r(sig),
                is_win=self._estimate_win(sig),
                regime=sig.regime,
            )
            results.append(
                {
                    "bot_id": sig.bot_id,
                    "strategy_id": sig.strategy_id,
                    "side": sig.side,
                    "entry": sig.entry_price,
                    "pnl_r": round(self._estimate_r(sig), 3),
                }
            )

        # Evaluate promotions every 10 ticks
        if i > 0 and i % 10 == 0:
            try:
                promos = self.pipeline.evaluate_promotions()
                results.append({"type": "promotion_eval", "results": promos})
            except Exception as exc:
                logger.warning("shadow promotion eval failed: %s", exc)

        self._ticks += len(signals)
        self._save_state()
        return results

    def _generate_signals(self, bars: dict[str, dict], i: int) -> list[ShadowSignal]:
        """Generate synthetic trading signals from live bar data.

        In production, this would call the actual strategy engines.
        For now, generates simple trend-following signals based on price action.
        """
        signals: list[ShadowSignal] = []

        # MNQ strategy: simple ORB-like signal based on bar direction
        mnq = bars.get("MNQ", {})
        if mnq.get("close") and mnq.get("open"):
            close = mnq["close"]
            open_ = mnq["open"]
            high = mnq.get("high", close)
            low = mnq.get("low", close)
            direction = "long" if close > open_ else "short"
            if i % 5 == 0:  # throttle: one signal every 5 ticks
                atr = (high - low) * 0.5
                signals.append(
                    ShadowSignal(
                        bot_id="mnq_futures",
                        strategy_id="mnq_orb_v2",
                        symbol="MNQ",
                        side=direction,
                        qty=1,
                        entry_price=close,
                        stop_price=close - atr * 2 if direction == "long" else close + atr * 2,
                        target_price=close + atr * 3 if direction == "long" else close - atr * 3,
                        regime="intraday",
                    )
                )

        # BTC strategy: simple trend
        btc = bars.get("BTC", {})
        if btc.get("close") and btc.get("open"):
            close = btc["close"]
            open_ = btc["open"]
            direction = "long" if close > open_ else "short"
            if i % 8 == 0:
                signals.append(
                    ShadowSignal(
                        bot_id="btc_sage_daily_etf",
                        strategy_id="btc_orb_v1",
                        symbol="BTC",
                        side=direction,
                        qty=1,
                        entry_price=close,
                        stop_price=close * 0.98 if direction == "long" else close * 1.02,
                        target_price=close * 1.03 if direction == "long" else close * 0.97,
                        regime="trend",
                    )
                )

        return signals

    @staticmethod
    def _estimate_r(sig: ShadowSignal) -> float:
        risk = abs(sig.entry_price - sig.stop_price)
        reward = abs(sig.target_price - sig.entry_price)
        if risk <= 0:
            return 0.0
        return reward / risk * (1 if sig.side == "long" else -1) * 0.5

    @staticmethod
    def _estimate_win(sig: ShadowSignal) -> bool:
        return sig.side == "long" and sig.target_price > sig.entry_price

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "total_signals": self._ticks,
            "shadow_pipeline": self.pipeline.summary(),
        }

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.summary(), indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("shadow_orch: save failed: %s", exc)
