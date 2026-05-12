"""Tests for trace_emitter — live JARVIS consult reasoning stream (Stream 2)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def test_emit_writes_one_jsonl_line(tmp_path: Path) -> None:
    """emit one record, file has exactly 1 line, parses as JSON."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "jarvis_trace.jsonl"
    rec = trace_emitter.TraceRecord(
        ts="2026-05-11T00:00:00Z",
        bot_id="bot_a",
        consult_id="abc123",
        action="ENTER",
        verdict={"size": 1.0},
        final_size=1.0,
    )
    trace_emitter.emit(rec, path=path)

    text = path.read_text()
    lines = [line for line in text.splitlines() if line]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["bot_id"] == "bot_a"
    assert parsed["consult_id"] == "abc123"
    assert parsed["action"] == "ENTER"
    assert parsed["final_size"] == 1.0
    assert parsed["verdict"] == {"size": 1.0}


def test_emit_appends_not_overwrites(tmp_path: Path) -> None:
    """emit 3 records, file has 3 lines in order."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "jarvis_trace.jsonl"
    for i in range(3):
        rec = trace_emitter.TraceRecord(
            ts=f"2026-05-11T00:00:0{i}Z",
            bot_id=f"bot_{i}",
            consult_id=f"id_{i}",
        )
        trace_emitter.emit(rec, path=path)

    lines = [line for line in path.read_text().splitlines() if line]
    assert len(lines) == 3
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert parsed["bot_id"] == f"bot_{i}"
        assert parsed["consult_id"] == f"id_{i}"


def test_emit_never_raises_on_bad_path(tmp_path: Path) -> None:
    """emit to a path under a non-existent/unwritable dir → no exception."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    # Path with a NUL char (invalid on every OS) — would normally raise.
    bad_path = Path("\x00/this/cannot/be/written/jarvis_trace.jsonl")
    rec = trace_emitter.TraceRecord(bot_id="x", consult_id="y")
    # MUST NOT raise
    trace_emitter.emit(rec, path=bad_path)


