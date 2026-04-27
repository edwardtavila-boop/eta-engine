"""EVOLUTIONARY TRADING ALGO // strategies.shadow_paper_tracker.

Per-(strategy, regime) re-entry tracker for the shadow paper book.

When a strategy is excluded from the live allowlist, we keep paper-
tracking it. The tracker buckets paper trades into fixed-size windows
and only signals re-entry once the strategy has produced a streak of
qualifying windows in a row.

The drill at
``scripts.chaos_drills.shadow_paper_tracker_drill`` is the
authoritative behavioural spec. In one sentence: a strategy
re-instates only after :data:`DEFAULT_REINSTATE_WINDOWS` consecutive
closed windows of :data:`DEFAULT_WINDOW_SIZE` trades each, with each
window's win rate >= :data:`DEFAULT_WIN_RATE_FLOOR` and cumulative R
>= 0. ``reinstate()`` clears the bucket so the operator's manual
allowlist edit is the source of truth.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


DEFAULT_WINDOW_SIZE: int = 20
DEFAULT_REINSTATE_WINDOWS: int = 3
DEFAULT_WIN_RATE_FLOOR: float = 0.52


@dataclass(frozen=True, slots=True)
class WindowStat:
    """Stats for one closed window of trades."""

    n: int
    wins: int
    win_rate: float
    cumulative_r: float
    qualifies: bool


class _Bucket:
    """One (strategy, regime) bucket: open trades + closed windows."""

    __slots__ = ("_open_trades", "_closed_windows")

    def __init__(self) -> None:
        self._open_trades: list[tuple[float, bool]] = []  # (pnl_r, is_win)
        self._closed_windows: deque[WindowStat] = deque()

    def record(
        self,
        *,
        pnl_r: float,
        is_win: bool,
        window_size: int,
        win_rate_floor: float,
    ) -> None:
        self._open_trades.append((pnl_r, is_win))
        if len(self._open_trades) >= window_size:
            window = self._open_trades[:window_size]
            self._open_trades = self._open_trades[window_size:]
            wins = sum(1 for _, w in window if w)
            n = len(window)
            cum_r = sum(r for r, _ in window)
            wr = wins / n if n else 0.0
            qualifies = wr >= win_rate_floor and cum_r >= 0.0
            self._closed_windows.append(
                WindowStat(
                    n=n,
                    wins=wins,
                    win_rate=wr,
                    cumulative_r=cum_r,
                    qualifies=qualifies,
                )
            )

    def recent(self, n: int) -> list[WindowStat]:
        if n <= 0:
            return []
        return list(self._closed_windows)[-n:]

    def clear(self) -> None:
        self._open_trades.clear()
        self._closed_windows.clear()


class ShadowPaperTracker:
    """Streak-gated re-entry tracker for shadow-paper strategies.

    The tracker is a pure in-memory state machine -- callers persist
    state separately if they need durability across restarts. The
    drill in ``scripts.chaos_drills.shadow_paper_tracker_drill``
    locks the state-machine contract.
    """

    def __init__(
        self,
        window_size: int = DEFAULT_WINDOW_SIZE,
        reinstate_windows: int = DEFAULT_REINSTATE_WINDOWS,
        win_rate_floor: float = DEFAULT_WIN_RATE_FLOOR,
    ) -> None:
        if window_size <= 0:
            raise ValueError(f"window_size must be positive, got {window_size}")
        if reinstate_windows <= 0:
            raise ValueError(
                f"reinstate_windows must be positive, got {reinstate_windows}"
            )
        if not 0.0 <= win_rate_floor <= 1.0:
            raise ValueError(
                f"win_rate_floor must be in [0,1], got {win_rate_floor}"
            )
        self.window_size = window_size
        self.reinstate_windows = reinstate_windows
        self.win_rate_floor = win_rate_floor
        self._buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)

    def record_shadow_trade(
        self,
        strategy: str,
        regime: str,
        *,
        pnl_r: float,
        is_win: bool,
    ) -> None:
        """Record one paper trade for ``(strategy, regime)``."""
        self._buckets[(strategy, regime)].record(
            pnl_r=pnl_r,
            is_win=is_win,
            window_size=self.window_size,
            win_rate_floor=self.win_rate_floor,
        )

    def should_reinstate(self, strategy: str, regime: str) -> bool:
        """Return True iff the last ``reinstate_windows`` are all qualifying."""
        recent = self._buckets[(strategy, regime)].recent(self.reinstate_windows)
        if len(recent) < self.reinstate_windows:
            return False
        return all(w.qualifies for w in recent)

    def reinstate(self, strategy: str, regime: str) -> None:
        """Clear the bucket so the next streak starts from empty."""
        self._buckets[(strategy, regime)].clear()

    def recent_window_stats(
        self, strategy: str, regime: str
    ) -> list[WindowStat]:
        """Most recent up-to-``reinstate_windows`` closed windows."""
        return self._buckets[(strategy, regime)].recent(self.reinstate_windows)


__all__ = [
    "DEFAULT_REINSTATE_WINDOWS",
    "DEFAULT_WINDOW_SIZE",
    "DEFAULT_WIN_RATE_FLOOR",
    "ShadowPaperTracker",
    "WindowStat",
]
