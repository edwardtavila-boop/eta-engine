"""
JARVIS v3 // attribution_cube (T12)

Multi-dimensional performance attribution. Joins the consult trace
stream with trade-close records and produces sliceable views by
(school × asset × hour_of_day × verdict).

Operator usage:

  "Which school is paying the bills in MNQ after 2pm?"
    → attribution_cube.query(slice_by=["school","asset","hour"],
                              filter={"asset":"MNQ", "hour_min":14})

  "Show me total R by school for the last 7 days."
    → attribution_cube.query(slice_by=["school"],
                              filter={"since_days_ago":7})

Implementation: pure dict aggregation over JSONL streams. No SQLite,
no pandas — small enough to fit a fleet's daily volume in memory.
Lazy-loads only the trace + trade_closes records the filter window
needs.

Public interface
----------------

* ``query(slice_by, filter)`` — main aggregation entry point.
* ``CubeQuery`` / ``CubeRow`` dataclasses (typed result).

NEVER raises. Empty / missing inputs → empty result.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.attribution_cube")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
DEFAULT_TRACE_PATH = _STATE_ROOT / "jarvis_trace.jsonl"
DEFAULT_TRADE_CLOSES_PATH = _STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"

EXPECTED_HOOKS = ("query",)

VALID_SLICE_DIMS = ("school", "asset", "hour", "verdict", "bot")


@dataclass(frozen=True)
class CubeRow:
    key: dict[str, Any]      # the slice values (e.g. {"school":"momentum","asset":"MNQ"})
    n_trades: int
    n_consults: int
    total_r: float
    avg_r: float
    win_rate: float          # fraction of trades with r>0
    max_r: float
    min_r: float


@dataclass(frozen=True)
class CubeQuery:
    slice_by: list[str]
    filter: dict[str, Any]
    rows: list[CubeRow]
    asof: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_by": self.slice_by,
            "filter": self.filter,
            "rows": [asdict(r) for r in self.rows],
            "asof": self.asof,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path, since_dt: datetime | None = None) -> list[dict[str, Any]]:
    """Read a JSONL file, optionally filtering by ts >= since_dt.

    NEVER raises; logs warnings and returns whatever was successfully
    read. ``ts`` parsing tolerates both naive and aware ISO strings.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_dt is not None:
                    ts = _parse_iso(rec.get("ts") or rec.get("closed_at"))
                    if ts is None or ts < since_dt:
                        continue
                out.append(rec)
    except OSError as exc:
        logger.warning("attribution_cube._read_jsonl failed: %s", exc)
    return out


