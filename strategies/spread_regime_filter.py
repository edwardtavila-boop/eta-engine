"""
EVOLUTIONARY TRADING ALGO  //  strategies.spread_regime_filter
==============================================================
Phase-4 partner module: rolling-median spread regime classifier
with hysteresis + staleness timeout.

Why this exists
---------------
Per docs/IBKR_PRO_DATA_INVENTORY.md Phase 4:
> spread_regime_filter — pause all entries when spread blows out
> beyond 4× the rolling median; resume once it drops back below
> 2× (hysteresis prevents flapping).

Originally co-located inside book_imbalance_strategy.py; broken
out into this dedicated module 2026-05-11 to:
  1. Match the file name the inventory + tests already reference
  2. Add a staleness timeout (was missing — paused state could
     persist forever if snapshots stopped arriving)
  3. Add a max-pause duration with operator alert hook

Mechanic
--------
- Maintain ``recent_spreads`` (rolling window, sized for actual
  snapshot cadence — default 5s)
- Track median spread; ratio = current / median
- Hysteresis: PAUSE when ratio >= pause_at_multiple, only resume
  once ratio drops below resume_at_multiple
- Staleness: if no snapshot has been seen in
  ``stale_after_seconds``, return verdict=STALE (caller should
  treat as PAUSE — fail closed)

Output verdicts
---------------
* NORMAL  — ratio < resume_at_multiple, not paused
* WIDE    — ratio in [resume_at_multiple, pause_at_multiple), not paused
* PAUSE   — ratio >= pause_at_multiple OR hysteresis still holding
* STALE   — no snapshot in stale_after_seconds (treat as PAUSE)

Backwards compatibility
-----------------------
``book_imbalance_strategy`` re-exports SpreadRegimeConfig,
SpreadRegimeState, update_spread_regime, make_spread_regime_filter
from this module so existing tests + imports keep working.
"""
from __future__ import annotations

# ruff: noqa: ANN401
# typing.Any is correct for the strategy-factory return — different
# concrete classes have different evaluate() signatures.
import bisect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class SpreadRegimeConfig:
    """Tuning surface for the global spread-regime filter."""
    lookback_minutes: int = 20         # rolling window for median spread
    pause_at_multiple: float = 4.0     # pause all entries when spread > median * this
    resume_at_multiple: float = 2.0    # only resume once spread drops below median * this
    snapshot_interval_seconds: float = 5.0  # depth capture cadence (was 1Hz comment, reality is 5s)
    stale_after_seconds: float = 60.0  # if no snap in this long, return STALE
    max_pause_seconds: float = 1800.0  # alert hook fires after PAUSE held this long (30min)


@dataclass
class SpreadRegimeState:
    """Carried across snapshots."""
    recent_spreads: list[float] = field(default_factory=list)
    sorted_spreads: list[float] = field(default_factory=list)
    paused: bool = False
    paused_at: datetime | None = None
    last_update_ts: datetime | None = None
    long_pause_alerted: bool = False  # prevents duplicate alerts


