from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts import disabled_worker_soak_guard


def _created_at() -> datetime:
    return datetime(2026, 5, 5, tzinfo=UTC)


def _write_registry(root: Path) -> None:
    path = root / "bots" / "registry.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test registry\n", encoding="utf-8")


def test_gate_blocks_until_fourteen_day_soak_completes(tmp_path: Path) -> None:
    _write_registry(tmp_path)

    report = disabled_worker_soak_guard.evaluate_unregistration_gate(
        root=tmp_path,
        created_at=_created_at(),
        now=datetime(2026, 5, 10, 12, tzinfo=UTC),
    )

    assert report.ready is False
    assert report.soak.ready is False
    assert report.registry_exists is True
    assert report.soak.ready_at == datetime(2026, 5, 19, tzinfo=UTC)
    assert any("14-day soak" in blocker for blocker in report.blockers)


def test_gate_blocks_after_soak_when_registry_is_missing(tmp_path: Path) -> None:
    report = disabled_worker_soak_guard.evaluate_unregistration_gate(
        root=tmp_path,
        created_at=_created_at(),
        now=datetime(2026, 5, 20, tzinfo=UTC),
    )

    assert report.ready is False
    assert report.soak.ready is True
    assert report.registry_exists is False
    assert any("bots/registry.py" in blocker for blocker in report.blockers)


def test_gate_requires_operator_approval_after_preconditions(tmp_path: Path) -> None:
    _write_registry(tmp_path)

    report = disabled_worker_soak_guard.evaluate_unregistration_gate(
        root=tmp_path,
        created_at=_created_at(),
        now=datetime(2026, 5, 20, tzinfo=UTC),
    )
    approved = disabled_worker_soak_guard.evaluate_unregistration_gate(
        root=tmp_path,
        created_at=_created_at(),
        now=datetime(2026, 5, 20, tzinfo=UTC),
        operator_approved=True,
    )

    assert report.preconditions_ready is True
    assert report.ready is False
    assert any("operator approval" in blocker for blocker in report.blockers)
    assert approved.ready is True


def test_cli_reports_blocked_json_without_writing_state(
    tmp_path: Path,
    capsys,
) -> None:
    _write_registry(tmp_path)

    exit_code = disabled_worker_soak_guard.main(
        [
            "--root",
            str(tmp_path),
            "--created-at",
            "2026-05-05T00:00:00Z",
            "--now",
            "2026-05-10T12:00:00Z",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["action"] == "do_not_unregister"
    assert payload["registry"]["path"] == "bots/registry.py"
