"""Pre-consult context enrichment.

Builds the small contextual envelope JARVIS wants in front of every consult:

* **multi-timeframe synthesis** — best-effort 5m/1h/D snapshot per symbol
  via ``eta_engine.data.library.default_library``. Each TF either yields a
  small dict (close, EMA20 vs close, trend direction) or an empty dict if
  the lookup failed.
* **nearby events** — hits the operator-curated event_calendar for prints
  inside the next 60 minutes.
* **session + time-of-day risk** — derived from ``now.hour`` in UTC; risk
  is a 0..1 scalar JARVIS uses to taper size during fragile windows.
* **multi_tf_agreement** — averaged trend signal across whichever TFs
  returned data, in ``[-1, +1]``. 0 when nothing is available.

Failure semantics: ``enrich()`` never raises. Any exception inside the
data fetch, calendar load, or computation falls back to safe defaults
(empty dicts, 0.0 agreement, neutral session string).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eta_engine.data import event_calendar

logger = logging.getLogger("eta_engine.jarvis_v3.context_enricher")

EXPECTED_HOOKS = ("enrich",)

_TFS: tuple[str, ...] = ("5m", "1h", "D")


@dataclass(frozen=True)
class EnrichedContext:
    """The bundle JARVIS reads before running its policy stack.

    ``multi_tf`` maps timeframe → small dict with at least ``close``,
    ``ema20_above``, ``trend_dir`` (+1 bull, -1 bear, 0 flat) when the
    library could resolve the symbol/TF; an empty dict otherwise.

    ``nearby_events`` is a tuple of ``CalendarEvent`` within the 60 min
    horizon. ``session`` is one of ASIA | LONDON | NY_AM | NY_PM |
    OVERNIGHT. ``time_of_day_risk`` is a 0..1 scalar. ``multi_tf_agreement``
    is the mean of available ``trend_dir`` values, in ``[-1, +1]``.
    """

    multi_tf: dict[str, dict] = field(default_factory=dict)
    nearby_events: tuple = ()
    session: str = ""
    time_of_day_risk: float = 0.0
    multi_tf_agreement: float = 0.0
    # Hermes Bridge Site B: snippets pulled from Hermes Agent's web_search
    # tool when a severity-3 calendar event is within 30 min. Empty tuple
    # when Hermes is unreachable, no severity-3 event nearby, or the
    # request hit the 2s timeout. Sage policies can read this to colour
    # the bias narrative ("CPI just printed hotter, vendor breakeven
    # repriced") without each policy having to call out to Hermes itself.
    news_snippets: tuple[str, ...] = ()


def _detect_session(now: datetime) -> str:
    """Map UTC hour to a coarse trading-session label."""
    h = now.hour
    if h >= 22 or h < 2:
        return "ASIA"
    if 2 <= h < 8:
        return "ASIA"
    if 8 <= h < 13:
        return "LONDON"
    if 13 <= h < 17:
        return "NY_AM"
    if 17 <= h < 21:
        return "NY_PM"
    # 21..22 is the overnight handoff sliver.
    return "OVERNIGHT"


def _time_of_day_risk(now: datetime, session: str) -> float:
    """Risk scalar in [0, 1]; higher = more fragile window."""
    # OVERNIGHT (21..22) is the thinnest book — top of the risk band.
    if session == "OVERNIGHT":
        return 0.8
    # Last 30 min of NY_PM is the index close — book starts thinning.
    if session == "NY_PM" and now.hour == 20 and now.minute >= 30:
        return 0.8
    if session == "ASIA":
        return 0.5
    if session in {"LONDON", "NY_AM"}:
        return 0.2
    if session == "NY_PM":
        return 0.2
    return 0.5


def _fetch_tf_snapshot(library: Any, symbol: str, tf: str) -> dict:  # noqa: ANN401
    """Best-effort one-TF lookup. Returns ``{}`` on any failure."""
    try:
        dataset = library.get(symbol=symbol, timeframe=tf)
    except Exception as exc:  # noqa: BLE001 — guard library API
        logger.debug("library.get(%s, %s) failed: %s", symbol, tf, exc)
        return {}
    if dataset is None:
        return {}

    try:
        bars = library.load_bars(dataset, limit=25, limit_from="tail")
    except Exception as exc:  # noqa: BLE001 — guard library load
        logger.debug("load_bars failed for %s %s: %s", symbol, tf, exc)
        return {}

    closes = []
    for bar in bars:
        close = getattr(bar, "close", None)
        if close is None and isinstance(bar, dict):
            close = bar.get("close")
        if close is None:
            continue
        try:
            closes.append(float(close))
        except (TypeError, ValueError):
            continue
    if not closes:
        return {}

    latest = closes[-1]
    # Use whichever window we have (target 20) for the EMA20 proxy.
    window = closes[-20:] if len(closes) >= 20 else closes
    ema20 = sum(window) / float(len(window))
    if latest > ema20 * 1.001:
        trend_dir = 1
    elif latest < ema20 * 0.999:
        trend_dir = -1
    else:
        trend_dir = 0
    return {
        "close": latest,
        "ema20_above": latest > ema20,
        "trend_dir": trend_dir,
    }


def _build_multi_tf(symbol: str) -> dict[str, dict]:
    """Try every TF; collect into a fresh dict. Empty on library failure."""
    try:
        from eta_engine.data import library as data_library

        lib = data_library.default_library()
    except Exception as exc:  # noqa: BLE001 — library may be dormant
        logger.debug("default_library() unavailable: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for tf in _TFS:
        snap = _fetch_tf_snapshot(lib, symbol, tf)
        if snap:
            out[tf] = snap
    return out


def _agreement(multi_tf: dict[str, dict]) -> float:
    """Mean ``trend_dir`` across TFs that returned data; 0.0 if none."""
    trends = [snap.get("trend_dir", 0) for snap in multi_tf.values() if isinstance(snap, dict) and "trend_dir" in snap]
    if not trends:
        return 0.0
    return float(sum(trends)) / float(len(trends))


def enrich(
    symbol: str,
    asset_class: str,
    now: datetime | None = None,
) -> EnrichedContext:
    """Build the EnrichedContext for the given symbol.

    Never raises. On any internal failure the returned context falls back to
    safe defaults (empty multi_tf, empty nearby_events, 0.0 risk/agreement).
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    multi_tf: dict[str, dict] = {}
    try:
        multi_tf = _build_multi_tf(symbol)
    except Exception as exc:  # noqa: BLE001 — never let library breaks bubble
        logger.debug("multi_tf build failed: %s", exc)
        multi_tf = {}

    try:
        evs = event_calendar.upcoming(now, horizon_min=60)
    except Exception as exc:  # noqa: BLE001 — calendar should not raise
        logger.debug("event_calendar.upcoming failed: %s", exc)
        evs = []

    try:
        session = _detect_session(now)
    except Exception as exc:  # noqa: BLE001
        logger.debug("session detection failed: %s", exc)
        session = ""

    try:
        risk = _time_of_day_risk(now, session)
    except Exception as exc:  # noqa: BLE001
        logger.debug("time_of_day_risk failed: %s", exc)
        risk = 0.0

    try:
        agreement = _agreement(multi_tf)
    except Exception as exc:  # noqa: BLE001
        logger.debug("multi_tf_agreement failed: %s", exc)
        agreement = 0.0

    # ``asset_class`` is not used in the current rules but kept on the
    # signature so the conductor can pass it through and so future rules
    # can branch on it without an interface change.
    _ = asset_class

    # Hermes Bridge Site B — only fires when a severity-3 event is within
    # 30 min. 2s budget; failure path yields empty tuple. We send a single
    # web_search for the nearest high-severity event so the consult sees
    # at most one network call regardless of how many events cluster.
    news_snippets: tuple[str, ...] = ()
    try:
        high_sev_soon = [e for e in evs if int(getattr(e, "severity", 0) or 0) >= 3]
        if high_sev_soon:
            from eta_engine.brain.jarvis_v3 import hermes_client

            ev = high_sev_soon[0]
            query = f"latest news {getattr(ev, 'kind', '')}".strip()
            if query:
                hres = hermes_client.web_search(
                    query=query,
                    n=3,
                    timeout_s=2.0,
                )
                if hres.ok and isinstance(hres.data, list):
                    news_snippets = tuple(
                        str(s.get("snippet", ""))[:200] for s in hres.data if isinstance(s, dict) and s.get("snippet")
                    )
    except Exception as exc:  # noqa: BLE001 — never break enrich() over a network call
        logger.debug("hermes_web_search failed: %s", exc)

    return EnrichedContext(
        multi_tf=multi_tf,
        nearby_events=tuple(evs),
        session=session,
        time_of_day_risk=risk,
        multi_tf_agreement=agreement,
        news_snippets=news_snippets,
    )
