"""
JARVIS v3 // dashboard_payload
==============================
JSON payload for the React JARVIS tile in the trading dashboard.

The dashboard already has a Court Verdict tile and a v3-aware Overview.
This module builds a compact, UI-ready dict describing JARVIS's current
state:

    {
      "health": "GREEN",
      "stress": {"composite": 0.32, "binding": "equity_dd", "components": [...]},
      "horizons": {"now":0.32, "next_15m":0.41, "next_1h":0.55, "overnight":0.40},
      "projection": {"level":0.32,"trend":+0.004,"forecast_5":0.35,"note":"flat"},
      "regime": "NEUTRAL",
      "suggestion": "TRADE",
      "recent_verdicts": [...],
      "active_gates": ["framework.autopilot", "bot.mnq"],
      "budget": {"hourly_burn_pct": 0.18, "daily_burn_pct": 0.42, "tier": "SONNET_OK"},
      "critique_flags": []
    }

This is pure transformation -- no live I/O. Caller assembles the
sources, calls ``build_payload(...)``, writes JSON to disk or pushes
via WebSocket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DashboardPayload(BaseModel):
    """Top-level payload the JARVIS tile consumes."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    health: str = Field(min_length=1)
    stress: dict[str, Any]
    horizons: dict[str, float]
    projection: dict[str, Any]
    regime: str
    session_phase: str
    suggestion: str
    recent_verdicts: list[dict[str, Any]] = Field(default_factory=list)
    active_gates: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    critique_flags: list[str] = Field(default_factory=list)
    precedent_hint: str = ""


def build_payload(
    *,
    health: str,
    stress: dict[str, Any],
    horizons: dict[str, float],
    projection: dict[str, Any],
    regime: str,
    session_phase: str,
    suggestion: str,
    recent_verdicts: list[dict[str, Any]] | None = None,
    active_gates: list[str] | None = None,
    budget: dict[str, Any] | None = None,
    critique_flags: list[str] | None = None,
    precedent_hint: str = "",
    now: datetime | None = None,
) -> DashboardPayload:
    """Assemble the dashboard payload.

    Every field is optional with sane defaults so partial sources (e.g.
    no precedent graph yet) still produce a valid payload.
    """
    return DashboardPayload(
        ts=now or datetime.now(UTC),
        health=health,
        stress=stress,
        horizons=horizons,
        projection=projection,
        regime=regime,
        session_phase=session_phase,
        suggestion=suggestion,
        recent_verdicts=recent_verdicts or [],
        active_gates=active_gates or [],
        budget=budget or {},
        critique_flags=critique_flags or [],
        precedent_hint=precedent_hint,
    )
