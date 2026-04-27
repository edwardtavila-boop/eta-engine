"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.shadow_paper_tracker_drill.

Drill: simulate reinstate; verify the tracker only flips after the full
streak of qualifying windows.

What this drill asserts
-----------------------
:class:`strategies.shadow_paper_tracker.ShadowPaperTracker` gates re-entry
onto the live allowlist behind:

* Default :data:`DEFAULT_REINSTATE_WINDOWS` (3) consecutive closed
  windows of :data:`DEFAULT_WINDOW_SIZE` (20) trades each.
* Each window's win-rate must be >= :data:`DEFAULT_WIN_RATE_FLOOR` (0.52).
* Each window's cumulative R must be >= 0.

A silent regression would either (a) let a strategy sneak back after
a single lucky window or (b) never reinstate even when the streak is
clean. We feed the tracker:

* 1 qualifying window -> should_reinstate False (too few windows).
* 3 losing windows -> should_reinstate False (below win-rate floor).
* 3 winning windows after a reset -> should_reinstate True.

Plus a :meth:`reinstate` call to confirm the bucket is cleared after
the operator acts on the signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.scripts.chaos_drills._common import drill_result
from eta_engine.strategies.shadow_paper_tracker import ShadowPaperTracker

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_shadow_paper_tracker"]


_STRATEGY: str = "atlas_trend_v1"
_REGIME: str = "TREND_UP"


def _feed_window(
    tracker: ShadowPaperTracker,
    *,
    wins: int,
    losses: int,
    pnl_per_win: float,
    pnl_per_loss: float,
) -> None:
    """Feed one full window of trades."""
    for _ in range(wins):
        tracker.record_shadow_trade(
            _STRATEGY,
            _REGIME,
            pnl_r=pnl_per_win,
            is_win=True,
        )
    for _ in range(losses):
        tracker.record_shadow_trade(
            _STRATEGY,
            _REGIME,
            pnl_r=pnl_per_loss,
            is_win=False,
        )


def drill_shadow_paper_tracker(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Drive the tracker through three phases and verify each decision."""
    tracker = ShadowPaperTracker(
        window_size=20,
        reinstate_windows=3,
        win_rate_floor=0.52,
    )

    # Phase 1: one qualifying window -- still not enough.
    _feed_window(tracker, wins=13, losses=7, pnl_per_win=1.0, pnl_per_loss=-0.4)
    if tracker.should_reinstate(_STRATEGY, _REGIME):
        return drill_result(
            "shadow_paper_tracker",
            passed=False,
            details="tracker reinstated after a single window (need 3 in a row)",
        )

    # Phase 2: three losing windows -- below win-rate floor, never reinstate.
    for _ in range(3):
        _feed_window(tracker, wins=5, losses=15, pnl_per_win=1.0, pnl_per_loss=-0.8)
    if tracker.should_reinstate(_STRATEGY, _REGIME):
        return drill_result(
            "shadow_paper_tracker",
            passed=False,
            details="tracker reinstated across losing windows",
        )

    # Phase 3: clear state, then 3 clean qualifying windows -> reinstate True.
    tracker.reinstate(_STRATEGY, _REGIME)
    for _ in range(3):
        _feed_window(tracker, wins=14, losses=6, pnl_per_win=1.1, pnl_per_loss=-0.4)
    if not tracker.should_reinstate(_STRATEGY, _REGIME):
        recent = [s.win_rate for s in tracker.recent_window_stats(_STRATEGY, _REGIME)]
        return drill_result(
            "shadow_paper_tracker",
            passed=False,
            details=f"tracker failed to reinstate after 3 clean windows; win_rates={recent}",
        )

    # reinstate() must clear the bucket -- subsequent queries are empty.
    tracker.reinstate(_STRATEGY, _REGIME)
    stats_after = tracker.recent_window_stats(_STRATEGY, _REGIME)
    if stats_after:
        return drill_result(
            "shadow_paper_tracker",
            passed=False,
            details=f"reinstate() did not clear bucket: {len(stats_after)} windows remain",
        )

    return drill_result(
        "shadow_paper_tracker",
        passed=True,
        details="tracker enforced 3-window streak; losing windows held gate; reinstate() cleared bucket",
        observed={
            "window_size": tracker.window_size,
            "reinstate_windows": tracker.reinstate_windows,
            "win_rate_floor": tracker.win_rate_floor,
        },
    )
