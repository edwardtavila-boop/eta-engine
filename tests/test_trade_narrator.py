"""Tests for trade_narrator — Track 10 (deterministic per-consult narrator)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path


def test_narrate_renders_basic_record() -> None:
    """A well-formed trace record → readable one-line paragraph."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    rec = {
        "ts": "2026-05-12T14:32:08+00:00",
        "bot_id": "atr_breakout_mnq",
        "consult_id": "abc12345dead",
        "action": "ENTER",
        "verdict": {"final_verdict": "PROCEED", "final_size_multiplier": 0.7},
        "final_size": 0.7,
    }
    line = trade_narrator.narrate(rec)
    assert "[14:32:08]" in line
    assert "atr_breakout_mnq" in line
    assert "PROCEED" in line
    assert "70%" in line
    assert "consult=abc12345" in line  # first 8 chars


def test_narrate_handles_dissent() -> None:
    """Dissent list shows up in the paragraph."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    rec = {
        "ts": "2026-05-12T09:00:00+00:00",
        "bot_id": "vp_mnq",
        "consult_id": "d1",
        "verdict": {"final_verdict": "PROCEED", "final_size_multiplier": 1.0},
        "dissent": [
            {"school": "mean_revert"},
            {"school": "momentum"},
        ],
    }
    line = trade_narrator.narrate(rec)
    assert "Dissent:" in line
    assert "mean_revert" in line
    assert "momentum" in line


def test_narrate_handles_block_reason() -> None:
    """Blocked consult surfaces the block_reason."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    rec = {
        "ts": "2026-05-12T10:00:00+00:00",
        "bot_id": "bot_x",
        "consult_id": "blocked1",
        "verdict": {"final_verdict": "BLOCKED", "final_size_multiplier": 0.0},
        "block_reason": "fleet_kill_active",
    }
    line = trade_narrator.narrate(rec)
    assert "BLOCKED: fleet_kill_active" in line


def test_narrate_never_raises_on_garbage() -> None:
    """Non-dict input → degraded placeholder, no exception."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    assert "??:??:??" in trade_narrator.narrate(None)  # type: ignore[arg-type]
    assert "??:??:??" in trade_narrator.narrate("not a dict")  # type: ignore[arg-type]
    assert "??:??:??" in trade_narrator.narrate(42)  # type: ignore[arg-type]


def test_narrate_handles_missing_fields() -> None:
    """Empty dict → returns SOME paragraph with placeholders."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    line = trade_narrator.narrate({})
    # Should NOT crash; should have a placeholder for time, bot, etc.
    assert "?" in line
    assert "UNKNOWN" in line  # action fallback


def test_append_to_journal_creates_dated_file(tmp_path: Path) -> None:
    """First append on a fresh day creates the file + header."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    rec = {
        "ts": "2026-05-12T15:00:00+00:00",
        "bot_id": "bot_a",
        "consult_id": "c1",
        "verdict": {"final_verdict": "PROCEED", "final_size_multiplier": 1.0},
    }
    fixed_now = datetime(2026, 5, 12, 15, 0, 0, tzinfo=UTC)
    ok = trade_narrator.append_to_journal(rec, journal_dir=tmp_path, now=fixed_now)
    assert ok
    expected = tmp_path / "2026-05-12.md"
    assert expected.exists()
    content = expected.read_text(encoding="utf-8")
    assert "# JARVIS Trade Journal" in content
    assert "bot_a" in content
    assert "PROCEED" in content


def test_append_to_journal_appends_without_duplicating_header(tmp_path: Path) -> None:
    """Second append on the same day re-uses the file, header appears once."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    fixed_now = datetime(2026, 5, 12, 15, 0, 0, tzinfo=UTC)
    for i in range(3):
        trade_narrator.append_to_journal(
            {"ts": fixed_now.isoformat(), "bot_id": f"b{i}", "consult_id": f"c{i}",
             "verdict": {"final_verdict": "HOLD", "final_size_multiplier": 0.0}},
            journal_dir=tmp_path,
            now=fixed_now,
        )

    content = (tmp_path / "2026-05-12.md").read_text(encoding="utf-8")
    # Header is the substring that appears once
    assert content.count("# JARVIS Trade Journal") == 1
    # Each bot_id appears
    for i in range(3):
        assert f"b{i}" in content


def test_read_day_returns_empty_string_when_missing(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import trade_narrator

    assert trade_narrator.read_day(date(2020, 1, 1), journal_dir=tmp_path) == ""


def test_read_day_accepts_iso_string(tmp_path: Path) -> None:
    """Pass 'YYYY-MM-DD' instead of a date object → still works."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    fixed_now = datetime(2026, 5, 12, 15, 0, 0, tzinfo=UTC)
    trade_narrator.append_to_journal(
        {"ts": fixed_now.isoformat(), "bot_id": "b", "consult_id": "c",
         "verdict": {"final_verdict": "PROCEED", "final_size_multiplier": 1.0}},
        journal_dir=tmp_path,
        now=fixed_now,
    )
    content_str = trade_narrator.read_day("2026-05-12", journal_dir=tmp_path)
    assert "PROCEED" in content_str
    # Bad string returns empty rather than raising
    assert trade_narrator.read_day("not-a-date", journal_dir=tmp_path) == ""


def test_week_files_returns_only_existing(tmp_path: Path) -> None:
    """week_files skips days without journal entries — no empty paths returned."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    end = date(2026, 5, 12)
    # Seed Monday, Wednesday, Friday only
    for d in (end - timedelta(days=6), end - timedelta(days=4), end - timedelta(days=2)):
        (tmp_path / f"{d.isoformat()}.md").write_text("seeded", encoding="utf-8")

    files = trade_narrator.week_files(end_date=end, journal_dir=tmp_path)
    assert len(files) == 3
    # All exist
    assert all(f.exists() for f in files)


def test_append_never_raises_on_bad_dir(tmp_path: Path, monkeypatch) -> None:
    """If mkdir fails, append returns False — no exception."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    def explode(*a, **kw):
        raise OSError("simulated disk error")

    # Force mkdir to fail
    monkeypatch.setattr("pathlib.Path.mkdir", explode)
    ok = trade_narrator.append_to_journal(
        {"ts": "2026-05-12T10:00:00Z", "bot_id": "x", "consult_id": "c",
         "verdict": {"final_verdict": "PROCEED", "final_size_multiplier": 1.0}},
        journal_dir=tmp_path,
    )
    assert ok is False


def test_size_modifier_renders_from_verdict_or_top_level() -> None:
    """size% prefers verdict.final_size_multiplier, falls back to final_size."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    rec_a = {
        "ts": "2026-05-12T00:00:00Z", "bot_id": "b", "consult_id": "c",
        "verdict": {"final_size_multiplier": 0.42},
    }
    rec_b = {
        "ts": "2026-05-12T00:00:00Z", "bot_id": "b", "consult_id": "c",
        "final_size": 0.42,
    }
    assert "42%" in trade_narrator.narrate(rec_a)
    assert "42%" in trade_narrator.narrate(rec_b)


def test_expected_hooks_declared() -> None:
    """Wiring audit must be able to read EXPECTED_HOOKS."""
    from eta_engine.brain.jarvis_v3 import trade_narrator

    assert "narrate" in trade_narrator.EXPECTED_HOOKS
    assert "append_to_journal" in trade_narrator.EXPECTED_HOOKS
    assert "read_day" in trade_narrator.EXPECTED_HOOKS
    assert "week_files" in trade_narrator.EXPECTED_HOOKS
