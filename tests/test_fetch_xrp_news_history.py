"""Tests for ``eta_engine.scripts.fetch_xrp_news_history``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.scripts import fetch_xrp_news_history as mod
from eta_engine.scripts import workspace_roots


def test_main_rejects_output_root_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)

    with pytest.raises(SystemExit) as exc:
        mod.main(["--root", str(outside_workspace)])

    assert exc.value.code == 2


def test_main_writes_sentiment_csv_with_mocked_series(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    day = datetime(2026, 4, 29, tzinfo=UTC).date()

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:  # type: ignore[no-untyped-def]
            return datetime(2026, 4, 29, 20, tzinfo=tz or UTC)

    monkeypatch.setattr(mod, "datetime", FakeDateTime)
    monkeypatch.setattr(
        mod,
        "_build_daily_series",
        lambda queries, days: {day: {"sec_mentions_ripple": 2, "sec_mentions_xrp": 1}},
    )
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", tmp_path.parent)

    rc = mod.main(["--days", "1", "--root", str(tmp_path)])

    assert rc == 0
    out = tmp_path / "XRPSENT_D.csv"
    assert out.exists()
    assert "sec_mentions_ripple" in out.read_text(encoding="utf-8")
