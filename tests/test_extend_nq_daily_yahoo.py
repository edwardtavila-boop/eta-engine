"""Tests for scripts.extend_nq_daily_yahoo.

The actual yfinance call is the tricky path; the unit suite stubs
that out via monkeypatch and exercises the deterministic glue:
file IO, dedup against last_ts, summary shape.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - used in stub signature at runtime
from pathlib import Path  # noqa: TC003 - tmp_path fixture annotation

import pytest  # noqa: TC002 - monkeypatch fixture annotation

from eta_engine.scripts.extend_nq_daily_yahoo import (
    _append_rows,
    _read_existing,
    extend,
)

# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def test_read_existing_missing_file_returns_none(tmp_path: Path) -> None:
    last_ts, n = _read_existing(tmp_path / "no_such.csv")
    assert last_ts is None
    assert n == 0


def test_read_existing_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "out.csv"
    _append_rows(
        p,
        [
            {"time": 1_700_000_000, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 10},
            {"time": 1_700_086_400, "open": 2, "high": 3, "low": 1, "close": 2, "volume": 20},
        ],
    )
    last_ts, n = _read_existing(p)
    assert n == 2
    assert last_ts == 1_700_086_400


def test_append_rows_writes_header_once(tmp_path: Path) -> None:
    p = tmp_path / "out.csv"
    _append_rows(
        p,
        [
            {
                "time": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ],
    )
    _append_rows(
        p,
        [
            {
                "time": 2,
                "open": 2,
                "high": 2,
                "low": 2,
                "close": 2,
                "volume": 2,
            }
        ],
    )
    text = p.read_text(encoding="utf-8")
    # Exactly one header line.
    assert text.count("time,open,high,low,close,volume") == 1


def test_append_rows_empty_returns_zero(tmp_path: Path) -> None:
    assert _append_rows(tmp_path / "out.csv", []) == 0


# ---------------------------------------------------------------------------
# extend() behaviour
# ---------------------------------------------------------------------------


def _stub_yahoo(rows: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    """Build a monkeypatch target replacing _fetch_yahoo_daily."""

    def _fn(_symbol: str, _start: datetime) -> list[dict[str, object]]:
        return rows

    return _fn


def test_extend_writes_only_fresh_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing CSV at last_ts=100; Yahoo returns rows at 50,100,150,200.
    Only 150 + 200 should land on disk."""
    p = tmp_path / "NQ1_D.csv"
    _append_rows(
        p,
        [
            {
                "time": 100,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ],
    )
    fake_rows = [
        {"time": 50, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0},
        {"time": 100, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"time": 150, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
        {"time": 200, "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3},
    ]
    monkeypatch.setattr(
        "eta_engine.scripts.extend_nq_daily_yahoo._fetch_yahoo_daily",
        _stub_yahoo(fake_rows),
    )
    summary = extend("NQ=F", p, dry_run=False)
    assert summary["fresh_rows"] == 2
    assert summary["written"] == 2
    last_ts, n = _read_existing(p)
    assert n == 3  # original + 2 new
    assert last_ts == 200


def test_extend_dry_run_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "NQ1_D.csv"
    _append_rows(
        p,
        [
            {
                "time": 100,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ],
    )
    monkeypatch.setattr(
        "eta_engine.scripts.extend_nq_daily_yahoo._fetch_yahoo_daily",
        _stub_yahoo(
            [
                {"time": 200, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
            ]
        ),
    )
    summary = extend("NQ=F", p, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["fresh_rows"] == 1
    assert "written" not in summary  # never touched disk
    _last_ts, n = _read_existing(p)
    assert n == 1  # unchanged


def test_extend_reports_zero_on_empty_yahoo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "NQ1_D.csv"
    _append_rows(
        p,
        [
            {
                "time": 100,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ],
    )
    monkeypatch.setattr(
        "eta_engine.scripts.extend_nq_daily_yahoo._fetch_yahoo_daily",
        _stub_yahoo([]),
    )
    summary = extend("NQ=F", p, dry_run=False)
    assert summary["fresh_rows"] == 0
    # Should have skipped the append entirely.
    _last_ts, n = _read_existing(p)
    assert n == 1
