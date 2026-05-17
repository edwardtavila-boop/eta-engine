"""Tests for ``eta_engine.scripts.fetch_btc_open_interest``."""

from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.scripts import fetch_btc_open_interest as mod
from eta_engine.scripts import workspace_roots


def test_main_rejects_output_path_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "BTCOI_1h.csv"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)

    with pytest.raises(SystemExit) as exc:
        mod.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2
