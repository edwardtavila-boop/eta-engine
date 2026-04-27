"""
JARVIS v3 // predictive
=======================
Forward-looking stress projection.

v2 stress is a snapshot. v3 adds a simple EWMA-of-deltas projection that
answers "if this trend keeps going, what will the stress be in ``steps``
ticks?" That lets policy fire BEFORE a hard threshold is hit, not AT it
(the whole point of preventative risk management).

The math is deliberately simple -- a single-exponential Holt trend is
plenty for a 60s-tick context where we care about 1-5 step ahead.

Pure / deterministic. No numpy -- stdlib only.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, ConfigDict, Field


class Projection(BaseModel):
    """Forecasted stress over the next K steps."""

    model_config = ConfigDict(frozen=True)

    level: float = Field(ge=0.0, le=1.0)
    trend: float = Field(
        description="Smoothed per-step delta. Positive -> worsening.",
    )
    forecast_1: float = Field(ge=0.0, le=1.0, description="1-step-ahead forecast.")
    forecast_3: float = Field(ge=0.0, le=1.0, description="3-step-ahead forecast.")
    forecast_5: float = Field(ge=0.0, le=1.0, description="5-step-ahead forecast.")
    samples: int = Field(ge=0)
    note: str = ""


class StressForecaster:
    """Holt (double-exponential) smoothing on composite stress.

    Parameters
    ----------
    alpha : level smoothing factor in (0, 1]
    beta  : trend smoothing factor in (0, 1]
    maxlen: ring-buffer size for history (trajectory queries)
    """

    def __init__(
        self,
        *,
        alpha: float = 0.5,
        beta: float = 0.3,
        maxlen: int = 256,
    ) -> None:
        if not (0.0 < alpha <= 1.0):
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        if not (0.0 < beta <= 1.0):
            raise ValueError(f"beta must be in (0, 1], got {beta}")
        self.alpha = alpha
        self.beta = beta
        self._level: float | None = None
        self._trend: float = 0.0
        self._history: deque[float] = deque(maxlen=maxlen)

    def update(self, composite: float) -> Projection:
        """Push a new composite and return the current projection."""
        x = max(0.0, min(1.0, float(composite)))
        self._history.append(x)
        if self._level is None:
            # Bootstrap: level = x, trend = 0.
            self._level = x
            self._trend = 0.0
        else:
            prev_level = self._level
            new_level = self.alpha * x + (1 - self.alpha) * (prev_level + self._trend)
            # Clamp level to [0,1] -- it represents a stress COMPOSITE so overshoot
            # is meaningless. Trend is allowed to exceed [-1,1] because it's a delta.
            self._level = _clip(new_level)
            self._trend = self.beta * (self._level - prev_level) + (1 - self.beta) * self._trend
        return self._build_projection()

    def reset(self) -> None:
        self._level = None
        self._trend = 0.0
        self._history.clear()

    def _build_projection(self) -> Projection:
        lvl = self._level or 0.0
        tr = self._trend
        f1 = _clip(lvl + 1 * tr)
        f3 = _clip(lvl + 3 * tr)
        f5 = _clip(lvl + 5 * tr)
        note = ""
        if tr > 0.02:
            note = "stress worsening rapidly"
        elif tr > 0.005:
            note = "stress drifting up"
        elif tr < -0.02:
            note = "stress improving rapidly"
        elif tr < -0.005:
            note = "stress drifting down"
        else:
            note = "stress flat"
        return Projection(
            level=round(lvl, 4),
            trend=round(tr, 6),
            forecast_1=round(f1, 4),
            forecast_3=round(f3, 4),
            forecast_5=round(f5, 4),
            samples=len(self._history),
            note=note,
        )


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def projection_from_series(series: list[float]) -> Projection:
    """Convenience wrapper -- build a fresh forecaster and run through series."""
    fc = StressForecaster()
    last: Projection | None = None
    for v in series:
        last = fc.update(v)
    if last is None:
        return Projection(
            level=0.0,
            trend=0.0,
            forecast_1=0.0,
            forecast_3=0.0,
            forecast_5=0.0,
            samples=0,
            note="empty series",
        )
    return last
