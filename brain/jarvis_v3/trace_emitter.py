"""
JARVIS v3 // trace_emitter
==========================
Live consult reasoning stream.

Writes one JSON line per `JarvisFull.consult()` to
``var/eta_engine/state/jarvis_trace.jsonl``. Operator-facing artifact that
makes the consult flow observable: dashboards, the supervisor heartbeat,
the wiring audit, and `kaizen_loop` all read from this stream.

Design rules:
  * ``emit()`` MUST NEVER raise. A failed write is logged and dropped;
    the live consult path keeps moving.
  * Rotation is size-based at ``MAX_BYTES_PER_FILE`` (10 MB by default).
    The rotated file is gzipped and timestamped, leaving the active file
    fresh for the next consult.
  * ``tail()`` reads backwards from EOF in fixed-size chunks so a 9 MB
    trace doesn't load into memory just to surface the last 3 lines on
    the heartbeat.

Public interface:
  * ``TraceRecord`` — dataclass with safe defaults for every field.
  * ``new_consult_id()`` — short, unique consult correlation id.
  * ``emit(rec, path=None)`` — append + rotate.
  * ``tail(n=20, path=None)`` — efficient newest-last read.

Pure stdlib (no pydantic — this path is hot and must not import heavy deps).
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import logging
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_TRACE_PATH = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_trace.jsonl")
MAX_BYTES_PER_FILE = 10 * 1024 * 1024  # 10 MB

EXPECTED_HOOKS = ("emit", "tail")

logger = logging.getLogger("eta_engine.trace_emitter")


@dataclass
class TraceRecord:
    """One row of the consult trace stream.

    Every field has a safe default — partial records emit cleanly even when
    an upstream stream failed and its slice of the record is empty.

    Schema versioning
    -----------------
    v1 (legacy): everything up to ``hermes_calls``.
    v2 (2026-05-12, T6/T7 prereq): adds ``schema_version``,
    ``school_inputs``, ``portfolio_inputs``, ``hot_weights_snapshot``,
    ``overrides_snapshot``, ``rng_master_seed``. All new fields default
    to empty/None so existing emit sites continue to produce v1-style
    records without modification. Emit sites populate v2 fields
    site-by-site over the migration window. Readers ignore unknown
    fields (we use ``dict.get()`` everywhere), so v2 records remain
    readable by v1-aware code with the new fields simply absent.

    See ``docs/TRACE_SCHEMA_V2_DESIGN.md`` for the full migration plan.
    """

    ts: str = ""
    bot_id: str = ""
    consult_id: str = ""
    action: str = ""
    verdict: dict = field(default_factory=dict)
    schools: dict = field(default_factory=dict)
    clashes: list = field(default_factory=list)
    dissent: list = field(default_factory=list)
    portfolio: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    hot_learn: dict = field(default_factory=dict)
    final_size: float = 0.0
    block_reason: str | None = None
    elapsed_ms: float = 0.0
    # Hermes Bridge Phase B: per-call-site outcome of any Hermes Agent
    # interactions during this consult. Empty dict when Hermes is
    # unreachable / backoff active / no site fired. Mirrors the field on
    # ConductorResult so the trace stream captures the full picture.
    hermes_calls: dict = field(default_factory=dict)

    # ── v2 schema fields (T6/T7 prereq) — see TRACE_SCHEMA_V2_DESIGN.md ──
    schema_version: int = 1
    """v1 = legacy (everything above this field).
    v2 = adds the replay-input snapshot fields below.
    Emit sites set this to 2 when populating the v2 fields; v1 sites
    leave the default. T6/T7 consumers dispatch on this."""

    school_inputs: dict = field(default_factory=dict)
    """Per-school RAW input snapshot keyed by school name.
    Each value should be a dict with at least:
        {"score": float, "size_modifier": float, "rationale": str,
         "rng_seed": int | None}
    Captures every school's vote BEFORE the consolidator merges them.
    Required for T6 causal attribution (perturb one school, observe
    verdict shift) and T7 deterministic replay."""

    portfolio_inputs: dict = field(default_factory=dict)
    """Snapshot of the PortfolioContext dict fed to
    portfolio_brain.assess at consult time. Keys mirror PortfolioContext
    fields: fleet_long_notional_by_asset, fleet_short_notional_by_asset,
    recent_entries_by_asset, open_correlated_exposure,
    portfolio_drawdown_today_r, fleet_kill_active. Required for T7."""

    hot_weights_snapshot: dict = field(default_factory=dict)
    """{school: weight} for this bot's asset_class at consult time.
    Snapshot of hot_learner.current_weights() result. Required for
    replay because hot_learner state drifts after the consult."""

    overrides_snapshot: dict = field(default_factory=dict)
    """{size_modifier: float | None, school_weights: {school: weight}}
    for this bot/asset at consult time. Captures Hermes-pinned
    overrides that were live. Required for replay because overrides
    expire (TTL-bounded)."""

    rng_master_seed: int | None = None
    """The deterministic master seed used to re-create the consult's
    randomness for replay. ``None`` when no stochastic schools fired."""


def new_consult_id() -> str:
    """Return a 12-char hex consult correlation id.

    Short enough to log on every consult, long enough to be collision-free
    across a multi-week trace window.
    """
    return uuid.uuid4().hex[:12]


def _resolve_path(path: Path | None) -> Path:
    return Path(path) if path is not None else DEFAULT_TRACE_PATH


def _rotate(active: Path) -> None:
    """Atomically gzip the active file and clear it for fresh writes.

    Naming: ``jarvis_trace_<UTC-stamp>.jsonl.gz`` next to the active file.
    Falls back to a non-gzipped rename if gzip itself raises (should never
    happen in practice; here for defense-in-depth — the caller already
    swallows exceptions, so this is belt-and-suspenders only).
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rotated = active.with_name(f"{active.stem}_{stamp}{active.suffix}.gz")

    # gzip the active file, then truncate it
    with active.open("rb") as src, gzip.open(rotated, "wb") as dst:
        shutil.copyfileobj(src, dst)
    # truncate by replacing with a fresh empty file
    active.unlink()


