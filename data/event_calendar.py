"""Operator-curated market event calendar.

A small YAML-backed reader that exposes a tuple of upcoming events for JARVIS
to weave into pre-consult context. Designed to be:

* **Failure-isolated** — missing file, malformed YAML, weird types: every
  load/upcoming call returns an empty tuple/list rather than raising. The
  consult path must never crash because the operator's calendar is stale.
* **Tiny** — a frozen dataclass plus two helpers. Operators hand-edit the
  YAML file. No web fetch, no caching, no daemon — keep this surface flat.

The seed YAML lives at ``var/eta_engine/state/event_calendar.yaml`` and is
seeded with a handful of high-severity macro prints (FOMC, CPI, EIA, NFP)
so the wiring is exercised on day one even if the operator hasn't curated
anything yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from eta_engine.scripts import workspace_roots

logger = logging.getLogger("eta_engine.data.event_calendar")

DEFAULT_YAML_PATH = workspace_roots.ETA_EVENT_CALENDAR_PATH


@dataclass(frozen=True)
class CalendarEvent:
    """A single operator-curated market event.

    ``kind`` is a short string like FOMC | CPI | EIA | NFP | EARNINGS |
    CRYPTO_UNLOCK; severity is an integer 1..3 (3 = highest).
    ``symbol`` is optional — None means market-wide.
    """

    ts_utc: str
    kind: str
    symbol: str | None
    severity: int


def _parse_event(raw: object) -> CalendarEvent | None:
    """Best-effort parse one mapping into a CalendarEvent. Returns None on any failure."""
    if not isinstance(raw, dict):
        return None
    try:
        ts_utc = str(raw.get("ts_utc", "")).strip()
        kind = str(raw.get("kind", "")).strip()
        symbol_raw = raw.get("symbol")
        symbol = None if symbol_raw is None else str(symbol_raw)
        severity = int(raw.get("severity", 1))
    except (TypeError, ValueError):
        return None
    if not ts_utc or not kind:
        return None
    return CalendarEvent(ts_utc=ts_utc, kind=kind, symbol=symbol, severity=severity)


def _parse_ts(ts_utc: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; returns None when unparseable."""
    raw = ts_utc.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=UTC)
    return out


def load(path: Path | None = None) -> tuple[CalendarEvent, ...]:
    """Load all events from ``path`` (defaults to ``DEFAULT_YAML_PATH``).

    Missing file, malformed YAML, or unexpected shapes all return an empty
    tuple. This function never raises.
    """
    target = path if path is not None else DEFAULT_YAML_PATH
    try:
        if not target.exists():
            return ()
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("event_calendar read failed for %s: %s", target, exc)
        return ()

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("event_calendar yaml parse failed for %s: %s", target, exc)
        return ()
    except Exception as exc:  # noqa: BLE001 — defensive: malformed input
        logger.warning("event_calendar unexpected parse error for %s: %s", target, exc)
        return ()

    if not isinstance(data, dict):
        return ()
    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        return ()

    out: list[CalendarEvent] = []
    for raw in raw_events:
        ev = _parse_event(raw)
        if ev is not None:
            out.append(ev)
    return tuple(out)


def upcoming(
    now: datetime,
    horizon_min: int = 60,
    path: Path | None = None,
) -> list[CalendarEvent]:
    """Return events whose ``ts_utc`` falls in ``[now, now + horizon_min]``.

    Past events (ts < now) and events beyond the horizon are filtered out.
    Returns an empty list on any failure.
    """
    try:
        all_events = load(path=path)
    except Exception as exc:  # noqa: BLE001 — load() shouldn't raise but guard anyway
        logger.warning("event_calendar.load unexpectedly raised: %s", exc)
        return []

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    horizon_end = now + timedelta(minutes=horizon_min)

    out: list[CalendarEvent] = []
    for ev in all_events:
        ts = _parse_ts(ev.ts_utc)
        if ts is None:
            continue
        if now <= ts <= horizon_end:
            out.append(ev)
    return out
