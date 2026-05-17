from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)

DEFAULT_PIPELINE_STATE = workspace_roots.ETA_JARVIS_INTEL_STATE_DIR / "shadow_pipeline.json"
DEFAULT_FILLS_JOURNAL = workspace_roots.ETA_RUNTIME_STATE_DIR / "shadow_fills.jsonl"

_SHADOW_ENABLED = "SHADOW_OBSERVER_ENABLED"


@dataclass
class ShadowFill:
    bot_id: str
    strategy_id: str
    symbol: str
    side: str
    qty: int
    entry_price: float
    exit_price: float
    pnl_r: float
    is_win: bool
    regime: str
    filled_at: str = ""

    def __post_init__(self) -> None:
        if not self.filled_at:
            self.filled_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "bot_id": self.bot_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl_r": round(self.pnl_r, 4),
            "is_win": self.is_win,
            "regime": self.regime,
            "filled_at": self.filled_at,
        }


class ShadowPipeline:
    def __init__(
        self,
        *,
        fills_journal: Path = DEFAULT_FILLS_JOURNAL,
        state_path: Path = DEFAULT_PIPELINE_STATE,
        tracker: object = None,
        promotion_gate: object = None,
        window_size: int = 20,
        reinstate_windows: int = 3,
        win_rate_floor: float = 0.52,
    ) -> None:
        self.fills_journal = fills_journal
        self.state_path = state_path
        self._tracker = tracker
        self._gate = promotion_gate
        self._window_size = window_size
        self._reinstate_windows = reinstate_windows
        self._win_rate_floor = win_rate_floor
        self._fills: list[ShadowFill] = []
        self._tick_count = 0

    @classmethod
    def default(cls) -> ShadowPipeline:
        from eta_engine.strategies.shadow_paper_tracker import (
            ShadowPaperTracker,
        )

        tracker = ShadowPaperTracker(
            window_size=20,
            reinstate_windows=3,
            win_rate_floor=0.52,
        )
        inst = cls(tracker=tracker)
        with contextlib.suppress(Exception):
            inst.wire_default_gate()
        return inst

    def wire_default_gate(self) -> None:
        try:
            from eta_engine.brain.avengers.promotion import (
                PromotionGate,
                PromotionStage,
                StageThresholds,
            )

            thresholds = {
                PromotionStage.SHADOW: StageThresholds(
                    min_days=14,
                    min_trades=50,
                    min_sharpe=1.0,
                    max_dd_pct=5.0,
                    min_win_rate=0.45,
                    max_slip_bps=3.0,
                ),
                PromotionStage.PAPER: StageThresholds(
                    min_days=21,
                    min_trades=100,
                    min_sharpe=1.3,
                    max_dd_pct=4.0,
                    min_win_rate=0.48,
                    max_slip_bps=2.5,
                ),
            }
            self._gate = PromotionGate(
                state_path=self.state_path.parent / "promotion.json",
                journal_path=self.state_path.parent / "promotion.jsonl",
                thresholds=thresholds,
            )
            logger.info("shadow_pipeline: PromotionGate wired")
        except Exception as exc:
            logger.debug("shadow_pipeline: PromotionGate wire failed: %s", exc)

    @property
    def enabled(self) -> bool:
        return os.environ.get(_SHADOW_ENABLED, "0") in {"1", "true", "yes", "on"}

    @property
    def total_fills(self) -> int:
        return len(self._fills)

    @property
    def total_wins(self) -> int:
        return sum(1 for f in self._fills if f.is_win)

    @property
    def win_rate(self) -> float:
        if not self._fills:
            return 0.0
        return self.total_wins / len(self._fills)

    @property
    def avg_pnl_r(self) -> float:
        if not self._fills:
            return 0.0
        return sum(f.pnl_r for f in self._fills) / len(self._fills)

    @property
    def sharpe(self) -> float:
        if len(self._fills) < 2:
            return 0.0
        rs = [f.pnl_r for f in self._fills]
        mean_r = sum(rs) / len(rs)
        variance = sum((r - mean_r) ** 2 for r in rs) / (len(rs) - 1)
        if variance <= 0:
            return 0.0
        return mean_r / (variance**0.5) * (252**0.5)

    def record_fill(
        self,
        *,
        bot_id: str,
        strategy_id: str,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        exit_price: float,
        pnl_r: float,
        is_win: bool,
        regime: str = "unknown",
    ) -> None:
        fill = ShadowFill(
            bot_id=bot_id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_r=pnl_r,
            is_win=is_win,
            regime=regime,
        )
        self._fills.append(fill)
        self._log_fill(fill)

        if self._tracker is not None:
            try:
                self._tracker.record_shadow_trade(
                    strategy_id,
                    regime,
                    pnl_r=pnl_r,
                    is_win=is_win,
                )
            except Exception as exc:
                logger.warning("shadow_pipeline: tracker.record failed: %s", exc)

        self._tick_count += 1

    def evaluate_promotions(self) -> list[dict]:
        results: list[dict] = []
        if self._tracker is None or self._gate is None:
            return results

        try:
            from eta_engine.brain.avengers.promotion import (
                StageMetrics,
            )
        except ImportError:
            return results

        strategies = self._collect_strategies()
        for strategy_id, regime in strategies:
            try:
                should = self._tracker.should_reinstate(strategy_id, regime)
                stats = self._tracker.recent_window_stats(strategy_id, regime)
                wins = sum(1 for s in stats if s.qualifies) if stats else 0
                total_trades = sum(s.n for s in stats) if stats else 0
                sharpe = self.sharpe
                wr = wins / len(stats) if stats else 0.0

                metrics = StageMetrics(
                    trades=total_trades,
                    days_active=float(self._tick_count),
                    sharpe=round(sharpe, 4),
                    max_dd_pct=0.0,
                    win_rate=round(wr, 4),
                    mean_slippage_bps=0.5,
                    pnl=round(self.avg_pnl_r * total_trades, 4),
                )

                self._gate.register(strategy_id, stage=None)
                self._gate.update_metrics(strategy_id, metrics)
                decision = self._gate.evaluate(strategy_id)

                results.append(
                    {
                        "strategy_id": strategy_id,
                        "regime": regime,
                        "action": decision.action.value,
                        "sharpe": round(sharpe, 4),
                        "win_rate": round(wr, 4),
                        "trades": total_trades,
                        "should_reinstate": should,
                        "reasons": decision.reasons,
                    }
                )
            except Exception as exc:
                logger.warning(
                    "shadow_pipeline: evaluate failed for %s/%s: %s",
                    strategy_id,
                    regime,
                    exc,
                )

        self._save_state()
        return results

    def _collect_strategies(self) -> set[tuple[str, str]]:
        strategies: set[tuple[str, str]] = set()
        for f in self._fills:
            if f.strategy_id and f.regime:
                strategies.add((f.strategy_id, f.regime))
        return strategies

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "total_fills": self.total_fills,
            "total_wins": self.total_wins,
            "win_rate": round(self.win_rate, 4),
            "avg_pnl_r": round(self.avg_pnl_r, 4),
            "sharpe": round(self.sharpe, 4),
            "tick_count": self._tick_count,
            "tracker_wired": self._tracker is not None,
            "gate_wired": self._gate is not None,
        }

    def _log_fill(self, fill: ShadowFill) -> None:
        try:
            self.fills_journal.parent.mkdir(parents=True, exist_ok=True)
            with self.fills_journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(fill.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("shadow_pipeline: log fill failed: %s", exc)

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.summary(), indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("shadow_pipeline: save state failed: %s", exc)

    def load_fills(self) -> None:
        if not self.fills_journal.exists():
            return
        try:
            for line in self.fills_journal.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    self._fills.append(ShadowFill(**data))
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue
        except OSError:
            pass
