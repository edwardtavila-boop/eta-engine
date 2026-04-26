"""APEX PREDATOR  //  brain.jarvis_v3.forecast_journal
==========================================================
Thin JSONL appender for the JARVIS Master Command Center's forecast
panel. Mirrors the pattern of ``drift.jsonl`` and
``calibration.jsonl`` -- producers append; the dashboard panel tails.

Every producer that holds a :class:`Projection` (typically from
:meth:`StressForecaster.update`) can call :func:`record_projection`
once per tick to surface the latest snapshot in the operator console
without re-importing the forecaster module each refresh.

Schema (one line per call)::

    {
        "ts":         "<iso8601>",
        "level":      "<NORMAL|ELEVATED|HIGH|EXTREME>",  # see _level_band()
        "trend":      "<UP|FLAT|DOWN>",                  # signum of trend
        "trend_raw":  <float>,                           # smoothed delta
        "forecast_1": <float>,                           # 1-step ahead
        "forecast_3": <float>,                           # 3-step ahead
        "forecast_5": <float>,                           # 5-step ahead
        "samples":    <int>,
        "note":       "<str>"
    }

Path: ``$APEX_FORECAST_JOURNAL`` if set, else
``~/.jarvis/forecast.jsonl``. Best-effort write -- OSError logged at
WARNING; the producer's tick path keeps running.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apex_predator.brain.jarvis_v3.predictive import Projection

logger = logging.getLogger(__name__)


DEFAULT_FORECAST_JOURNAL: Path = Path("~/.jarvis/forecast.jsonl").expanduser()


# Stress-band thresholds. Intentionally on the cautious side -- a
# 0.4 composite already wants the operator's eyes on the dashboard.
_LEVEL_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.75, "EXTREME"),
    (0.55, "HIGH"),
    (0.40, "ELEVATED"),
    (0.0,  "NORMAL"),
)


def _level_band(composite: float) -> str:
    for threshold, label in _LEVEL_THRESHOLDS:
        if composite >= threshold:
            return label
    return "NORMAL"


def _trend_band(trend: float, *, flat_band: float = 0.005) -> str:
    """Coarsen the smoothed trend into UP / FLAT / DOWN. ``flat_band``
    is the half-width of the flat region -- |trend| under this is
    treated as flat to avoid jittery panel updates on tiny deltas."""
    if trend > flat_band:
        return "UP"
    if trend < -flat_band:
        return "DOWN"
    return "FLAT"


def journal_path() -> Path:
    """Resolve the journal path. Honours ``APEX_FORECAST_JOURNAL``
    env override so test runs don't pollute the operator's home dir.
    """
    override = os.environ.get("APEX_FORECAST_JOURNAL")
    return Path(override) if override else DEFAULT_FORECAST_JOURNAL


def record_projection(
    projection: Projection,
    *,
    note: str = "",
    ts: datetime | None = None,
) -> bool:
    """Append one record. Returns True on success, False on OSError
    (logged at WARNING). Producer code should not branch on the
    return value -- the journal is best-effort observability, not a
    hard dependency."""
    rec: dict[str, Any] = {
        "ts":         (ts or datetime.now(UTC)).isoformat(),
        "level":      _level_band(projection.level),
        "level_raw":  projection.level,
        "trend":      _trend_band(projection.trend),
        "trend_raw":  projection.trend,
        "forecast_1": projection.forecast_1,
        "forecast_3": projection.forecast_3,
        "forecast_5": projection.forecast_5,
        "samples":    projection.samples,
        "note":       note or projection.note,
    }
    path = journal_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError as exc:
        logger.warning("forecast_journal append failed: %s", exc)
        return False
    return True


__all__ = [
    "DEFAULT_FORECAST_JOURNAL",
    "journal_path",
    "record_projection",
]
