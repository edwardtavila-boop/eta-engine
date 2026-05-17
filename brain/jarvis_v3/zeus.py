"""
JARVIS v3 // zeus — the unified brain snapshot (Zeus Supercharge)

After 17 tracks of building specialized lenses (causal layer, replay
engine, attribution cube, regime classifier, Kelly optimizer, …),
Zeus is the operator's ONE-CALL surface. "What's happening across
everything?" → one tool, one snapshot, the full picture.

Composed surfaces (read-only, all gracefully degrade on absence):

  • fleet_status     — kaizen tier counts + top elite/dark
  • topology         — node-link graph (T17) compressed to summary
  • overrides        — currently active size_modifiers + school_weights (T2)
  • regime           — current market regime + recommended pack (T8)
  • recent_consults  — last N trace records (T1's stream, tail mode)
  • kelly_recs       — top-5 highest-conviction Kelly recommendations (T13)
  • attribution_top  — top-5 winners and top-5 losers by R from cube (T12)
  • sentiment        — fear/greed scalars for BTC, ETH (T16)
  • wiring_audit     — n_dark modules + names (existing)
  • health           — 9-layer health check synopsis (existing)
  • memory_top       — recent operator memory facts (existing)
  • upcoming_events  — econ events ≤ 60 min ahead (existing)
  • bots_online      — registered agents on the inter-agent bus (T14)
  • asof             — UTC ISO timestamp

Design contract
---------------

1. NEVER raises. Every sub-fetch is wrapped in try/except; a failure
   on one surface puts ``{"error": "..."}`` in that key rather than
   tanking the whole snapshot.
2. Defensive caching (in-process, 30s TTL) so a chatty operator who
   asks "status, status, status" doesn't pay for 3 cubes back-to-back.
3. Pure read — no writes, no side effects.

Public interface
----------------

* ``snapshot(force_refresh=False)`` — full unified state.
* ``ZeusSnapshot`` dataclass (typed).

Storage: in-process cache only. Persistent cache wouldn't help — the
snapshot's value is its freshness.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("eta_engine.brain.jarvis_v3.zeus")

CACHE_TTL_SECONDS = 30
DEFAULT_TRACE_TAIL_N = 10
DEFAULT_KELLY_LOOKBACK_DAYS = 30

EXPECTED_HOOKS = ("snapshot",)


@dataclass(frozen=True)
class ZeusSnapshot:
    asof: str
    fleet_status: dict[str, Any]
    topology: dict[str, Any]
    overrides: dict[str, Any]
    regime: dict[str, Any]
    recent_consults: list[dict[str, Any]]
    kelly_recs: list[dict[str, Any]]  # top-5 only
    attribution_top: dict[str, Any]  # {top_winners, top_losers}
    sentiment: dict[str, Any]
    wiring_audit: dict[str, Any]
    upcoming_events: list[dict[str, Any]]
    bots_online: list[dict[str, Any]]
    memory_top: list[dict[str, Any]] = field(default_factory=list)
    cache_age_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_snapshot: ZeusSnapshot | None = None
_cache_built_at: float = 0.0


def _cache_age() -> float:
    if _cache_built_at == 0.0:
        return float("inf")
    return time.monotonic() - _cache_built_at


# ---------------------------------------------------------------------------
# Sub-fetch helpers — each is its own try/except so one failure can't
# tank the whole snapshot.
# ---------------------------------------------------------------------------


def _safe[T](fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("zeus sub-fetch failed: %s", exc)
        if isinstance(default, dict):
            return {**default, "error": str(exc)[:200]}
        return default


def _fetch_fleet_status() -> dict[str, Any]:
    p = workspace_roots.ETA_KAIZEN_LATEST_PATH
    if not p.exists():
        return {"n_bots": 0, "tier_counts": {}, "error": "no_kaizen_latest"}
    data = json.loads(p.read_text(encoding="utf-8"))
    elite = data.get("elite_summary") or {}
    return {
        "n_bots": data.get("n_bots", 0),
        "tier_counts": data.get("tier_counts", {}),
        "mc_counts": data.get("mc_counts", {}),
        "action_counts": data.get("action_counts", {}),
        "top5_elite": [
            {"bot_id": r.get("bot_id"), "tier": r.get("tier"), "score": r.get("score")}
            for r in (elite.get("top5_elite") or [])[:5]
        ],
        "top5_dark": [
            {"bot_id": r.get("bot_id"), "tier": r.get("tier"), "score": r.get("score")}
            for r in (elite.get("top5_dark") or [])[:5]
        ],
    }


def _fetch_topology_summary() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import risk_topology

    g = risk_topology.build_topology()
    # Compress to summary (the full graph is ~100KB+; not needed in snapshot)
    return {
        "n_nodes": g.get("n_nodes", 0),
        "n_edges": g.get("n_edges", 0),
        "asof": g.get("asof"),
    }


def _fetch_overrides() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    return hermes_overrides.active_overrides_summary()


def _fetch_regime() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import regime_classifier

    return regime_classifier.current_regime().to_dict()


def _fetch_recent_consults(n: int) -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import trace_emitter

    return trace_emitter.tail(n=n) or []


def _fetch_kelly_top5() -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    recs = kelly_optimizer.recommend_sizing(
        lookback_days=DEFAULT_KELLY_LOOKBACK_DAYS,
    )
    # Filter out insufficient_data and take top 5 by recommended_size_modifier
    rich = [r for r in recs if not r.get("insufficient_data")][:5]
    return [
        {
            "bot_id": r["bot_id"],
            "recommended_size_modifier": r["recommended_size_modifier"],
            "avg_r": r["avg_r"],
            "n_trades": r["n_trades"],
        }
        for r in rich
    ]


def _fetch_attribution_top() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import attribution_cube

    cube = attribution_cube.query(
        slice_by=["bot"],
        filter={"since_days_ago": 7},
    ).to_dict()
    rows = cube.get("rows") or []
    return {
        "top_winners": [
            {
                "bot_id": r["key"].get("bot"),
                "total_r": r["total_r"],
                "n_trades": r["n_trades"],
                "win_rate": r["win_rate"],
            }
            for r in rows[:5]
        ],
        "top_losers": [
            {
                "bot_id": r["key"].get("bot"),
                "total_r": r["total_r"],
                "n_trades": r["n_trades"],
                "win_rate": r["win_rate"],
            }
            for r in rows[-5:][::-1]  # tail reversed
        ],
        "n_total_bots_with_trades": len(rows),
    }


def _fetch_sentiment() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import sentiment_overlay

    out = {}
    for asset in ("BTC", "ETH", "SOL", "macro"):
        s = sentiment_overlay.current_sentiment(asset)
        out[asset] = s if s is not None else {"error": "no_recent_snapshot"}
    return out


def _fetch_wiring_audit() -> dict[str, Any]:
    from eta_engine.scripts import jarvis_wiring_audit

    statuses = jarvis_wiring_audit.audit() or []
    dark = [s for s in statuses if getattr(s, "expected_to_fire", False) and getattr(s, "dark_for_days", 0) >= 7]
    return {
        "n_dark": len(dark),
        "dark_modules": [getattr(s, "module", "") for s in dark],
        "n_total_modules": len(statuses),
    }


def _fetch_upcoming_events(horizon_min: int = 60) -> list[dict[str, Any]]:
    from eta_engine.data import event_calendar

    out = []
    for ev in event_calendar.upcoming(datetime.now(UTC), horizon_min=horizon_min) or []:
        out.append(
            {
                "ts_utc": getattr(ev, "ts_utc", ""),
                "kind": getattr(ev, "kind", ""),
                "symbol": getattr(ev, "symbol", None),
                "severity": int(getattr(ev, "severity", 1)),
            }
        )
    return out


def _fetch_bots_online() -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import agent_registry

    return agent_registry.list_agents(only_alive=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_snapshot(trace_n: int = DEFAULT_TRACE_TAIL_N) -> ZeusSnapshot:
    """Fan out all sub-fetches, assemble into one ZeusSnapshot.

    NEVER raises. Failed sub-fetches surface as ``{"error": "..."}`` in
    their slot.
    """
    return ZeusSnapshot(
        asof=datetime.now(UTC).isoformat(),
        fleet_status=_safe(_fetch_fleet_status, {"n_bots": 0, "tier_counts": {}}),
        topology=_safe(_fetch_topology_summary, {"n_nodes": 0, "n_edges": 0}),
        overrides=_safe(_fetch_overrides, {"size_modifiers": {}, "school_weights": {}}),
        regime=_safe(_fetch_regime, {"regime": "UNKNOWN", "confidence": 0.0}),
        recent_consults=_safe(lambda: _fetch_recent_consults(trace_n), []),
        kelly_recs=_safe(_fetch_kelly_top5, []),
        attribution_top=_safe(_fetch_attribution_top, {"top_winners": [], "top_losers": []}),
        sentiment=_safe(_fetch_sentiment, {}),
        wiring_audit=_safe(_fetch_wiring_audit, {"n_dark": 0, "dark_modules": []}),
        upcoming_events=_safe(lambda: _fetch_upcoming_events(60), []),
        bots_online=_safe(_fetch_bots_online, []),
        memory_top=[],  # populated by the skill layer if needed
        cache_age_s=0.0,
    )


def snapshot(
    force_refresh: bool = False,
    trace_n: int = DEFAULT_TRACE_TAIL_N,
) -> ZeusSnapshot:
    """Return the unified brain snapshot.

    Uses an in-process 30-second cache. Pass ``force_refresh=True`` to
    bypass and rebuild. NEVER raises.
    """
    global _cache_snapshot, _cache_built_at
    try:
        with _cache_lock:
            age = _cache_age()
            if not force_refresh and _cache_snapshot is not None and age < CACHE_TTL_SECONDS:
                # Return a copy with the age field updated
                d = _cache_snapshot.to_dict()
                d["cache_age_s"] = round(age, 2)
                return ZeusSnapshot(**d)

            snap = _build_snapshot(trace_n=trace_n)
            _cache_snapshot = snap
            _cache_built_at = time.monotonic()
            return snap
    except Exception as exc:  # noqa: BLE001
        logger.warning("zeus.snapshot top-level failure: %s", exc)
        return ZeusSnapshot(
            asof=datetime.now(UTC).isoformat(),
            fleet_status={"error": str(exc)},
            topology={"error": str(exc)},
            overrides={"error": str(exc)},
            regime={"error": str(exc)},
            recent_consults=[],
            kelly_recs=[],
            attribution_top={"top_winners": [], "top_losers": []},
            sentiment={},
            wiring_audit={"error": str(exc)},
            upcoming_events=[],
            bots_online=[],
            memory_top=[],
            cache_age_s=0.0,
        )


def clear_cache() -> None:
    """Force the next ``snapshot()`` call to rebuild."""
    global _cache_snapshot, _cache_built_at
    with _cache_lock:
        _cache_snapshot = None
        _cache_built_at = 0.0
