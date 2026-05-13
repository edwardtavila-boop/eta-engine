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
        offset=offset_after_first,
        path=path,
    )
    ids_second = [r["consult_id"] for r in records]
    assert ids_second == ["c2", "c3"]
    assert offset_after_second == path.stat().st_size


def test_read_since_handles_missing_file(tmp_path: Path) -> None:
    """Missing file → ([], 0). Doesn't raise."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    records, offset = trace_emitter.read_since(
        offset=42,
        path=tmp_path / "does_not_exist.jsonl",
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


# ---------------------------------------------------------------------------
# Schema v2 — T6/T7 prereq fields + helpers
# ---------------------------------------------------------------------------


def test_capture_v2_extras_returns_all_v2_fields() -> None:
    """capture_v2_extras() output dict has every schema v2 field."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    extras = trace_emitter.capture_v2_extras(
        bot_id="test_bot",
        asset_class="MNQ",
        portfolio_ctx=None,
        hot_weights={"momentum": 1.1},
        school_inputs={"momentum": {"score": 0.5}},
        rng_master_seed=42,
    )
    assert set(extras.keys()) == {
        "schema_version",
        "school_inputs",
        "portfolio_inputs",
        "hot_weights_snapshot",
        "overrides_snapshot",
        "rng_master_seed",
    }
    assert extras["schema_version"] == 2
    assert extras["hot_weights_snapshot"] == {"momentum": 1.1}
    assert extras["school_inputs"] == {"momentum": {"score": 0.5}}
    assert extras["rng_master_seed"] == 42


def test_capture_v2_extras_with_dataclass_portfolio_ctx() -> None:
    """A PortfolioContext dataclass gets unpacked into the snapshot dict."""
    from eta_engine.brain.jarvis_v3 import portfolio_brain, trace_emitter

    ctx = portfolio_brain.PortfolioContext(
        fleet_long_notional_by_asset={"MNQ": 50000},
        fleet_short_notional_by_asset={},
        recent_entries_by_asset={"MNQ": 3},
        open_correlated_exposure=0.4,
        portfolio_drawdown_today_r=-1.5,
        fleet_kill_active=False,
    )
    extras = trace_emitter.capture_v2_extras(
        bot_id="b",
        asset_class="MNQ",
        portfolio_ctx=ctx,
    )
    pi = extras["portfolio_inputs"]
    assert pi["portfolio_drawdown_today_r"] == -1.5
    assert pi["fleet_long_notional_by_asset"] == {"MNQ": 50000}
    assert pi["fleet_kill_active"] is False


def test_capture_v2_extras_handles_dict_portfolio_ctx() -> None:
    """A pre-built dict is accepted as portfolio_ctx (test fixtures)."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    ctx_dict = {"fleet_kill_active": True, "portfolio_drawdown_today_r": -2.0}
    extras = trace_emitter.capture_v2_extras(
        bot_id="b",
        asset_class="MNQ",
        portfolio_ctx=ctx_dict,
    )
    assert extras["portfolio_inputs"]["fleet_kill_active"] is True
    assert extras["portfolio_inputs"]["portfolio_drawdown_today_r"] == -2.0


def test_capture_v2_extras_captures_overrides_snapshot(tmp_path, monkeypatch) -> None:
    """Live overrides for the bot AND asset appear in the snapshot."""
    from eta_engine.brain.jarvis_v3 import hermes_overrides, trace_emitter

    overrides_path = tmp_path / "ho.json"
    monkeypatch.setattr(hermes_overrides, "DEFAULT_OVERRIDES_PATH", overrides_path)
    # Pin both a size_modifier and a school weight
    hermes_overrides.apply_size_modifier(
        bot_id="test_bot",
        modifier=0.6,
        reason="test",
        ttl_minutes=10,
        path=overrides_path,
    )
    hermes_overrides.apply_school_weight(
        asset="MNQ",
        school="momentum",
        weight=1.3,
        reason="test",
        ttl_minutes=10,
        path=overrides_path,
    )
    extras = trace_emitter.capture_v2_extras(
        bot_id="test_bot",
        asset_class="MNQ",
    )
    snap = extras["overrides_snapshot"]
    assert snap["size_modifier"] == 0.6
    assert snap["school_weights"] == {"momentum": 1.3}


def test_capture_v2_extras_never_raises_on_missing_ctx() -> None:
    """portfolio_ctx=None → portfolio_inputs empty, no exception."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    extras = trace_emitter.capture_v2_extras(
        bot_id="b",
        asset_class="MNQ",
        portfolio_ctx=None,
    )
    assert extras["portfolio_inputs"] == {}