def update_spread_regime(snapshot: dict, config: SpreadRegimeConfig,
                          state: SpreadRegimeState,
                          *, now: datetime | None = None) -> dict:
    """Track rolling median spread; return regime status.

    Uses bisect.insort to maintain sorted_spreads incrementally
    instead of sort() on every call (was O(N log N) per snap → now
    O(N) for insort, with N capped at lookback_minutes / cadence).

    Args:
        snapshot: depth snapshot dict with at least {"spread": float}
        config:   tuning
        state:    mutable state carried across calls
        now:      override for testing; defaults to datetime.now(UTC)

    Output:
        {"paused": bool, "current_spread": float, "median": float,
         "ratio": float, "verdict": "NORMAL"|"WIDE"|"PAUSE"|"STALE",
         "last_update_age_seconds": float | None,
         "pause_held_seconds": float | None,
         "long_pause_warning": bool}

    Strategies should refuse to enter when verdict in {"PAUSE","STALE"}.
    """
    now = now or datetime.now(UTC)
    # Defensive: depth snapshots from real feeds sometimes carry
    # spread=None when bid or ask is missing.  dict.get's default
    # fires only when the KEY is absent, not when the value is None,
    # so a present-but-None field would slip through and crash
    # float() — coerce to 0.0 explicitly.
    spread_raw = snapshot.get("spread")
    spread = float(spread_raw) if spread_raw is not None else 0.0

    # Cap at lookback_minutes worth of snaps at the configured cadence
    max_len = max(1, int(config.lookback_minutes * 60 / max(config.snapshot_interval_seconds, 0.001)))

    # Maintain rolling list + sorted shadow incrementally
    state.recent_spreads.append(spread)
    bisect.insort(state.sorted_spreads, spread)
    if len(state.recent_spreads) > max_len:
        evicted = state.recent_spreads.pop(0)
        # Remove the evicted value from sorted_spreads (O(log N) lookup + O(N) delete)
        idx = bisect.bisect_left(state.sorted_spreads, evicted)
        if idx < len(state.sorted_spreads) and state.sorted_spreads[idx] == evicted:
            state.sorted_spreads.pop(idx)

    state.last_update_ts = now

    if not state.recent_spreads:
        return _build_result(state, spread, 0.0, 0.0, "NORMAL", now)

    median = state.sorted_spreads[len(state.sorted_spreads) // 2]

    if median <= 0:
        return _build_result(state, spread, median, 0.0, "NORMAL", now)

    ratio = spread / median

    # Hysteresis: pause at higher threshold, resume at lower
    if state.paused:
        if ratio <= config.resume_at_multiple:
            state.paused = False
            state.paused_at = None
            state.long_pause_alerted = False
            verdict = "NORMAL"
        else:
            verdict = "PAUSE"
    else:
        if ratio >= config.pause_at_multiple:
            state.paused = True
            state.paused_at = now
            verdict = "PAUSE"
        elif ratio >= config.resume_at_multiple:
            verdict = "WIDE"
        else:
            verdict = "NORMAL"

    return _build_result(state, spread, median, ratio, verdict, now,
                          config=config)


def check_staleness(state: SpreadRegimeState, config: SpreadRegimeConfig,
                     *, now: datetime | None = None) -> dict:
    """Standalone staleness check for callers that need to ask
    'is the regime data stale?' WITHOUT submitting a new snapshot.

    Returns the same dict shape as update_spread_regime, with
    verdict='STALE' when the last update is older than
    config.stale_after_seconds.

    Usage in a hot path:
        regime = check_staleness(state, config)
        if regime["verdict"] in {"PAUSE", "STALE"}:
            return  # refuse to enter
    """
    now = now or datetime.now(UTC)
    if state.last_update_ts is None:
        return {"paused": True, "current_spread": 0.0, "median": 0.0,
                "ratio": 0.0, "verdict": "STALE",
                "last_update_age_seconds": None,
                "pause_held_seconds": None,
                "long_pause_warning": False,
                "reason": "no_snapshot_yet"}
    age = (now - state.last_update_ts).total_seconds()
    if age > config.stale_after_seconds:
        return {"paused": True, "current_spread": 0.0, "median": 0.0,
                "ratio": 0.0, "verdict": "STALE",
                "last_update_age_seconds": round(age, 2),
                "pause_held_seconds": None,
                "long_pause_warning": False,
                "reason": "stale_no_recent_snapshot"}
    # Still fresh — return last-known regime
    return _build_result(state, 0.0, 0.0, 0.0,
                          "PAUSE" if state.paused else "NORMAL", now,
                          config=config)


def _build_result(state: SpreadRegimeState, spread: float, median: float,
                   ratio: float, verdict: str, now: datetime,
                   *, config: SpreadRegimeConfig | None = None) -> dict:
    last_update_age = None
    if state.last_update_ts is not None:
        last_update_age = round((now - state.last_update_ts).total_seconds(), 2)

    pause_held = None
    long_pause_warning = False
    if state.paused and state.paused_at is not None:
        pause_held = round((now - state.paused_at).total_seconds(), 2)
        if config is not None and pause_held > config.max_pause_seconds:
            long_pause_warning = True
            # First time we cross the threshold: flip the alerted flag so
            # callers can de-dupe.
            if not state.long_pause_alerted:
                state.long_pause_alerted = True

    return {"paused": state.paused,
            "current_spread": round(spread, 4),
            "median": round(median, 4),
            "ratio": round(ratio, 2),
            "verdict": verdict,
            "last_update_age_seconds": last_update_age,
            "pause_held_seconds": pause_held,
            "long_pause_warning": long_pause_warning}


def make_spread_regime_filter(config: SpreadRegimeConfig | None = None) -> Any:
    """Factory: returns a callable wrapper holding its own state.
    Mirrors how the registry-strategy bridge constructs other strategies.
    """
    cfg = config or SpreadRegimeConfig()
    state = SpreadRegimeState()

    class _SpreadRegimeFilter:
        def __init__(self) -> None:
            self.cfg = cfg
            self.state = state

        def update(self, snapshot: dict) -> dict:
            return update_spread_regime(snapshot, self.cfg, self.state)

        def check_staleness(self) -> dict:
            return check_staleness(self.state, self.cfg)

    return _SpreadRegimeFilter()
