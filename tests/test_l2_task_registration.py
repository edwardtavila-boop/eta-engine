"""Static checks for the VPS L2 scheduled-task registrar."""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "register_l2_cron_tasks.ps1"


def test_l2_cron_registrar_uses_runtime_overrides() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ETA_PYTHON_EXE" in text
    assert "ETA_TASK_USER" in text
    assert "[string]$WorkspaceRoot" in text
    assert "$env:USERDOMAIN\\$env:USERNAME" in text
    assert "fxut9145410" not in text


def test_l2_cron_registrar_registers_expected_tasks() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for task_name in (
        "ETA-L2-BacktestDaily",
        "ETA-L2-PromotionEvaluator",
        "ETA-L2-CalibrationDaily",
        "ETA-L2-RegistryAdapter",
        "ETA-L2-SweepWeekly",
        "ETA-L2-FillAuditWeekly",
    ):
        assert task_name in text
    assert "-RunLevel Limited" in text
    assert "New-ScheduledTaskTrigger -Daily" in text
    assert "New-ScheduledTaskTrigger -Weekly" in text


def test_l2_cron_registrar_start_now_is_explicit() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "[switch]$StartNow" in text
    assert "if ($StartNow)" in text
    assert "Start-ScheduledTask -TaskName $t.Name" in text
