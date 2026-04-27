"""
EVOLUTIONARY TRADING ALGO  //  session_filter
=================================
Session windows + news blackout gates.
Wrong session = wrong trade. Gate everything.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# High-impact event tags
# ---------------------------------------------------------------------------

HIGH_IMPACT_TAGS: list[str] = [
    "FOMC",
    "CPI",
    "PCE",
    "NFP",
    "GDP",
    "PPI",
    "ISM",
    "FED_SPEAK",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SessionWindow(BaseModel):
    """Named trading session with time boundaries (UTC)."""

    name: str
    start_time: time
    end_time: time
    timezone: str = "UTC"
    description: str = ""


class NewsEvent(BaseModel):
    """Scheduled economic event."""

    tag: str
    scheduled_time: datetime
    impact: str = Field(default="high", description="high | medium | low")


# ---------------------------------------------------------------------------
# Default session windows (all times UTC)
# ---------------------------------------------------------------------------

DEFAULT_SESSIONS: list[SessionWindow] = [
    SessionWindow(
        name="asia_close_bias",
        start_time=time(5, 0),
        end_time=time(7, 0),
        description="Asia session close. Bias formation for London.",
    ),
    SessionWindow(
        name="london_open_htf",
        start_time=time(7, 0),
        end_time=time(9, 0),
        description="London open. HTF displacement + liquidity sweep.",
    ),
    SessionWindow(
        name="ny_premarket",
        start_time=time(11, 0),
        end_time=time(13, 30),
        description="NY pre-market. Positioning before RTH.",
    ),
    SessionWindow(
        name="ny_open_htf",
        start_time=time(13, 30),
        end_time=time(16, 0),
        description="NY open HTF. Primary execution window.",
    ),
    SessionWindow(
        name="ny_lunch_reset",
        start_time=time(16, 0),
        end_time=time(18, 0),
        description="NY lunch. Low volume chop. Avoid new entries.",
    ),
    SessionWindow(
        name="ny_close_htf",
        start_time=time(18, 0),
        end_time=time(20, 0),
        description="NY close. Final displacement + settlement.",
    ),
]

_SESSION_INDEX: dict[str, SessionWindow] = {s.name: s for s in DEFAULT_SESSIONS}


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------


def _time_in_range(start: time, end: time, check: time) -> bool:
    """Check if `check` falls within [start, end). Handles midnight wrap."""
    if start <= end:
        return start <= check < end
    # wraps midnight
    return check >= start or check < end


def is_htf_window(
    dt: datetime,
    sessions: list[SessionWindow] | None = None,
) -> SessionWindow | None:
    """Return the active HTF session window, or None if outside all windows.

    Args:
        dt: Datetime to check (converted to UTC internally).
        sessions: Override session list. Defaults to DEFAULT_SESSIONS.
    """
    sessions = sessions or DEFAULT_SESSIONS

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc_time = dt.astimezone(UTC).time()

    for session in sessions:
        if _time_in_range(session.start_time, session.end_time, utc_time):
            return session
    return None


# ---------------------------------------------------------------------------
# News blackout
# ---------------------------------------------------------------------------


def is_news_blackout(
    dt: datetime,
    events: list[NewsEvent],
    pre_minutes: int = 15,
    post_minutes: int = 30,
) -> bool:
    """True if `dt` falls within the blackout zone of any high-impact event.

    Blackout = [event_time - pre_minutes, event_time + post_minutes].
    Only events whose tag is in HIGH_IMPACT_TAGS are considered.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    for event in events:
        if event.tag not in HIGH_IMPACT_TAGS:
            continue

        evt_time = event.scheduled_time
        if evt_time.tzinfo is None:
            evt_time = evt_time.replace(tzinfo=UTC)

        blackout_start = evt_time - timedelta(minutes=pre_minutes)
        blackout_end = evt_time + timedelta(minutes=post_minutes)

        if blackout_start <= dt <= blackout_end:
            return True

    return False


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def get_session(name: str) -> SessionWindow | None:
    """Lookup a default session by name."""
    return _SESSION_INDEX.get(name)


def list_sessions() -> list[str]:
    """List all default session window names."""
    return list(_SESSION_INDEX.keys())