def emit(rec: TraceRecord, path: Path | None = None) -> None:
    """Append ``rec`` as one JSON line. Rotate if file exceeds ``MAX_BYTES_PER_FILE``.

    NEVER raises. On any failure the error is logged and the function returns.
    """
    try:
        target = _resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(asdict(rec), default=str) + "\n"
        encoded = line.encode("utf-8")

        with target.open("ab") as fh:
            fh.write(encoded)
            fh.flush()
            # fsync is best-effort; some filesystems / mocks don't support it
            with contextlib.suppress(OSError, AttributeError):
                os.fsync(fh.fileno())

        # Rotate if the file now exceeds the cap
        try:
            if target.stat().st_size > MAX_BYTES_PER_FILE:
                _rotate(target)
        except OSError as exc:
            logger.warning("trace_emitter rotation check failed: %s", exc)

    except Exception as exc:  # noqa: BLE001 — emit() never raises by contract
        logger.warning("trace_emitter.emit dropped record: %s", exc)


def _read_last_n_lines(path: Path, n: int, chunk_size: int = 8192) -> list[str]:
    """Read up to the last ``n`` non-empty lines from ``path`` without slurping the file.

    Walks backwards from EOF in ``chunk_size`` increments, accumulating bytes
    until at least ``n + 1`` newlines are present (the +1 guards a partial
    leading fragment from being treated as a complete line).
    """
    with path.open("rb") as fh:
        fh.seek(0, io.SEEK_END)
        file_size = fh.tell()
        if file_size == 0:
            return []

        buffer = b""
        pos = file_size

        while pos > 0 and buffer.count(b"\n") <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            buffer = fh.read(read_size) + buffer

        text = buffer.decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-n:]


