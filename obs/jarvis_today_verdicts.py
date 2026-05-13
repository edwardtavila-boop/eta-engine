"""Today's JARVIS verdicts -- aggregator for the dashboard panel
(Tier-2 #6, 2026-04-27).

Reads JARVIS audit JSONL files and aggregates today's records into the
shape the dashboard frontend expects:

  * by-bot verdict counts
  * top denial / deferral reasons
  * average size_cap_mult for CONDITIONAL verdicts
  * hourly verdict timeline (for the panel sparkline)

Exposed as a stand-alone function ``aggregate_today()`` that the
``trading-dashboard/backend`` FastAPI app imports + serves at
``/api/jarvis/today_verdicts``.

Decoupled from the FastAPI layer so it's also unit-testable without
spinning up the dashboard.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_audit_records(audit_globs: list[str], *, since: datetime) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for glob in audit_globs:
        gp = Path(glob)
        files = [gp] if gp.is_file() else list(Path(gp.parent).glob(gp.name))
        for f in files:
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = rec.get("ts")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    if ts < since:
                        continue
                    rec["_ts_parsed"] = ts
                    records.append(rec)
            except OSError as exc:
                logger.warning("can't read %s: %s", f, exc)
    return records


def aggregate_today(
    *,
    audit_globs: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the dashboard-panel payload for today's JARVIS verdicts.

    Returns a dict shaped::

        {
          "ts": "2026-04-27T...",
          "window_hours": 24,
          "totals": {"APPROVED": 152, "CONDITIONAL": 14, "DENIED": 6, "DEFERRED": 2},
          "by_subsystem": {"bot.mnq": {"APPROVED": 40, ...}, ...},
          "top_denial_reasons": [["high_vol_throttle", 4], ...],
          "avg_conditional_cap": 0.72,
          "hourly_timeline": [{"hr": "13", "approved": 20, "rejected": 3}, ...],
          "policy_versions_seen": [0, 17],
        }
    """
    now = now or datetime.now(UTC)
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    audit_globs = audit_globs or [
        str(Path(__file__).resolve().parents[1] / "state" / "jarvis_audit" / "*.jsonl"),
        str(Path(__file__).resolve().parents[1] / "var" / "jarvis_audit" / "*.jsonl"),
    ]

    records = _load_audit_records(audit_globs, since=since)
    totals: Counter[str] = Counter()
    by_subsystem: dict[str, Counter[str]] = defaultdict(Counter)
    denial_reasons: Counter[str] = Counter()
    cond_caps: list[float] = []
    hourly: dict[int, Counter[str]] = defaultdict(Counter)
    policy_versions_seen: set[int] = set()

    for rec in records:
        resp = rec.get("response", {}) or {}
        verdict = resp.get("verdict", "UNKNOWN")
        subsystem = (rec.get("request", {}) or {}).get("subsystem", "unknown")
        reason_code = resp.get("reason_code", "")
        ts: datetime = rec["_ts_parsed"]
        pv = rec.get("policy_version", 0)

        totals[verdict] += 1
        by_subsystem[subsystem][verdict] += 1
        if verdict in ("DENIED", "DEFERRED") and reason_code:
            denial_reasons[reason_code] += 1
        if verdict == "CONDITIONAL":
            cap = resp.get("size_cap_mult")
            if isinstance(cap, (int, float)):
                cond_caps.append(float(cap))
        hr = ts.hour
        if verdict in ("DENIED", "DEFERRED"):
            hourly[hr]["rejected"] += 1
        elif verdict == "APPROVED":
            hourly[hr]["approved"] += 1
        elif verdict == "CONDITIONAL":
            hourly[hr]["conditional"] += 1
        with suppress(TypeError, ValueError):
            policy_versions_seen.add(int(pv))

    # Sage modulation stats
    sage_loosened = 0
    sage_tightened = 0
    sage_deferred = 0
    sage_convictions: list[float] = []
    sage_alignments: list[float] = []
    for rec in records:
        resp = rec.get("response", {}) or {}
        sage_mod = resp.get("sage_modulation", "")
        sage_conv = resp.get("sage_conviction")
        sage_align = resp.get("sage_alignment")
        if sage_mod == "loosened":
            sage_loosened += 1
        elif sage_mod in ("tightened", "deferred"):
            sage_tightened += 1
            if sage_mod == "deferred":
                sage_deferred += 1
        if isinstance(sage_conv, (int, float)):
            sage_convictions.append(float(sage_conv))
        if isinstance(sage_align, (int, float)):
            sage_alignments.append(float(sage_align))

    avg_cap = sum(cond_caps) / len(cond_caps) if cond_caps else 1.0

    return {
        "ts": now.isoformat(),
        "window_hours": (now - since).total_seconds() / 3600.0,
        "totals": dict(totals),
        "by_subsystem": {k: dict(v) for k, v in by_subsystem.items()},
        "top_denial_reasons": denial_reasons.most_common(10),
        "avg_conditional_cap": round(avg_cap, 4),
        "hourly_timeline": [{"hr": str(h).zfill(2), **dict(hourly[h])} for h in sorted(hourly.keys())],
        "policy_versions_seen": sorted(policy_versions_seen),
        "sage": {
            "n_loosened": sage_loosened,
            "n_tightened": sage_tightened,
            "n_deferred": sage_deferred,
            "avg_conviction": (round(sum(sage_convictions) / len(sage_convictions), 4) if sage_convictions else 0.0),
            "avg_alignment": (round(sum(sage_alignments) / len(sage_alignments), 4) if sage_alignments else 0.5),
        },
    }
