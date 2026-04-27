"""
JARVIS v3 // nl_query
=====================
Natural-language audit query.

Operator asks: ``"why did you deny request abc123?"`` or
``"show me every DENIED request from bot.mnq in the last 3 hours"``.

This is NOT a free-form LLM -- it's a deterministic grammar of seven
query intents over the JSONL audit log. Each intent returns a
``QueryResult`` with a structured answer + supporting records.

Intents:
  1. WHY_VERDICT     -- "why did you <verdict> request <id>?"
  2. COUNT_VERDICT   -- "how many <verdict> in last <window>?"
  3. LIST_VERDICT    -- "list <verdict> from <subsystem> in last <window>"
  4. REASON_FREQ     -- "what are the most common reason_codes today?"
  5. SUBSYSTEM_STATS -- "how is <subsystem> doing?"
  6. LAST_BINDING    -- "what's been the binding constraint lately?"
  7. HEALTH          -- "are you healthy?"

A tiny regex-based dispatcher maps free-text to an intent. Callers can
bypass parsing by calling the intent functions directly.

Stdlib only.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class QueryResult(BaseModel):
    """Structured answer to an NL query."""

    model_config = ConfigDict(frozen=True)

    intent: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    records: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Log loader
# ---------------------------------------------------------------------------


def _load_records(audit_path: Path | str) -> list[dict[str, Any]]:
    p = Path(audit_path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _parse_ts(r: dict[str, Any]) -> datetime | None:
    ts = r.get("ts")
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=UTC)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _filter_window(
    records: list[dict[str, Any]],
    *,
    hours: float | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if hours is None:
        return records
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    out: list[dict[str, Any]] = []
    for r in records:
        ts = _parse_ts(r)
        if ts is None or ts >= cutoff:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------


def why_verdict(audit_path: Path | str, request_id: str) -> QueryResult:
    for r in _load_records(audit_path):
        if r.get("request_id") == request_id:
            return QueryResult(
                intent="WHY_VERDICT",
                summary=(
                    f"{r.get('verdict', '?')} -- {r.get('reason', 'no reason logged')} "
                    f"(reason_code={r.get('reason_code', '?')})"
                ),
                records=[r],
                stats={"stress_composite": float(r.get("stress_composite", 0.0))},
            )
    return QueryResult(
        intent="WHY_VERDICT",
        summary=f"no record found for request_id={request_id}",
    )


def count_verdict(
    audit_path: Path | str,
    verdict: str,
    hours: float = 24.0,
) -> QueryResult:
    recs = _filter_window(_load_records(audit_path), hours=hours)
    vnorm = verdict.upper()
    matches = [r for r in recs if str(r.get("verdict", "")).upper() == vnorm]
    return QueryResult(
        intent="COUNT_VERDICT",
        summary=f"{len(matches)} {vnorm} in last {hours}h (of {len(recs)} total)",
        stats={"count": len(matches), "total": len(recs)},
    )


def list_verdict(
    audit_path: Path | str,
    verdict: str,
    subsystem: str | None = None,
    hours: float = 24.0,
    limit: int = 20,
) -> QueryResult:
    recs = _filter_window(_load_records(audit_path), hours=hours)
    vnorm = verdict.upper()
    out: list[dict[str, Any]] = []
    for r in recs:
        if str(r.get("verdict", "")).upper() != vnorm:
            continue
        if subsystem and r.get("subsystem") != subsystem:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return QueryResult(
        intent="LIST_VERDICT",
        summary=f"showing {len(out)} {vnorm} records (subsystem={subsystem or 'any'})",
        records=out,
    )


def reason_freq(
    audit_path: Path | str,
    hours: float = 24.0,
    top: int = 10,
) -> QueryResult:
    recs = _filter_window(_load_records(audit_path), hours=hours)
    counts = Counter(r.get("reason_code", "unknown") for r in recs)
    top_items = counts.most_common(top)
    summary = ", ".join(f"{k}={v}" for k, v in top_items[:5]) or "no records"
    return QueryResult(
        intent="REASON_FREQ",
        summary=f"top reasons (last {hours}h): {summary}",
        records=[{"reason_code": k, "count": v} for k, v in top_items],
    )


def subsystem_stats(audit_path: Path | str, subsystem: str, hours: float = 24.0) -> QueryResult:
    recs = [r for r in _filter_window(_load_records(audit_path), hours=hours) if r.get("subsystem") == subsystem]
    n = len(recs)
    by_verdict: Counter[str] = Counter(str(r.get("verdict", "")).upper() for r in recs)
    return QueryResult(
        intent="SUBSYSTEM_STATS",
        summary=(f"{subsystem}: {n} requests in last {hours}h  ({dict(by_verdict)})"),
        stats={k: float(v) for k, v in by_verdict.items()},
    )


def last_binding(audit_path: Path | str, hours: float = 6.0) -> QueryResult:
    recs = _filter_window(_load_records(audit_path), hours=hours)
    bindings = [r.get("binding_constraint", "none") for r in recs if r.get("binding_constraint")]
    counts = Counter(bindings)
    top = counts.most_common(5)
    summary = ", ".join(f"{k}={v}" for k, v in top) or "no binding data"
    return QueryResult(
        intent="LAST_BINDING",
        summary=f"binding constraints (last {hours}h): {summary}",
        records=[{"binding_constraint": k, "count": v} for k, v in top],
    )


def health(audit_path: Path | str) -> QueryResult:
    recs = _load_records(audit_path)
    n = len(recs)
    if n == 0:
        return QueryResult(
            intent="HEALTH",
            summary="no audit records -- JARVIS has not decided anything yet",
        )
    last = recs[-1]
    return QueryResult(
        intent="HEALTH",
        summary=f"{n} records total; last {last.get('verdict', '?')} at {last.get('ts', '?')}",
        stats={"count": n},
    )


# ---------------------------------------------------------------------------
# Free-text dispatcher (grammar-bounded, not an LLM)
# ---------------------------------------------------------------------------

_RE_WHY = re.compile(
    r"why.*(?:deny|denied|approve|approved|defer|deferred|conditional)"
    r".*(?:request|id)\s*[=:]?\s*([a-z0-9]{6,})",
    re.IGNORECASE,
)
_RE_COUNT = re.compile(
    r"how many\s+(APPROVED|DENIED|CONDITIONAL|DEFERRED)\s*(?:in last\s*(\d+)\s*h)?",
    re.IGNORECASE,
)
_RE_LIST = re.compile(
    r"list\s+(APPROVED|DENIED|CONDITIONAL|DEFERRED)"
    r"(?:\s+from\s+([a-z0-9._-]+))?"
    r"(?:\s+in last\s*(\d+)\s*h)?",
    re.IGNORECASE,
)
_RE_REASONS = re.compile(r"(top|most common|frequent)\s+reasons?", re.IGNORECASE)
_RE_SUBSYS = re.compile(r"how is\s+([a-z0-9._-]+)\s+doing", re.IGNORECASE)
_RE_BINDING = re.compile(r"binding\s+constraint", re.IGNORECASE)
_RE_HEALTH = re.compile(r"(healthy|health|are you ok)", re.IGNORECASE)


def dispatch(audit_path: Path | str, query: str) -> QueryResult:
    """Parse free text and call the right intent. Falls back to HEALTH."""
    q = query.strip()
    if not q:
        return health(audit_path)
    if m := _RE_WHY.search(q):
        return why_verdict(audit_path, m.group(1))
    if m := _RE_LIST.search(q):
        verdict = m.group(1)
        subsystem = m.group(2)
        hours = float(m.group(3)) if m.group(3) else 24.0
        return list_verdict(audit_path, verdict, subsystem, hours=hours)
    if m := _RE_COUNT.search(q):
        verdict = m.group(1)
        hours = float(m.group(2)) if m.group(2) else 24.0
        return count_verdict(audit_path, verdict, hours=hours)
    if _RE_REASONS.search(q):
        return reason_freq(audit_path)
    if m := _RE_SUBSYS.search(q):
        return subsystem_stats(audit_path, m.group(1))
    if _RE_BINDING.search(q):
        return last_binding(audit_path)
    if _RE_HEALTH.search(q):
        return health(audit_path)
    # Default: health ping so we never silently confuse the operator.
    return QueryResult(
        intent="UNPARSED",
        summary="couldn't parse query; intents: WHY_VERDICT, COUNT_VERDICT, "
        "LIST_VERDICT, REASON_FREQ, SUBSYSTEM_STATS, LAST_BINDING, HEALTH",
    )