def test_capture_v2_extras_never_raises_on_broken_ctx() -> None:
    """portfolio_ctx that raises on attribute access → partial capture, no exception."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    class Hostile:
        def __getattribute__(self, name: str) -> object:
            raise RuntimeError(f"refuses to expose {name}")

    extras = trace_emitter.capture_v2_extras(
        bot_id="b",
        asset_class="MNQ",
        portfolio_ctx=Hostile(),
    )
    # No exception; portfolio_inputs ends up empty
    assert extras["portfolio_inputs"] == {}


def test_v2_record_emitted_with_extras_round_trips(tmp_path) -> None:
    """A TraceRecord built from capture_v2_extras() round-trips through emit/tail."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    extras = trace_emitter.capture_v2_extras(
        bot_id="round_trip_bot",
        asset_class="MNQ",
        hot_weights={"momentum": 1.05},
        rng_master_seed=7,
    )
    rec = trace_emitter.TraceRecord(
        ts="2026-05-12T22:30:00+00:00",
        bot_id="round_trip_bot",
        consult_id="rt001",
        action="ENTER",
        **extras,
    )
    path = tmp_path / "trace.jsonl"
    trace_emitter.emit(rec, path=path)
    loaded = trace_emitter.tail(n=1, path=path)
    assert len(loaded) == 1
    assert loaded[0]["schema_version"] == 2
    assert loaded[0]["hot_weights_snapshot"] == {"momentum": 1.05}
    assert loaded[0]["rng_master_seed"] == 7
    # is_v2_record dispatches correctly
    assert trace_emitter.is_v2_record(loaded[0]) is True
    # extract_replay_inputs returns the snapshot
    ri = trace_emitter.extract_replay_inputs(loaded[0])
    assert ri is not None
    assert ri["hot_weights_snapshot"] == {"momentum": 1.05}


def test_v1_record_has_schema_version_1_by_default() -> None:
    """Legacy emitters that don't touch the v2 fields produce v1 records."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    rec = trace_emitter.TraceRecord(bot_id="b1", consult_id="c1")
    assert rec.schema_version == 1
    assert rec.school_inputs == {}
    assert rec.portfolio_inputs == {}
    assert rec.hot_weights_snapshot == {}
    assert rec.overrides_snapshot == {}
    assert rec.rng_master_seed is None


def test_v2_record_round_trips_through_json(tmp_path: Path) -> None:
    """A record with v2 fields populated survives emit + tail without loss."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    rec = trace_emitter.TraceRecord(
        ts="2026-05-12T15:00:00+00:00",
        bot_id="b1",
        consult_id="c_v2",
        action="ENTER",
        verdict={"final_verdict": "PROCEED"},
        schema_version=2,
        school_inputs={
            "momentum": {"score": 1.2, "size_modifier": 0.8, "rng_seed": 42},
            "mean_revert": {"score": -0.5, "size_modifier": 0.0, "rng_seed": None},
        },
        portfolio_inputs={
            "fleet_long_notional_by_asset": {"MNQ": 50000},
            "portfolio_drawdown_today_r": -1.2,
        },
        hot_weights_snapshot={"momentum": 1.1, "mean_revert": 0.9},
        overrides_snapshot={"size_modifier": 0.7},
        rng_master_seed=12345,
    )
    trace_emitter.emit(rec, path=path)

    out = trace_emitter.tail(n=1, path=path)
    assert len(out) == 1
    loaded = out[0]
    assert loaded["schema_version"] == 2
    assert loaded["school_inputs"]["momentum"]["score"] == 1.2
    assert loaded["school_inputs"]["mean_revert"]["rng_seed"] is None
    assert loaded["portfolio_inputs"]["portfolio_drawdown_today_r"] == -1.2
    assert loaded["hot_weights_snapshot"] == {"momentum": 1.1, "mean_revert": 0.9}
    assert loaded["overrides_snapshot"]["size_modifier"] == 0.7
    assert loaded["rng_master_seed"] == 12345


