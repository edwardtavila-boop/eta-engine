"""APEX PREDATOR  //  brain.avengers.local_handlers
==========================================================
Concrete implementations of the local-handler bypass path the
``AvengerDaemon`` consults each tick. Each handler returns a dict
summary on success, or ``None`` to fall through to ``Fleet.dispatch``
(LLM round-trip).

All handlers are designed for a long-lived 24/7 VPS process:

  * Defensive against missing dependencies / paths -- never raise.
  * Bounded work -- no unbounded directory walks or unbounded reads.
  * Idempotent -- safe to call back-to-back without side-effect drift.
  * Stateless -- no module-level mutable cache that survives a tick.

The handlers are wired into ``daemon._run_local_background_task`` via
the dispatch table at the bottom of this file. Tests can monkey-patch
the table to inject a synthetic handler for any task.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apex_predator.brain.avengers.dispatch import BackgroundTask

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# DASHBOARD_ASSEMBLE -- write jarvis_dashboard.collect_state() to a snapshot
# ---------------------------------------------------------------------------

def _dashboard_assemble_handler(_task: BackgroundTask) -> dict[str, Any] | None:
    """Snapshot the dashboard state to a known path so the operator can
    read it without re-importing the module each tick.

    Output path: ``$APEX_DASHBOARD_PATH`` if set, else
    ``~/.jarvis/dashboard_latest.json``. Atomic write (tmp + rename) so
    a partial write never replaces a good snapshot.
    """
    try:
        from apex_predator.scripts.jarvis_dashboard import collect_state
    except ImportError as exc:
        logger.warning("dashboard_assemble: import failed -- %s", exc)
        return None

    try:
        state = collect_state()
    except Exception as exc:  # noqa: BLE001 -- collector must never crash daemon
        logger.warning("dashboard_assemble: collect_state raised -- %s", exc)
        return {"error": str(exc), "written": False}

    out_path = Path(
        os.environ.get("APEX_DASHBOARD_PATH")
        or Path.home() / ".jarvis" / "dashboard_latest.json"
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")
        tmp.replace(out_path)
    except OSError as exc:
        logger.warning("dashboard_assemble: write failed -- %s", exc)
        return {"error": str(exc), "written": False}

    return {
        "written": str(out_path),
        "panels":  list(state.keys()),
        "size_bytes": out_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# LOG_COMPACT -- prune timestamped per-run JSON files older than the
# rolling window. Targets only the gitignored runtime-artifact patterns.
# ---------------------------------------------------------------------------

# Each entry: (parent_dir_relative_to_repo, glob, max_age_days).
_COMPACT_TARGETS: tuple[tuple[str, str, float], ...] = (
    ("docs/broker_connections", "preflight_venue_connections_20*Z.json", 7.0),
    ("docs/btc_live",            "btc_live_paperfallback_20*Z.json",     14.0),
    ("docs/btc_paper",           "btc_paper_run_20*Z.json",              14.0),
    ("docs/btc_inventory",       "*_20*Z.json",                          14.0),
)


def _log_compact_handler(_task: BackgroundTask) -> dict[str, Any] | None:
    """Delete timestamped runtime-artifact files older than the
    per-target threshold. Files matching ``*_latest.json`` are
    explicitly skipped (the .gitignore comment marks them as the live
    snapshot to preserve)."""
    now = time.time()
    pruned = 0
    freed_bytes = 0
    errors: list[str] = []
    for rel_dir, glob, max_age_days in _COMPACT_TARGETS:
        target = _REPO_ROOT / rel_dir
        if not target.is_dir():
            continue
        cutoff = now - max_age_days * 86_400.0
        for path in target.glob(glob):
            if path.name.endswith("_latest.json"):
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            pruned += 1
            freed_bytes += size

    return {
        "pruned":      pruned,
        "freed_bytes": freed_bytes,
        "errors":      errors,
    }


# ---------------------------------------------------------------------------
# PROMPT_WARMUP -- exercise the Anthropic prefix cache so the next live
# request lands warm. The only billable handler; emits est_cost_usd so
# the daemon can mark billing_mode=anthropic_api.
# ---------------------------------------------------------------------------

# Cheapest tier-3 Haiku 4.5 priced at $0.001/$0.005 per 1K input/output
# tokens; warmup is ~250 input tokens / ~50 output. Real billing comes
# from the actual API response usage block.
_WARMUP_TOKEN_BUDGET: int = 250
_WARMUP_PRICE_PER_K_INPUT_USD: float = 0.001  # haiku-4.5 pricing


def _prompt_warmup_handler(_task: BackgroundTask) -> dict[str, Any] | None:
    """Warm the Anthropic prefix cache. Returns ``None`` when no API
    key is configured -- the daemon falls through to Fleet dispatch
    (which itself no-ops in dry-run modes).

    The Anthropic SDK is imported lazily so a system without it can
    still run the daemon -- the handler simply skips warmup work.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic  # noqa: F401  -- presence check only
    except ImportError:
        return None

    # Real warmup logic lives behind the SDK. We deliberately do NOT
    # issue an API call from inside the daemon's tick path on the test
    # harness -- a flag toggle protects production from accidentally
    # firing N requests/day from CI smoke runs.
    if not _is_warmup_enabled():
        return {
            "warmed":       0,
            "failed":       0,
            "est_cost_usd": 0.0,
            "skipped":      "warmup disabled (set APEX_PROMPT_WARMUP=1 to enable)",
        }

    # When enabled, we'd issue a small number of warmup requests here.
    # Returning a deterministic stub keeps the unit-test surface tight
    # while the real SDK path can be filled in once the operator
    # selects the prefix-set to warm.
    est_cost = (_WARMUP_TOKEN_BUDGET / 1000.0) * _WARMUP_PRICE_PER_K_INPUT_USD
    return {
        "warmed":       0,
        "failed":       0,
        "est_cost_usd": est_cost,
        "skipped":      "warmup SDK call not yet wired -- placeholder",
    }


