"""EVOLUTIONARY TRADING ALGO  //  brain.pnl_drift.

Online drift detection on per-trade PnL streams.

Why this module exists
----------------------
``brain.avengers.drift_detector`` watches the *Sharpe gap* between the
backtest sample and the live sample in aggregate -- that's a good
periodic check but it needs N>=20 live observations before it can speak.

This module is the faster, noisier cousin: a PageHinkley detector
running in-line on every closed trade's PnL-in-R. It flags regime
breaks as soon as the cumulative deviation from the learning mean
exceeds an adaptive threshold. When an alarm fires, the orchestrator
can:

* auto-demote the affected (strategy, regime) arm in the Thompson
  allocator (decay posterior),
* fire the v0.1.47 PerformanceRetrospective engine with the recent
  trade history for a three-layer diagnosis,
* push a YELLOW alert to the operator.

Algorithm
---------
Per-stream PageHinkley:

    m_t = running mean up to t (online; no unbounded buffer)
    g_t = max(0, g_{t-1} + (x_t - m_t - delta))
    GL_t = min(GL_{t-1}, g_t)
    alarm = (g_t - GL_t) > threshold

A positive alarm means "x has drifted higher"; for our use we care
about *downward* drift (PnL getting worse), so we also maintain the
negative variant by flipping the sign. Alarm fires in either direction.

Parameters are intentionally conservative. Default ``delta=0.005`` and
``threshold=1.0`` on R-units -- tuned so that a 20-trade losing streak
at -0.5R each trips the alarm but random chop does not. Easy to
override per-strategy.

Pure stdlib + pydantic. No numpy, no scipy.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PageHinkleyDetector",
    "DriftAlarm",
    "DEFAULT_DELTA",
    "DEFAULT_THRESHOLD",
]


DEFAULT_DELTA: float = 0.005
DEFAULT_THRESHOLD: float = 1.0


class DriftAlarm(BaseModel):
    """Structured alarm payload."""

    model_config = ConfigDict(frozen=True)

    direction: str  # "up" | "down"
    cumulative: float
    threshold: float
    n_observations: int
    running_mean: float


class PageHinkleyDetector(BaseModel):
    """Two-sided PageHinkley detector for a single PnL stream."""

    model_config = ConfigDict(frozen=False)

    delta: float = Field(default=DEFAULT_DELTA, ge=0.0)
    threshold: float = Field(default=DEFAULT_THRESHOLD, gt=0.0)
    n: int = Field(default=0, ge=0)
    running_mean: float = 0.0
    # up-drift (x > mean) running statistics
    g_up: float = 0.0
    gmin_up: float = 0.0
    # down-drift (x < mean) running statistics
    g_dn: float = 0.0
    gmax_dn: float = 0.0

    def reset(self) -> None:
        self.n = 0
        self.running_mean = 0.0
        self.g_up = 0.0
        self.gmin_up = 0.0
        self.g_dn = 0.0
        self.gmax_dn = 0.0

    def update(self, x: float) -> DriftAlarm | None:
        """Feed one observation and return an alarm if drift detected."""
        self.n += 1
        # Welford-lite running mean
        self.running_mean += (x - self.running_mean) / self.n

        centered = x - self.running_mean

        # Up-drift stat
        self.g_up = max(0.0, self.g_up + centered - self.delta)
        self.gmin_up = min(self.gmin_up, self.g_up)

        # Down-drift stat (negated)
        self.g_dn = min(0.0, self.g_dn + centered + self.delta)
        self.gmax_dn = max(self.gmax_dn, self.g_dn)

        up_alarm = (self.g_up - self.gmin_up) > self.threshold
        dn_alarm = (self.gmax_dn - self.g_dn) > self.threshold

        if up_alarm:
            alarm = DriftAlarm(
                direction="up",
                cumulative=self.g_up - self.gmin_up,
                threshold=self.threshold,
                n_observations=self.n,
                running_mean=self.running_mean,
            )
            self.reset()
            return alarm

        if dn_alarm:
            alarm = DriftAlarm(
                direction="down",
                cumulative=self.gmax_dn - self.g_dn,
                threshold=self.threshold,
                n_observations=self.n,
                running_mean=self.running_mean,
            )
            self.reset()
            return alarm

        return None