def test_is_v2_record_recognizes_dataclass_and_dict() -> None:
    from eta_engine.brain.jarvis_v3 import trace_emitter

    # Dataclass path
    v1_rec = trace_emitter.TraceRecord(bot_id="b1")
    v2_rec = trace_emitter.TraceRecord(bot_id="b2", schema_version=2)
    assert trace_emitter.is_v2_record(v1_rec) is False
    assert trace_emitter.is_v2_record(v2_rec) is True

    # Dict path (what we get from reading JSONL)
    assert trace_emitter.is_v2_record({"bot_id": "b"}) is False
    assert trace_emitter.is_v2_record({"schema_version": 1}) is False
    assert trace_emitter.is_v2_record({"schema_version": 2}) is True
    assert trace_emitter.is_v2_record({"schema_version": 3}) is True  # future v3
    # Bogus values fall back to v1
    assert trace_emitter.is_v2_record({"schema_version": "not_a_number"}) is False
    # Non-dict, non-dataclass
    assert trace_emitter.is_v2_record(None) is False  # type: ignore[arg-type]
    assert trace_emitter.is_v2_record("nope") is False  # type: ignore[arg-type]


def test_extract_replay_inputs_returns_none_for_v1() -> None:
    """Pre-v2 records lack the snapshot fields → return None so T6/T7 reject cleanly."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    v1_rec = trace_emitter.TraceRecord(bot_id="b1", consult_id="c1")
    assert trace_emitter.extract_replay_inputs(v1_rec) is None
    assert trace_emitter.extract_replay_inputs({"consult_id": "c", "schema_version": 1}) is None


def test_extract_replay_inputs_packs_all_snapshot_fields() -> None:
    """v2 record → dict with all 5 replay-input fields."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    v2_rec = trace_emitter.TraceRecord(
        bot_id="b1",
        consult_id="c2",
        schema_version=2,
        school_inputs={"momentum": {"score": 1.0}},
        portfolio_inputs={"drawdown": 0.0},
        hot_weights_snapshot={"momentum": 1.0},
        overrides_snapshot={"size_modifier": None},
        rng_master_seed=999,
    )
    out = trace_emitter.extract_replay_inputs(v2_rec)
    assert out is not None
    assert set(out.keys()) == {
        "school_inputs",
        "portfolio_inputs",
        "hot_weights_snapshot",
        "overrides_snapshot",
        "rng_master_seed",
    }
    assert out["school_inputs"]["momentum"]["score"] == 1.0
    assert out["rng_master_seed"] == 999


def test_v1_jsonl_line_parses_into_v2_dataclass_safely(tmp_path: Path) -> None:
    """A legacy v1 line (no schema_version key) round-trips through tail()
    with the v2 fields appearing as empty dicts / None — no exception."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    # Hand-write a legacy v1 line (mimics records emitted before this PR).
    legacy = (
        '{"ts":"2026-04-01T00:00:00Z","bot_id":"legacy_bot",'
        '"consult_id":"old1","action":"ENTER",'
        '"verdict":{"final_verdict":"PROCEED"},"final_size":1.0}'
    )
    path.write_text(legacy + "\n", encoding="utf-8")

    records = trace_emitter.tail(n=1, path=path)
    assert len(records) == 1
    legacy_rec = records[0]
    # Legacy fields readable
    assert legacy_rec["bot_id"] == "legacy_bot"
    # Schema dispatch correctly identifies as v1
    assert trace_emitter.is_v2_record(legacy_rec) is False
    # extract_replay_inputs returns None
    assert trace_emitter.extract_replay_inputs(legacy_rec) is None


def test_read_since_respects_limit(tmp_path: Path) -> None:
    """limit caps one poll without skipping unreturned records."""
    from eta_engine.brain.jarvis_v3 import trace_emitter

    path = tmp_path / "trace.jsonl"
    for i in range(5):
        trace_emitter.emit(trace_emitter.TraceRecord(consult_id=f"c{i}"), path=path)

    records, next_offset = trace_emitter.read_since(offset=0, limit=2, path=path)
    assert [r["consult_id"] for r in records] == ["c0", "c1"]

    more_records, final_offset = trace_emitter.read_since(
        offset=next_offset,
        limit=10,
        path=path,
    )
    assert [r["consult_id"] for r in more_records] == ["c2", "c3", "c4"]
    assert final_offset == path.stat().st_size
