"""APEX PREDATOR  //  strategies.shadow_paper_tracker
==========================================================
Reinstatement gate for strategies retired from the live allowlist.

When a strategy is paused (allowlist removal) we keep it running in
paper mode. Re-entry to live trading is gated behind a streak of
qualifying paper windows so a single lucky run can't sneak it back in:

    * a window closes after :data:`DEFAULT_WINDOW_SIZE` shadow trades
    * the last :data:`DEFAULT_REINSTATE_WINDOWS` consecutive windows
      must each meet the win-rate floor AND have non-negative R
    * :meth:`reinstate` clears the bucket once the operator promotes
      the strategy back

The tracker is a pure ledger by default. Constructing with a
``journal_path`` enables an opt-in JSONL sink so live ticks land in
``state/shadow_paper_tracker.jsonl`` for the SHADOW_TICK avenger
handler to tally.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SIZE: int = 20
DEFAULT_REINSTATE_WINDOWS: int = 3
DEFAULT_WIN_RATE_FLOOR: float = 0.52


@dataclass(frozen=True)
class WindowStats:
    n: int
    wins: int
    win_rate: float
    cum_r: float


class _Bucket:
    __slots__ = ("trades", "closed_windows", "max_windows")

    def __init__(self, max_windows: int) -> None:
        self.trades: list[tuple[float, bool]] = []  # (pnl_r, is_win)
        self.closed_windows: deque[WindowStats] = deque(maxlen=max(max_windows * 2, 1))
        self.max_windows = max_windows


class ShadowPaperTracker:
    """Per-(strategy, regime) bucket of shadow trades and closed windows."""

    def __init__(
        self,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        reinstate_windows: int = DEFAULT_REINSTATE_WINDOWS,
        win_rate_floor: float = DEFAULT_WIN_RATE_FLOOR,
        journal_path: Path | None = None,
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be > 0")
        if reinstate_windows <= 0:
            raise ValueError("reinstate_windows must be > 0")
        if not 0.0 <= win_rate_floor <= 1.0:
            raise ValueError("win_rate_floor must be in [0, 1]")
        self.window_size = window_size
        self.reinstate_windows = reinstate_windows
        self.win_rate_floor = win_rate_floor
        self.journal_path = journal_path
        self._buckets: dict[tuple[str, str], _Bucket] = defaultdict(
            lambda: _Bucket(max_windows=reinstate_windows)
        )

    def record_shadow_trade(
        self,
        strategy: str,
        regime: str,
        *,
        pnl_r: float,
        is_win: bool,
    ) -> None:
        bucket = self._buckets[(strategy, regime)]
        bucket.trades.append((pnl_r, is_win))
        if self.journal_path is not None:
            self._append_journal(
                strategy=strategy,
                regime=regime,
                pnl_r=pnl_r,
                is_win=is_win,
            )
        if len(bucket.trades) >= self.window_size:
            window = bucket.trades[: self.window_size]
            del bucket.trades[: self.window_size]
            wins = sum(1 for _, w in window if w)
            cum_r = sum(r for r, _ in window)
            bucket.closed_windows.append(
                WindowStats(
                    n=self.window_size,
                    wins=wins,
                    win_rate=wins / self.window_size,
                    cum_r=cum_r,
                )
            )

    def recent_window_stats(self, strategy: str, regime: str) -> list[WindowStats]:
        key = (strategy, regime)
        if key not in self._buckets:
            return []
        windows = list(self._buckets[key].closed_windows)
        return windows[-self.reinstate_windows :]

    def should_reinstate(self, strategy: str, regime: str) -> bool:
        recent = self.recent_window_stats(strategy, regime)
        if len(recent) < self.reinstate_windows:
            return False
        return all(
            w.win_rate >= self.win_rate_floor and w.cum_r >= 0.0
            for w in recent
        )

    def reinstate(self, strategy: str, regime: str) -> None:
        """Clear bucket after operator promotes strategy back to live."""
        self._buckets.pop((strategy, regime), None)

    def _append_journal(
        self,
        *,
        strategy: str,
        regime: str,
        pnl_r: float,
        is_win: bool,
    ) -> None:
        """Best-effort JSONL append. Failures degrade to a log warning
        so a writeable-disk hiccup never tears down the trading loop.
        """
        if self.journal_path is None:
            return
        rec = {
            "ts":       datetime.now(UTC).isoformat(),
            "strategy": strategy,
            "regime":   regime,
            "pnl_r":    pnl_r,
            "is_win":   bool(is_win),
        }
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            logger.warning(
                "shadow_paper_tracker journal write failed: %s", exc,
            )