def test_rotation_when_size_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write enough records to exceed MAX_BYTES_PER_FILE → rotated file appears, active file fresh."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    # Shrink rotation limit to 1 KB for test speed.
    monkeypatch.setattr(trace_emitter, "MAX_BYTES_PER_FILE", 1024)

    path = tmp_path / "jarvis_trace.jsonl"
    # Each record's JSON line is ~120-200 bytes; 50 should clear 1 KB easily.
    for i in range(50):
        rec = trace_emitter.TraceRecord(
            ts=f"2026-05-11T00:00:{i:02d}Z",
            bot_id=f"bot_with_a_decent_length_id_{i}",
            consult_id=f"consult_id_padding_{i:08d}",
            action="ENTER",
            verdict={"size": 1.0, "padding": "x" * 30},
        )
        trace_emitter.emit(rec, path=path)

    # At least one rotated .gz file must exist in tmp_path
    rotated = list(tmp_path.glob("jarvis_trace_*.jsonl.gz"))
    assert len(rotated) >= 1, f"Expected rotated file(s), got: {list(tmp_path.iterdir())}"

    # Each rotated file is valid gzip and contains valid JSON lines
    for r in rotated:
        with gzip.open(r, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    json.loads(line)  # must parse

    # The active file must be smaller than MAX_BYTES_PER_FILE (fresh)
    if path.exists():
        assert path.stat().st_size <= trace_emitter.MAX_BYTES_PER_FILE


def test_tail_returns_last_n(tmp_path: Path) -> None:
    """emit 30 records, tail(5) returns the last 5 in order."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "jarvis_trace.jsonl"
    for i in range(30):
        rec = trace_emitter.TraceRecord(
            ts=f"2026-05-11T00:{i:02d}:00Z",
            bot_id=f"bot_{i}",
            consult_id=f"id_{i:04d}",
        )
        trace_emitter.emit(rec, path=path)

    result = trace_emitter.tail(n=5, path=path)
    assert len(result) == 5
    # Newest last — so the last entry should be id_0029
    assert result[-1]["consult_id"] == "id_0029"
    assert result[0]["consult_id"] == "id_0025"
    # Verify ordering
    for i, rec in enumerate(result):
        assert rec["consult_id"] == f"id_{25 + i:04d}"


def test_tail_handles_missing_file(tmp_path: Path) -> None:
    """tail on a non-existent path returns []."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    missing = tmp_path / "does_not_exist.jsonl"
    assert trace_emitter.tail(n=10, path=missing) == []


def test_new_consult_id_unique() -> None:
    """call 100 times, all distinct, all 12 chars."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    ids = [trace_emitter.new_consult_id() for _ in range(100)]
    assert len(set(ids)) == 100
    for cid in ids:
        assert isinstance(cid, str)
        assert len(cid) == 12


def test_record_default_factory() -> None:
    """TraceRecord() returns a record with empty dicts/lists (not None)."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    rec = trace_emitter.TraceRecord()
    assert rec.verdict == {}
    assert rec.schools == {}
    assert rec.clashes == []
    assert rec.dissent == []
    assert rec.portfolio == {}
    assert rec.context == {}
    assert rec.hot_learn == {}
    assert rec.ts == ""
    assert rec.bot_id == ""
    assert rec.consult_id == ""
    assert rec.action == ""
    assert rec.final_size == 0.0
    assert rec.block_reason is None
    assert rec.elapsed_ms == 0.0


# ---------------------------------------------------------------------------
# read_since() — cursor-based subscribe path used by jarvis_subscribe_events
# ---------------------------------------------------------------------------


def test_read_since_returns_empty_when_caught_up(tmp_path: Path) -> None:
    """offset == file_size → ([], file_size). No-op poll, no records returned."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    for i in range(3):
        trace_emitter.emit(
            trace_emitter.TraceRecord(consult_id=f"c{i}", bot_id=f"b{i}"),
            path=path,
        )
    file_size = path.stat().st_size
    records, next_offset = trace_emitter.read_since(offset=file_size, path=path)
    assert records == []
    assert next_offset == file_size


def test_read_since_returns_new_records_after_offset(tmp_path: Path) -> None:
    """Cursor pattern: poll, emit, poll again — second poll sees only new records."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    # First batch
    trace_emitter.emit(trace_emitter.TraceRecord(consult_id="c0"), path=path)
    trace_emitter.emit(trace_emitter.TraceRecord(consult_id="c1"), path=path)

    # First poll from offset=0 sees both
    records, offset_after_first = trace_emitter.read_since(offset=0, path=path)
    ids_first = [r["consult_id"] for r in records]
    assert ids_first == ["c0", "c1"]
    assert offset_after_first == path.stat().st_size

    # Second batch added after the first poll
    trace_emitter.emit(trace_emitter.TraceRecord(consult_id="c2"), path=path)
    trace_emitter.emit(trace_emitter.TraceRecord(consult_id="c3"), path=path)

    # Second poll only sees the new ones
    records, offset_after_second = trace_emitter.read_since(
        offset=offset_after_first, path=path,
    )
    ids_second = [r["consult_id"] for r in records]
    assert ids_second == ["c2", "c3"]
    assert offset_after_second == path.stat().st_size


def test_read_since_handles_missing_file(tmp_path: Path) -> None:
    """Missing file → ([], 0). Doesn't raise."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    records, offset = trace_emitter.read_since(
        offset=42, path=tmp_path / "does_not_exist.jsonl",
    )
    assert records == []
    assert offset == 0


def test_read_since_handles_partial_trailing_line(tmp_path: Path) -> None:
    """Partial line (no trailing newline) is not consumed; cursor stops at last newline."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    # Write one complete line plus an incomplete fragment.
    with path.open("w", encoding="utf-8") as fh:
        fh.write('{"consult_id":"complete"}\n')
        fh.write('{"consult_id":"partial')  # missing closing brace + newline

    records, next_offset = trace_emitter.read_since(offset=0, path=path)
    assert len(records) == 1
    assert records[0]["consult_id"] == "complete"
    # Cursor must NOT advance past the partial fragment.
    assert next_offset < path.stat().st_size


def test_read_since_resets_when_file_rotated(tmp_path: Path) -> None:
    """offset > file_size (file was rotated/truncated) → reset to 0, return what's there."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    trace_emitter.emit(trace_emitter.TraceRecord(consult_id="fresh"), path=path)
    fake_old_offset = path.stat().st_size + 99999  # caller's stale cursor from before rotation

    records, next_offset = trace_emitter.read_since(offset=fake_old_offset, path=path)
    assert len(records) == 1
    assert records[0]["consult_id"] == "fresh"
    assert next_offset == path.stat().st_size


def test_read_since_skips_malformed_but_advances(tmp_path: Path) -> None:
    """Garbage line is skipped from the returned list but doesn't stall the cursor."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not-json garbage\n")
        fh.write('{"consult_id":"good"}\n')

    records, next_offset = trace_emitter.read_since(offset=0, path=path)
    assert len(records) == 1
    assert records[0]["consult_id"] == "good"
    assert next_offset == path.stat().st_size


def test_read_since_respects_limit(tmp_path: Path) -> None:
    """limit caps one poll without skipping unreturned records."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    for i in range(5):
        trace_emitter.emit(trace_emitter.TraceRecord(consult_id=f"c{i}"), path=path)

    records, next_offset = trace_emitter.read_since(offset=0, limit=2, path=path)
    assert [r["consult_id"] for r in records] == ["c0", "c1"]

    more_records, final_offset = trace_emitter.read_since(
        offset=next_offset, limit=10, path=path,
    )
    assert [r["consult_id"] for r in more_records] == ["c2", "c3", "c4"]
    assert final_offset == path.stat().st_size
