from __future__ import annotations

from pathlib import Path

from scripts import submodule_wiring_preflight


def test_parse_submodule_status_prefixes() -> None:
    status = submodule_wiring_preflight.parse_submodule_status_lines(
        [
            " 15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (main)",
            "+19768b0cc158bdc920fdb44e42e0e23931282b8e firm (feature)",
            "-1c3a2ef93a2d25561a4ec3e022cdbe1176ce590a mnq_backtest",
        ]
    )

    assert status["eta_engine"].gitlink == "aligned"
    assert status["firm"].gitlink == "diverged"
    assert status["mnq_backtest"].gitlink == "uninitialized"


def test_report_blocks_dirty_or_diverged_submodules(tmp_path: Path) -> None:
    for name in ("eta_engine", "firm", "mnq_backtest"):
        (tmp_path / name).mkdir()

    report = submodule_wiring_preflight.evaluate_submodule_wiring(
        root=tmp_path,
        required_modules=("eta_engine", "firm", "mnq_backtest"),
        submodule_status_lines=[
            " 15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (main)",
            "+19768b0cc158bdc920fdb44e42e0e23931282b8e firm (feature)",
            " 1c3a2ef93a2d25561a4ec3e022cdbe1176ce590a mnq_backtest (master)",
        ],
        porcelain_by_module={
            "eta_engine": [],
            "firm": [" M eta_engine/src/mnq/risk/gate_chain.py"],
            "mnq_backtest": [],
        },
    )

    assert report.ready is False
    assert report.action == "do_not_wire_gitlinks"
    assert report.modules["eta_engine"].ready is True
    assert report.modules["firm"].ready is False
    assert "gitlink diverged" in report.modules["firm"].blockers
    assert "dirty worktree" in report.modules["firm"].blockers


def test_report_requires_every_module(tmp_path: Path) -> None:
    (tmp_path / "eta_engine").mkdir()

    report = submodule_wiring_preflight.evaluate_submodule_wiring(
        root=tmp_path,
        required_modules=("eta_engine", "firm"),
        submodule_status_lines=[
            " 15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (main)",
        ],
        porcelain_by_module={"eta_engine": []},
    )

    assert report.ready is False
    assert report.modules["firm"].gitlink == "missing"
    assert "missing submodule checkout" in report.modules["firm"].blockers


def test_payload_is_machine_readable(tmp_path: Path) -> None:
    (tmp_path / "eta_engine").mkdir()

    report = submodule_wiring_preflight.evaluate_submodule_wiring(
        root=tmp_path,
        required_modules=("eta_engine",),
        submodule_status_lines=[
            " 15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (main)",
        ],
        porcelain_by_module={"eta_engine": []},
    )

    payload = report.as_payload()
    assert payload["ready"] is True
    assert payload["action"] == "safe_to_wire_gitlinks"
    assert payload["modules"]["eta_engine"]["dirty_entries"] == []