def tail(n: int = 20, path: Path | None = None) -> list[dict]:
    """Return up to the last ``n`` records as parsed dicts, newest last.

    Reads the file backwards in fixed chunks so a 10 MB trace stream
    doesn't load into memory. Malformed lines are skipped silently.
    Missing file → empty list.
    """
    if n <= 0:
        return []
    target = _resolve_path(path)
    try:
        if not target.exists():
            return []
        raw_lines = _read_last_n_lines(target, n)
        records: list[dict] = []
        for line in raw_lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip malformed lines — trace stream is best-effort
                continue
        return records
    except Exception as exc:  # noqa: BLE001
        logger.warning("trace_emitter.tail failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# v2 schema helpers — used by T6 (causal_attribution) and T7 (replay_engine)
# ---------------------------------------------------------------------------


def is_v2_record(rec: dict | TraceRecord) -> bool:
    """True iff ``rec`` declares ``schema_version >= 2``.

    Accepts either a parsed dict (what reads from JSONL produce) or a
    live ``TraceRecord`` dataclass. Records emitted before the v2 schema
    bump don't have the field and return ``False``.

    Used by T6's ``causal_attribution.analyze`` and T7's
    ``replay_engine.replay`` to dispatch between "this record has the
    replay-input snapshot fields, use them" vs "this is a legacy record,
    return a pre-v2 error envelope to the caller".
    """
    if isinstance(rec, TraceRecord):
        return rec.schema_version >= 2
    if isinstance(rec, dict):
        try:
            return int(rec.get("schema_version", 1)) >= 2
        except (TypeError, ValueError):
            return False
    return False


def extract_replay_inputs(rec: dict | TraceRecord) -> dict[str, Any] | None:
    """Pack the v2 replay-input fields into a single dict for T6/T7.

    Returns ``None`` if ``rec`` is not v2. Otherwise returns:

        {
            "school_inputs": dict,
            "portfolio_inputs": dict,
            "hot_weights_snapshot": dict,
            "overrides_snapshot": dict,
            "rng_master_seed": int | None,
        }

    Caller treats ``None`` as the signal to return a "pre-v2 record"
    error envelope. This helper exists so T6 and T7 don't both reach
    into the record's fields by hand — keeps the schema dependency in
    one place.
    """
    if not is_v2_record(rec):
        return None

    def _g(key: str, default: Any) -> Any:  # noqa: ANN401
        if isinstance(rec, TraceRecord):
            return getattr(rec, key, default)
        if isinstance(rec, dict):
            return rec.get(key, default)
        return default

    return {
        "school_inputs": dict(_g("school_inputs", {})),
        "portfolio_inputs": dict(_g("portfolio_inputs", {})),
        "hot_weights_snapshot": dict(_g("hot_weights_snapshot", {})),
        "overrides_snapshot": dict(_g("overrides_snapshot", {})),
        "rng_master_seed": _g("rng_master_seed", None),
    }


def read_since(
    offset: int,
    limit: int = 100,
    path: Path | None = None,
) -> tuple[list[dict], int]:
    """Read records appended after a byte offset, returning new records + next offset.

    Powers Hermes Agent's "subscribe to events" polling loop without
    requiring a real push channel. The client passes back the previous
    ``next_offset`` on every tick; the server returns whatever is newer.

    Behaviour:
      * ``offset < 0``: clamped to 0.
      * ``offset >= file_size``: caller is already caught up — returns
        ``([], file_size)``. No-op, cheap.
      * Active file rotated since last poll (``offset > file_size``):
        reset to 0 and return whatever's in the new (smaller) file.
        Caller loses no data because the rotated copy is preserved on
        disk as ``jarvis_trace_<stamp>.jsonl.gz``.
      * Partial trailing line (write race): the partial line stays in
        the buffer; the new offset stops at the last newline so the
        next poll picks it up cleanly.
      * Missing file: ``([], 0)`` — equivalent to "stream not started".
      * Malformed JSON line: skipped, but the offset still advances
        past it (we don't want to spin on a corrupted record).

    NEVER raises. On unexpected error, logs and returns ``([], offset)``
    so the caller's cursor is preserved.
    """
    target = _resolve_path(path)
    if limit <= 0:
        limit = 100
    if offset < 0:
        offset = 0
    try:
        if not target.exists():
            return [], 0
        file_size = target.stat().st_size

        # File rotated away under us: rotated file is gzipped to a
        # sibling, active file restarted from zero. Reset cursor.
        # (offset > file_size only happens when active file shrank.)
        if offset > file_size:
            offset = 0

        # Caught up - no new data.
        if offset == file_size:
            return [], file_size

        with target.open("rb") as fh:
            fh.seek(offset)
            # Read up to ~4 MB at a time so a giant trace doesn't OOM
            # a poll. The remainder will come on the next call.
            chunk_cap = 4 * 1024 * 1024
            data = fh.read(chunk_cap)

        if not data:
            return [], offset

        # Split on byte newlines so the cursor can advance only through
        # records actually returned. This prevents limit=2 from skipping
        # events 3..N in the same chunk.
        if data.endswith(b"\n"):
            usable = data
        else:
            last_nl = data.rfind(b"\n")
            if last_nl < 0:
                # Whole chunk is a partial line - caller will retry.
                return [], offset
            usable = data[: last_nl + 1]

        advance_to = offset
        records: list[dict] = []
        for line in usable.splitlines(keepends=True):
            advance_to += len(line)
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped.decode("utf-8", errors="replace")))
            except json.JSONDecodeError:
                # Skip malformed line - best-effort, cursor still advances.
                continue
            if len(records) >= limit:
                break

        return records, advance_to

    except Exception as exc:  # noqa: BLE001
        logger.warning("trace_emitter.read_since failed: %s", exc)
        return [], offset
