"""Tests for ``eta_engine.scripts.resample_btc_timeframes``."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.scripts import resample_btc_timeframes as mod
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
        mod.main(["--symbol", "BTC", "--tf", "15m", "--root", str(outside_workspace)])

    assert exc.value.code == 2


def test_main_synthesizes_inside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    history_root = workspace / "data" / "crypto" / "history"
    history_root.mkdir(parents=True)
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", workspace)

    base_ts = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    with (history_root / "BTC_5m.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        writer.writerow([base_ts, 100.0, 101.0, 99.0, 100.5, 10.0])
        writer.writerow([base_ts + 300, 100.5, 102.0, 100.0, 101.5, 11.0])
        writer.writerow([base_ts + 600, 101.5, 103.0, 101.0, 102.5, 12.0])

    rc = mod.main(["--symbol", "BTC", "--tf", "15m", "--root", str(history_root)])

    assert rc == 0
    out = history_root / "BTC_15m.csv"
    assert out.exists()
    with out.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["open"] == "100.0"
    assert rows[0]["high"] == "103.0"
    assert rows[0]["volume"] == "33.0"
