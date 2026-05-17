from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRAR = ROOT.parent / "scripts" / "register-command-center-watchdog.ps1"


def test_command_center_watchdog_registrar_supports_system_then_current_user_fallback() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert 'TaskName = "Eta-CommandCenter-Doctor"' in text
    assert "[switch]$CurrentUser" in text
    assert "NT AUTHORITY\\SYSTEM" in text
    assert "LogonType ServiceAccount" in text
    assert "LogonType Interactive" in text
    assert "SYSTEM registration was unavailable" in text
    assert "Non-admin shell detected; registering $TaskName as a current-user fallback." in text
    assert 'Registered $TaskName every $EveryMinutes minute(s) as $registeredPrincipal.' in text
    assert "current_user:$currentUser" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUser" in text
    assert "command-center-doctor.ps1" in text


def test_command_center_watchdog_registrar_stays_canonical() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert "Expected canonical workspace root named EvolutionaryTradingAlgo" in text
    assert "Resolve-Path -LiteralPath $Root" in text
    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
