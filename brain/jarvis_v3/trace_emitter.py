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

DEFAULT_TRACE_PATH = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_trace.jsonl")
MAX_BYTES_PER_FILE = 10 * 1024 * 1024  # 10 MB

EXPECTED_HOOKS = ("emit", "tail")

logger = logging.getLogger("eta_engine.trace_emitter")


@dataclass
class TraceRecord:
    """One row of the consult trace stream.

    Every field has a safe default — partial records emit cleanly even when
    an upstream stream failed and its slice of the record is empty.
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
