from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.scripts import quantum_daily_rebalance, workspace_roots


def test_cli_rejects_out_dir_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "quantum"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        quantum_daily_rebalance,
        "_compute_instrument_stats",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("trade stats should not load for rejected out-dir"),
        ),
    )

    with pytest.raises(SystemExit) as exc:
        quantum_daily_rebalance.main(["--out-dir", str(outside_workspace)])

    assert exc.value.code == 2
    assert not outside_workspace.exists()


def test_cli_rejects_state_dir_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_workspace = tmp_path / "workspace"
    output = fake_workspace / "var" / "eta_engine" / "state" / "quantum"
    outside_workspace = tmp_path / "outside" / "quantum_state"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        quantum_daily_rebalance,
        "_compute_instrument_stats",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("trade stats should not load for rejected state-dir"),
        ),
    )

    with pytest.raises(SystemExit) as exc:
        quantum_daily_rebalance.main(
            [
                "--out-dir",
                str(output),
                "--state-dir",
                str(outside_workspace),
            ],
        )

    assert exc.value.code == 2
    assert not output.exists()
    assert not outside_workspace.exists()
