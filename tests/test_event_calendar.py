"""Tests for eta_engine.data.event_calendar — operator-curated event YAML reader."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.data import event_calendar as ec


def test_load_missing_file_returns_empty(tmp_path) -> None:
    """A non-existent path returns an empty tuple, never raises."""
    missing = tmp_path / "does_not_exist.yaml"
    result = ec.load(path=missing)
    assert result == ()


def test_load_malformed_yaml_returns_empty(tmp_path) -> None:
    """Malformed YAML returns empty tuple, never raises."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: a: list:\n - nope\n  bad indent", encoding="utf-8")
    result = ec.load(path=bad)
    assert result == ()


def test_load_valid_yaml(tmp_path) -> None:
    """A two-event YAML returns two CalendarEvents with fields populated."""
    yaml_text = (
        "events:\n"
        "  - ts_utc: \"2026-06-18T18:00:00Z\"\n"
        "    kind: FOMC\n"
        "    symbol: null\n"
        "    severity: 3\n"
        "  - ts_utc: \"2026-05-13T12:30:00Z\"\n"
        "    kind: CPI\n"
        "    symbol: null\n"
        "    severity: 3\n"
    )
    path = tmp_path / "cal.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    result = ec.load(path=path)
    assert len(result) == 2
    assert result[0].kind == "FOMC"
    assert result[0].severity == 3
    assert result[1].kind == "CPI"


def test_upcoming_filters_horizon(tmp_path) -> None:
    """Three events at +10, +30, +90 min; horizon=60 returns first two."""
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    events = [
        (now + timedelta(minutes=10), "FOMC"),
        (now + timedelta(minutes=30), "CPI"),
        (now + timedelta(minutes=90), "NFP"),
    ]
    yaml_text = "events:\n"
    for ts, kind in events:
        yaml_text += (
            f"  - ts_utc: \"{ts.strftime('%Y-%m-%dT%H:%M:%SZ')}\"\n"
            f"    kind: {kind}\n"
            "    symbol: null\n"
            "    severity: 2\n"
        )
    path = tmp_path / "cal.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    result = ec.upcoming(now, horizon_min=60, path=path)
    assert len(result) == 2
    assert result[0].kind == "FOMC"
    assert result[1].kind == "CPI"


def test_upcoming_ignores_past(tmp_path) -> None:
    """Events in the past are not returned."""
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    past_ts = now - timedelta(minutes=10)
    yaml_text = (
        "events:\n"
        f"  - ts_utc: \"{past_ts.strftime('%Y-%m-%dT%H:%M:%SZ')}\"\n"
        "    kind: FOMC\n"
        "    symbol: null\n"
        "    severity: 3\n"
    )
    path = tmp_path / "cal.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    result = ec.upcoming(now, horizon_min=60, path=path)
    assert result == []