def _parse_iso(s: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------


def _key_for(rec: dict[str, Any], dim: str) -> str:
    """Return the slice value for one dimension. Falls back to '?'
    when the field is absent. Hour is bucketed to integer 0-23 UTC.
    """
    if dim == "school":
        # The trace records "schools" as a dict; for attribution we expand each
        # trade across the schools that contributed. Caller is responsible for
        # iterating school keys. Here we just return the raw school name.
        return str(rec.get("_school", "?"))
    if dim == "asset":
        return str(rec.get("asset_class") or rec.get("asset") or "?")
    if dim == "verdict":
        v = rec.get("verdict")
        if isinstance(v, dict):
            return str(v.get("final_verdict", "?"))
        return str(rec.get("final_verdict", "?"))
    if dim == "hour":
        ts = _parse_iso(rec.get("ts") or rec.get("closed_at"))
        return str(ts.astimezone(UTC).hour) if ts else "?"
    if dim == "bot":
        return str(rec.get("bot_id", "?"))
    return "?"


def _expand_record_per_school(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """When slicing by school, one trade record splits into one row per
    school that contributed (with the school annotation attached).
    """
    schools_dict = rec.get("schools") or rec.get("school_inputs") or {}
    if not isinstance(schools_dict, dict) or not schools_dict:
        return [{**rec, "_school": "unknown"}]
    return [{**rec, "_school": s} for s in schools_dict]


def _normalize_records_for_slicing(
    records: list[dict[str, Any]], slice_by: list[str],
) -> list[dict[str, Any]]:
    if "school" in slice_by:
        out: list[dict[str, Any]] = []
        for r in records:
            out.extend(_expand_record_per_school(r))
        return out
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def query(
    slice_by: list[str] | None = None,
    filter: dict[str, Any] | None = None,  # noqa: A002 — intentional, mirror SQL
    *,
    trace_path: Path | None = None,
    trade_closes_path: Path | None = None,
) -> CubeQuery:
    """Aggregate trade closes (joined with consults) into a sliced view.

    Args:
      slice_by: subset of ``("school","asset","hour","verdict","bot")``.
                Unknown dims are dropped silently. Default: ``["bot"]``.
      filter: optional dict with keys:
        * ``asset``: keep records matching this asset_class
        * ``bot_id``: keep records for this bot
        * ``school``: keep only school rows matching (only meaningful with school slicing)
        * ``since_days_ago``: int — only records newer than N days
        * ``hour_min`` / ``hour_max``: int — hour-of-day window (UTC)

    Returns ``CubeQuery`` with one CubeRow per unique slice key.
    """
    slice_by = list(slice_by or ["bot"])
    slice_by = [d for d in slice_by if d in VALID_SLICE_DIMS]
    if not slice_by:
        slice_by = ["bot"]
    filter = dict(filter or {})

    since_dt: datetime | None = None
    if "since_days_ago" in filter:
        try:
            since_dt = datetime.now(UTC) - timedelta(days=int(filter["since_days_ago"]))
        except (TypeError, ValueError):
            since_dt = None

    trades = _read_jsonl(trade_closes_path or DEFAULT_TRADE_CLOSES_PATH, since_dt)

    # Optional filter passes
    if "asset" in filter and filter["asset"]:
        asset_norm = str(filter["asset"]).upper()
        trades = [
            t for t in trades
            if str(t.get("asset_class") or t.get("asset") or "").upper() == asset_norm
        ]
    if "bot_id" in filter and filter["bot_id"]:
        bot_norm = str(filter["bot_id"])
        trades = [t for t in trades if str(t.get("bot_id", "")) == bot_norm]
    if "hour_min" in filter or "hour_max" in filter:
        try:
            h_min = int(filter.get("hour_min", 0))
        except (TypeError, ValueError):
            h_min = 0
        try:
            h_max = int(filter.get("hour_max", 23))
        except (TypeError, ValueError):
            h_max = 23
        new_trades: list[dict[str, Any]] = []
        for t in trades:
            ts = _parse_iso(t.get("ts") or t.get("closed_at"))
            if ts is None:
                continue
            h = ts.astimezone(UTC).hour
            if h_min <= h <= h_max:
                new_trades.append(t)
        trades = new_trades

    # Expand per-school if needed
    rows_norm = _normalize_records_for_slicing(trades, slice_by)

    # School-specific filter (only after expansion)
    if "school" in filter and filter["school"]:
        school_norm = str(filter["school"])
        rows_norm = [r for r in rows_norm if r.get("_school") == school_norm]

    # Aggregate
    buckets: dict[tuple, dict[str, Any]] = defaultdict(lambda: {
        "n_trades": 0,
        "n_consults": 0,
        "total_r": 0.0,
        "wins": 0,
        "max_r": float("-inf"),
        "min_r": float("inf"),
        "consult_ids": set(),
    })
    for rec in rows_norm:
        key_tuple = tuple(_key_for(rec, d) for d in slice_by)
        b = buckets[key_tuple]
        b["n_trades"] += 1
        try:
            r = float(rec.get("r", rec.get("r_value", 0.0)))
        except (TypeError, ValueError):
            r = 0.0
        b["total_r"] += r
        if r > 0:
            b["wins"] += 1
        if r > b["max_r"]:
            b["max_r"] = r
        if r < b["min_r"]:
            b["min_r"] = r
        cid = rec.get("consult_id")
        if cid:
            b["consult_ids"].add(cid)

    cube_rows: list[CubeRow] = []
    for key_tuple, b in buckets.items():
        n_trades = b["n_trades"]
        cube_rows.append(CubeRow(
            key={d: key_tuple[i] for i, d in enumerate(slice_by)},
            n_trades=n_trades,
            n_consults=len(b["consult_ids"]),
            total_r=round(b["total_r"], 4),
            avg_r=round(b["total_r"] / n_trades, 4) if n_trades else 0.0,
            win_rate=round(b["wins"] / n_trades, 4) if n_trades else 0.0,
            max_r=round(b["max_r"], 4) if n_trades else 0.0,
            min_r=round(b["min_r"], 4) if n_trades else 0.0,
        ))

    # Stable sort: descending total_r so the operator sees winners first
    cube_rows.sort(key=lambda r: r.total_r, reverse=True)

    return CubeQuery(
        slice_by=slice_by,
        filter=filter,
        rows=cube_rows,
        asof=datetime.now(UTC).isoformat(),
    )
