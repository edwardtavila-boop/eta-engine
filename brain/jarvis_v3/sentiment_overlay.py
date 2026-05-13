"""
JARVIS v3 // sentiment_overlay (T16)

External sentiment/news signal as a JARVIS feature. Polls LunarCrush
(for crypto) and a configurable RSS / Bigdata aggregator (for macro)
on a 15-minute cadence, distills the signal into a 0–1 fear/greed
scalar plus a small topic dictionary, persists to a sidecar JSON, and
exposes it via:

  * ``current_sentiment(asset_class)`` — for portfolio_brain.assess
    and the hermes-overrides skill to consult as one more feature
    when sizing decisions.
  * ``sentiment_history(n)`` — for T8 regime classifier training.

Design rules
------------
1. **NEVER blocks the consult hot-path.** Sentiment fetch is async +
   cached; consults read the cached value with zero network IO.
2. **Graceful degradation when sources are down.** Missing data → the
   feature is simply absent. portfolio_brain doesn't fail.
3. **All writes go under** ``var/eta_engine/state/sentiment/`` per
   CLAUDE.md hard rule #1.
4. **Cache age guard.** If the cache is older than ``STALE_AFTER_MIN``
   (default 60 min), ``current_sentiment`` returns ``None`` rather than
   stale data — better to skip the feature than mis-weight on hours-old
   sentiment.

This module is intentionally NOT chatty over the wire. Hermes Agent
talks to LunarCrush via the operator's existing MCP integration
(``mcp__4e13b96c-...__*`` connector). This module's job is just to
read the cached JSON Hermes drops and expose a Python-callable getter
for portfolio_brain / hot_learner / hermes_overrides skills.

Stub for now: the actual fetch task (a Hermes scheduled_task that polls
LunarCrush and writes to the cache file) is documented in
``deploy/hermes_vps_config.yaml`` example block. This module is the
READ surface only — it works the moment the fetch task starts emitting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.sentiment_overlay")

DEFAULT_CACHE_DIR = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\sentiment",
)
STALE_AFTER_MIN = 60

# Asset → cache file mapping. The Hermes scheduled task writes to these
# paths; this module reads from them. Operator can wire up additional
# assets by adding entries here.
_CACHE_FILES: dict[str, str] = {
    "BTC": "lunarcrush_btc.json",
    "ETH": "lunarcrush_eth.json",
    "SOL": "lunarcrush_sol.json",
    "macro": "macro_sentiment.json",  # SPX / macro / news aggregator
}

EXPECTED_HOOKS = (
    "current_sentiment",
    "sentiment_history",
    "write_sentiment_snapshot",
)


def _resolve_dir(d: Path | None) -> Path:
    return Path(d) if d is not None else DEFAULT_CACHE_DIR


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_cache(asset_class: str, cache_dir: Path | None = None) -> dict[str, Any] | None:
    """Return the latest sentiment snapshot for ``asset_class`` or ``None``.

    NEVER raises. Returns ``None`` for missing file, parse errors, or
    stale data (older than STALE_AFTER_MIN minutes).
    """
    fname = _CACHE_FILES.get(asset_class)
    if fname is None:
        # Try a case-insensitive lookup (e.g. "btc" → "BTC")
        for key, val in _CACHE_FILES.items():
            if key.lower() == asset_class.lower():
                fname = val
                break
    if fname is None:
        return None

    target = _resolve_dir(cache_dir) / fname
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sentiment cache parse failed for %s: %s", asset_class, exc)
        return None
    if not isinstance(data, dict):
        return None

    asof = _parse_iso(data.get("asof"))
    if asof is None:
        logger.warning("sentiment cache missing asof for %s — treating as stale", asset_class)
        return None
    if _now() - asof > timedelta(minutes=STALE_AFTER_MIN):
        logger.info(
            "sentiment cache for %s is stale (%s) — returning None",
            asset_class,
            asof,
        )
        return None
    return data


# ---------------------------------------------------------------------------
# Public read surface
# ---------------------------------------------------------------------------


def current_sentiment(
    asset_class: str,
    cache_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the latest sentiment snapshot for ``asset_class`` or ``None``.

    Snapshot shape:

        {
            "asof": ISO timestamp,
            "fear_greed": float in [0.0, 1.0],  # 0=peak fear, 1=peak greed
            "social_volume_z": float,  # z-score vs 30-day baseline
            "topic_flags": {"squeeze": bool, "capitulation": bool, ...},
            "raw_source": str,  # "lunarcrush" | "macro_rss" | "bigdata"
            "extras": dict,  # source-specific extras for debugging
        }

    Callers should treat ``None`` as "no sentiment signal available;
    proceed with whatever the cascade produced".
    """
    if not asset_class:
        return None
    return _load_cache(asset_class, cache_dir)


def sentiment_history(
    asset_class: str,
    n: int = 100,
    cache_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the last ``n`` historical snapshots for ``asset_class``.

    The fetch task writes a rolling history alongside the current snapshot
    at ``<cache_dir>/<asset>_history.jsonl``. Used by T8 regime classifier
    training. NEVER raises; returns ``[]`` on any failure.
    """
    if n <= 0:
        return []
    fname = _CACHE_FILES.get(asset_class)
    if fname is None:
        for key, val in _CACHE_FILES.items():
            if key.lower() == asset_class.lower():
                fname = val
                break
    if fname is None:
        return []
    history_name = fname.rsplit(".", 1)[0] + "_history.jsonl"
    target = _resolve_dir(cache_dir) / history_name
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("sentiment history read failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    # Most recent N — walk backwards
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= n:
            break
    return list(reversed(out))  # restore chronological order


# ---------------------------------------------------------------------------
# Public write surface — used by the Hermes fetch scheduled task
# ---------------------------------------------------------------------------


def write_sentiment_snapshot(
    asset_class: str,
    snapshot: dict[str, Any],
    cache_dir: Path | None = None,
    append_history: bool = True,
) -> bool:
    """Persist ``snapshot`` as the active reading for ``asset_class``.

    Atomic via temp-file rename. Optionally appends to the rolling
    history file used by T8 training. Returns True on success, False on
    failure (logged, not raised).

    The snapshot is annotated with ``asof = now`` if the caller didn't
    set one, so freshness check is robust.
    """
    if not asset_class or not isinstance(snapshot, dict):
        return False
    fname = _CACHE_FILES.get(asset_class)
    if fname is None:
        for key, val in _CACHE_FILES.items():
            if key.lower() == asset_class.lower():
                fname = val
                break
    if fname is None:
        # Allow the operator to add new assets at runtime by tolerating
        # unknown classes: fall back to lowercase-of-asset_class.json.
        fname = f"{asset_class.lower()}.json"

    base = _resolve_dir(cache_dir)
    target = base / fname

    if "asof" not in snapshot:
        snapshot = {**snapshot, "asof": _now().isoformat()}

    try:
        base.mkdir(parents=True, exist_ok=True)
        # Atomic write — temp + os.replace
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp_sentiment_", suffix=".json", dir=str(base))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2, default=str)
            os.replace(tmp_name, target)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        logger.warning("sentiment snapshot write failed for %s: %s", asset_class, exc)
        return False

    if append_history:
        history_target = base / (fname.rsplit(".", 1)[0] + "_history.jsonl")
        try:
            with history_target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, default=str) + "\n")
        except OSError as exc:
            logger.warning("sentiment history append failed: %s", exc)
            # Don't fail the whole call — the current snapshot is the
            # important part.
    return True
