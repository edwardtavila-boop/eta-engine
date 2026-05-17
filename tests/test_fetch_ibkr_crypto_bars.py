"""Tests for ``eta_engine.scripts.fetch_ibkr_crypto_bars``."""

from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.scripts import fetch_ibkr_crypto_bars as mod
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
        mod.main(
            [
                "--symbol",
                "BTC",
                "--start",
                "2026-05-01",
                "--end",
                "2026-05-02",
                "--root",
                str(outside_workspace),
            ],
        )

    assert exc.value.code == 2
