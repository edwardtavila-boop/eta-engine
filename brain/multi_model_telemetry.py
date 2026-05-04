"""Persistent telemetry for the Force-Multiplier orchestrator.

Every ``route_and_execute`` call (and each stage of ``force_multiplier_chain``)
appends a structured JSONL record to ``state/force_multiplier_calls.jsonl``.
This is the canonical audit log for what the orchestrator did, why, and
what it cost.

Why a JSONL file
================
- Append-only is crash-safe: a partial write at most loses the last line
- ``jq`` / ``pandas.read_json(lines=True)`` ingest it directly
- No DB dependency; rotates trivially with logrotate or a cron job

Schema
======
Each line is a single JSON object with these fields::

    {
      "ts": "2026-05-04T19:30:00.123456+00:00",   # ISO-8601 UTC
      "kind": "route" | "chain_stage",            # call type
      "category": "architecture_decision",        # TaskCategory.value
      "preferred_provider": "claude",             # what policy said
      "actual_provider": "claude",                # what ran (= preferred unless fallback)
      "tier": "opus",                             # ModelTier.value
      "model": "opus" | "deepseek-v4-flash" | ...,
      "input_tokens": 18,
      "output_tokens": 24,
      "cost_usd": 0.000009,
      "elapsed_ms": 850,
      "fallback_used": false,
      "fallback_reason": "",
      "stage": "plan" | "implement" | "verify" | null,  # only when kind="chain_stage"
      "chain_id": "0192-...",                     # links chain stages to one run
    }

Reading the log
===============
::

    jq -c '.actual_provider' state/force_multiplier_calls.jsonl | sort | uniq -c
    # how many calls per provider

    jq -s 'map(.cost_usd) | add' state/force_multiplier_calls.jsonl
    # total spend

    jq -c 'select(.fallback_used)' state/force_multiplier_calls.jsonl
    # every call that fell back, with reason

Disabling
=========
Set ``ETA_FM_TELEMETRY=0`` to disable writes (e.g. in unit tests that
shouldn't pollute the log).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default location: <eta_engine>/state/force_multiplier_calls.jsonl
_DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "state" / "force_multiplier_calls.jsonl"
_LOG_LOCK = threading.Lock()


def _resolve_log_path() -> Path:
    """Allow override via ``ETA_FM_TELEMETRY_LOG`` for tests / VPS deployments."""
    explicit = os.environ.get("ETA_FM_TELEMETRY_LOG", "").strip()
    return Path(explicit) if explicit else _DEFAULT_LOG_PATH


def telemetry_enabled() -> bool:
    """``ETA_FM_TELEMETRY=0`` disables writes. Default ON."""
    return os.environ.get("ETA_FM_TELEMETRY", "1").strip() != "0"


def new_chain_id() -> str:
    """Create a chain correlation id (UUIDv7-ish: time-prefixed for sortability)."""
    return f"{int(datetime.now(tz=UTC).timestamp() * 1000):013d}-{uuid.uuid4().hex[:12]}"


def log_call(*, record: dict[str, Any]) -> None:
    """Append one record to the FM telemetry log. Best-effort; never raises.

    The orchestrator calls this at the END of every routed call so a crash
    mid-call doesn't write a half-baked record.
    """
    if not telemetry_enabled():
        return

    record = dict(record)  # shallow copy so caller can reuse
    record.setdefault("ts", datetime.now(tz=UTC).isoformat())

    path = _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        # Telemetry must never crash the calling code.
        logger.warning("FM telemetry write failed (path=%s): %s", path, exc)


def read_recent(*, limit: int = 100) -> list[dict[str, Any]]:
    """Tail the log for dashboards / cost reports. Best-effort; missing log = []."""
    path = _resolve_log_path()
    if not path.is_file():
        return []
    try:
        # Read the whole file then take last `limit` lines. The log is small
        # enough (one line per LLM call) that this is fine in practice.
        with path.open(encoding="utf-8") as fh:
            lines = fh.readlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip malformed line, keep going
        return out
    except OSError as exc:
        logger.warning("FM telemetry read failed (path=%s): %s", path, exc)
        return []


def summarize(*, limit: int = 1000) -> dict[str, Any]:
    """Aggregate the last ``limit`` calls for quick reporting.

    Returns counts, total cost, fallback rate, and per-provider breakdown.
    Suitable for piping into a CLI ``--summary`` flag or a dashboard widget.
    """
    records = read_recent(limit=limit)
    if not records:
        return {"calls": 0, "total_cost_usd": 0.0, "by_provider": {}}

    total_cost = sum(float(r.get("cost_usd") or 0) for r in records)
    fallbacks = sum(1 for r in records if r.get("fallback_used"))

    by_provider: dict[str, dict[str, Any]] = {}
    for r in records:
        prov = r.get("actual_provider") or "unknown"
        slot = by_provider.setdefault(prov, {"calls": 0, "cost_usd": 0.0, "fallbacks_received": 0})
        slot["calls"] += 1
        slot["cost_usd"] += float(r.get("cost_usd") or 0)
        if r.get("fallback_used"):
            slot["fallbacks_received"] += 1

    return {
        "calls": len(records),
        "total_cost_usd": round(total_cost, 6),
        "fallback_count": fallbacks,
        "fallback_rate": round(fallbacks / len(records), 3) if records else 0.0,
        "by_provider": {p: {**s, "cost_usd": round(s["cost_usd"], 6)} for p, s in by_provider.items()},
        "first_ts": records[0].get("ts"),
        "last_ts": records[-1].get("ts"),
    }
