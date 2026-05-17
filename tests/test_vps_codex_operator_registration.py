from __future__ import annotations

from pathlib import Path

ETA_ROOT = Path(__file__).resolve().parents[1]


def test_codex_operator_registrar_uses_canonical_state_paths() -> None:
    text = (ETA_ROOT / "deploy" / "scripts" / "register_codex_operator_task.ps1").read_text(encoding="utf-8")

    assert "ETA-Codex-Overnight-Operator" in text
    assert "ETA-ThreeAI-Sync" in text
    assert "var\\eta_engine\\state" in text
    assert "codex_overnight_operator.py" in text
    assert "three_ai_sync.py" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 30)" in text


def test_vps_bootstrap_invokes_codex_operator_registrar() -> None:
    text = (ETA_ROOT / "deploy" / "vps_bootstrap.ps1").read_text(encoding="utf-8")

    assert "SkipCodexOperator" in text
    assert "register_codex_operator_task.ps1" in text
    assert "ETA-Codex-Overnight-Operator" in text
    assert "ETA-ThreeAI-Sync" in text


def test_vps_bootstrap_registers_fm_status_server() -> None:
    text = (ETA_ROOT / "deploy" / "vps_bootstrap.ps1").read_text(encoding="utf-8")

    assert 'Name="FmStatusServer"' in text
    assert 'Xml="FmStatusServer.xml"' in text
    assert r"deploy\FmStatusServer.xml" in text
    assert "Update-FmStatusServiceXmlExecutable" in text
    assert "Resolve-EtaPython" in text
    assert "127.0.0.1:8422" in text
