"""Per-symbol last-sage-report cache (Wave-6 pre-live, 2026-04-27).

Bridges ``v22_sage_confluence`` (which produces a ``SageReport`` during
evaluation) and the bot's ``record_fill_outcome`` (which feeds each
school's verdict + realized R into the EdgeTracker).

The natural alternative -- threading the report through ``ActionResponse``
-- would require modifying a Pydantic model + every consumer. This
keeps the live path untouched at the cost of a small module-level dict.

Semantics
---------
* Last-write-wins per (symbol, side). An ORDER_PLACE that's superseded
  by a newer one before the first one fills loses its sage attribution
  (acceptable noise -- edge tracker observations are an EWMA over many).
* ``pop_last`` is read-once: it removes the entry so a fill that comes
  in twice doesn't double-count.
* Thread-safe via module lock so the consultation pool + the bot
  callback can race without corruption.

Usage
-----
::

    # In v22_sage_confluence after ``consult_sage`` returns:
    from eta_engine.brain.jarvis_v3.sage.last_report_cache import set_last
    set_last(symbol, side, report)

    # In bot.record_fill_outcome:
    from eta_engine.brain.jarvis_v3.sage.last_report_cache import pop_last
    report = pop_last(symbol, side)
    if report is not None:
        # iterate per_school -> tracker.observe(...)
        ...
"""
from __future__ import annotations

import threading
from typing import Any

_CACHE: dict[tuple[str, str], Any] = {}
_LOCK = threading.Lock()


def set_last(symbol: str, side: str, report: Any) -> None:  # noqa: ANN401 -- duck-typed SageReport
    """Store the most recent SageReport for ``(symbol, side)``.

    Idempotent overwrite: a newer report replaces the older one.
    """
    if not symbol:
        return
    with _LOCK:
        _CACHE[(symbol, side or "")] = report


def pop_last(symbol: str, side: str = "") -> Any | None:  # noqa: ANN401 -- duck-typed SageReport
    """Remove + return the most recent SageReport for ``(symbol, side)``.

    Returns ``None`` when nothing's cached. Side-effect: drops the entry
    so a re-fired fill callback doesn't re-attribute.

    When ``side`` is empty, returns + drops the most-recent entry for
    the symbol regardless of side (handy when the bot doesn't track
    side at fill-close time).
    """
    if not symbol:
        return None
    with _LOCK:
        if side:
            return _CACHE.pop((symbol, side), None)
        # Side-agnostic: pop any entry matching symbol
        match_key = next(
            (k for k in _CACHE if k[0] == symbol),
            None,
        )
        if match_key is None:
            return None
        return _CACHE.pop(match_key)


def cache_size() -> int:
    """Diagnostic: how many (symbol, side) pairs are currently cached."""
    with _LOCK:
        return len(_CACHE)


def clear_all() -> int:
    """Drop every cached report. Returns count cleared."""
    with _LOCK:
        n = len(_CACHE)
        _CACHE.clear()
        return n
