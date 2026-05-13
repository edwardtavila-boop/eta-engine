"""
Multi-Model Telemetry — call logging, chain tracking, and cost analytics.

Provides ``log_call()`` and ``new_chain_id()`` for the multi_model orchestrator
to track every LLM invocation across all three providers.

Schema (one JSON object per line in the log file)::

    {
      "ts": "2026-05-04T19:30:00.123456+00:00",
      "chain_id": "CHN-abc12345",
      "call_id":  "CAL-def123",
      "category": "architecture_decision",
      "provider": "claude",         # actual_provider after fallback
      "model":    "opus",
      "tier":     "opus",
      "elapsed_ms":  850,
      "input_tokens":  18,
      "output_tokens": 24,
      "cost_usd": 0.000009,
      "fallback_used":   false,
      "fallback_reason": "",
      "text_preview":    "first 200 chars of response..."
    }

Reading
=======
``read_telemetry(limit=N)`` returns up to N records from the END of the
log. ``summarize(limit=N)`` aggregates per-provider/per-category spend
over the last N records.

Disabling
=========
Set ``ETA_FM_TELEMETRY=0`` to skip writes (used in tests that shouldn't
pollute the log). Set ``ETA_FM_TELEMETRY_LOG=/path/to/file.jsonl`` to
override the log path (used by isolated unit tests via ``tmp_path``).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Default location: <workspace>/var/eta_engine/state/multi_model_telemetry.jsonl.
# Resolved lazily so ETA_FM_TELEMETRY_LOG can override at runtime.
_DEFAULT_TELEMETRY_PATH = (
    Path(__file__).resolve().parents[2] / "var" / "eta_engine" / "state" / "multi_model_telemetry.jsonl"
)
_LOG_LOCK = threading.Lock()


def _resolve_log_path() -> Path:
    """Override hook for tests + alternate VPS deployments.

    Reads ``ETA_FM_TELEMETRY_LOG`` each call so a test's ``monkeypatch.setenv``
    is picked up immediately.
    """
    explicit = os.environ.get("ETA_FM_TELEMETRY_LOG", "").strip()
    return Path(explicit) if explicit else _DEFAULT_TELEMETRY_PATH


# Back-compat alias: callers that imported ``TELEMETRY_PATH`` directly
# still work, but the ``_resolve_log_path()`` accessor is preferred for
# anything that needs to honor the env-var override at call time.
TELEMETRY_PATH = _DEFAULT_TELEMETRY_PATH


def telemetry_enabled() -> bool:
    """``ETA_FM_TELEMETRY=0`` disables writes. Default ON."""
    return os.environ.get("ETA_FM_TELEMETRY", "1").strip() != "0"


@dataclass
class CallRecord:
    chain_id: str
    call_id: str
    category: str
    provider: str
    model: str
    tier: str
    elapsed_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    fallback_used: bool
    fallback_reason: str
    text_preview: str
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def new_chain_id() -> str:
    return f"CHN-{uuid.uuid4().hex[:8]}"


def log_call(
    *,
    record: dict[str, Any] | None = None,
    chain_id: str = "",
    category: str = "",
    provider: str = "",
    model: str = "",
    tier: str = "",
    elapsed_ms: float = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0,
    fallback_used: bool = False,
    fallback_reason: str = "",
    text_preview: str = "",
) -> str:
    """Append one record to the telemetry log. Two calling conventions:

    1. Keyword form (typed): ``log_call(chain_id=..., category=..., ...)``
       — the canonical shape. Each field maps to a CallRecord attribute.

    2. ``record=`` form (dict): ``log_call(record={"category": ..., ...})``
       — back-compat for orchestrator callers that build the payload via
       a separate ``_telemetry_record()`` helper. Unknown keys are dropped
       to keep the dataclass shape pure.

    Returns the call_id of the appended record (always emitted, even
    when ``ETA_FM_TELEMETRY=0`` — caller can correlate even with logging
    suppressed).
    """
    call_id = f"CAL-{uuid.uuid4().hex[:6]}"

    if record is not None:
        # Pull the keyword fields from the dict; ignore unknowns so future
        # schema additions don't break old call sites.
        get = record.get
        # 'preferred_provider' is informational; we log the actual provider.
        provider = get("actual_provider") or get("provider") or provider
        chain_id = get("chain_id") or chain_id
        category = get("category") or category
        model = get("model") or model
        tier = get("tier") or tier
        elapsed_ms = float(get("elapsed_ms") or elapsed_ms or 0)
        input_tokens = int(get("input_tokens") or input_tokens or 0)
        output_tokens = int(get("output_tokens") or output_tokens or 0)
        cost_usd = float(get("cost_usd") or cost_usd or 0)
        fallback_used = bool(get("fallback_used") or fallback_used)
        fallback_reason = get("fallback_reason") or fallback_reason
        text_preview = (get("text_preview") or text_preview or "")[:200]

    cr = CallRecord(
        chain_id=chain_id or new_chain_id(),
        call_id=call_id,
        category=category,
        provider=provider,
        model=model,
        tier=tier or "",
        elapsed_ms=elapsed_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        text_preview=text_preview[:200],
    )

    if not telemetry_enabled():
        return call_id

    path = _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(cr), default=str) + "\n")
    except OSError:
        # Telemetry must never crash the calling code.
        pass

    return call_id


def read_telemetry(limit: int = 100, *, newest_first: bool = False) -> list[dict[str, Any]]:
    """Return up to ``limit`` records from the END of the log.

    The log is JSONL. Malformed lines are skipped silently. Default
    order is chronological (oldest first within the returned slice);
    pass ``newest_first=True`` to reverse.
    """
    path = _resolve_log_path()
    if not path.is_file():
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if newest_first:
        out.reverse()
    return out


def read_recent(*, limit: int = 100) -> list[dict[str, Any]]:
    """Tail the telemetry log. Returns ``limit`` most recent records,
    chronologically ordered (oldest first within the returned slice).

    Alias for ``read_telemetry`` with the default ordering — kept for
    callers that prefer the descriptive name (CLI, cost report).
    """
    return read_telemetry(limit=limit, newest_first=False)


def summarize(*, limit: int = 1000) -> dict[str, Any]:
    """Aggregate the last ``limit`` records for dashboards / cost reports.

    Returns counts, total cost, fallback rate, per-provider breakdown,
    and the timestamps of the first/last records included. Suitable for
    the FM ``status`` CLI subcommand and the Prometheus exporter.
    """
    records = read_telemetry(limit=limit)
    if not records:
        return {
            "calls": 0,
            "total_cost_usd": 0.0,
            "fallback_count": 0,
            "fallback_rate": 0.0,
            "by_provider": {},
            "first_ts": None,
            "last_ts": None,
        }

    total_cost = sum(float(r.get("cost_usd") or 0) for r in records)
    fallbacks = sum(1 for r in records if r.get("fallback_used"))

    by_provider: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cost_usd": 0.0, "fallbacks_received": 0},
    )
    for r in records:
        prov = r.get("provider") or r.get("actual_provider") or "unknown"
        slot = by_provider[prov]
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