def _is_warmup_enabled() -> bool:
    return os.environ.get("APEX_PROMPT_WARMUP", "0") == "1"


# ---------------------------------------------------------------------------
# SHADOW_TICK -- pull shadow_paper_tracker stats from a known journal
# and emit a one-line summary. Returns None when no shadow journal
# exists (the tracker hasn't been wired into a live tick yet).
# ---------------------------------------------------------------------------

_SHADOW_JOURNAL = _REPO_ROOT / "state" / "shadow_paper_tracker.jsonl"


def _shadow_tick_handler(_task: BackgroundTask) -> dict[str, Any] | None:
    """Tally strategy/regime stats from the shadow-paper tracker
    journal. Returns ``None`` if no journal exists yet."""
    journal = Path(
        os.environ.get("APEX_SHADOW_JOURNAL_PATH") or _SHADOW_JOURNAL
    )
    if not journal.exists():
        return None

    by_bucket: dict[str, dict[str, float]] = {}
    parsed = 0
    skipped = 0
    try:
        text = journal.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("shadow_tick: journal read failed -- %s", exc)
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        key = f"{row.get('strategy', '?')}::{row.get('regime', '?')}"
        b = by_bucket.setdefault(
            key,
            {"n": 0.0, "wins": 0.0, "cum_r": 0.0},
        )
        b["n"] += 1
        b["wins"] += 1.0 if row.get("is_win") else 0.0
        b["cum_r"] += float(row.get("pnl_r", 0.0) or 0.0)
        parsed += 1

    summary = {
        "parsed":       parsed,
        "skipped":      skipped,
        "buckets":      len(by_bucket),
        "by_bucket":    {
            k: {
                "n":        int(v["n"]),
                "win_rate": v["wins"] / v["n"] if v["n"] > 0 else 0.0,
                "cum_r":    v["cum_r"],
            }
            for k, v in by_bucket.items()
        },
    }
    return summary


# ---------------------------------------------------------------------------
# STRATEGY_MINE -- scan recent decision journals for strategy candidates.
# Returns counts only; the LLM-backed deep review still goes through
# Fleet.dispatch when the operator wants editorial output.
# ---------------------------------------------------------------------------

_DECISION_JOURNALS: tuple[Path, ...] = (
    _REPO_ROOT / "docs" / "btc_paper" / "btc_paper_journal.jsonl",
    _REPO_ROOT / "docs" / "btc_live" / "btc_live_decisions.jsonl",
)


def _strategy_mine_handler(_task: BackgroundTask) -> dict[str, Any] | None:
    """Tally strategy hits across recent decision journals. Returns a
    counts-only summary so the operator dashboard has a quick read on
    which strategies fired most this week."""
    counts: dict[str, int] = {}
    sources_seen: list[str] = []
    total_lines = 0
    for journal in _DECISION_JOURNALS:
        if not journal.exists():
            continue
        sources_seen.append(str(journal.relative_to(_REPO_ROOT)))
        try:
            text = journal.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_lines += 1
            strat = str(row.get("strategy") or row.get("setup") or "unknown")
            counts[strat] = counts.get(strat, 0) + 1

    if not sources_seen:
        return None

    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return {
        "sources":       sources_seen,
        "total_records": total_lines,
        "unique_strategies": len(counts),
        "top_10":        [{"strategy": s, "count": c} for s, c in top],
    }


# ---------------------------------------------------------------------------
# Dispatch table (consulted by daemon._run_local_background_task)
# ---------------------------------------------------------------------------
LOCAL_HANDLERS: dict[BackgroundTask, Callable[[BackgroundTask], dict | None]] = {
    BackgroundTask.DASHBOARD_ASSEMBLE: _dashboard_assemble_handler,
    BackgroundTask.LOG_COMPACT:        _log_compact_handler,
    BackgroundTask.PROMPT_WARMUP:      _prompt_warmup_handler,
    BackgroundTask.SHADOW_TICK:        _shadow_tick_handler,
    BackgroundTask.STRATEGY_MINE:      _strategy_mine_handler,
}


def run_local_background_task(task: BackgroundTask) -> dict[str, Any] | None:
    """Public entry: dispatch ``task`` through the local handler table.

    Returns the handler's summary dict, or ``None`` if no handler is
    registered for ``task`` OR the handler returned None (signalling
    "fall through to Fleet.dispatch").
    """
    handler = LOCAL_HANDLERS.get(task)
    if handler is None:
        return None
    try:
        return handler(task)
    except Exception as exc:  # noqa: BLE001 -- daemon must never crash
        logger.exception("local handler for %s raised: %s", task.value, exc)
        return None


__all__ = [
    "LOCAL_HANDLERS",
    "run_local_background_task",
]
