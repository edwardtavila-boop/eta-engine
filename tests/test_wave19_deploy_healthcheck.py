from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "wave19_deploy.ps1"


def test_wave19_deploy_prefers_canonical_bootstrap_and_uses_wrapper_fallback_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "canonical VPS bootstrap" in text
    assert '=== Step 3/7: VPS Bootstrap ===' in text
    assert "compatibility wrapper fallback" in text
    assert '$bootstrapScript = "$EtaEngineDir\\deploy\\vps_bootstrap.ps1"' in text
    assert '$v2Script = "$EtaEngineDir\\deploy\\vps_bootstrap_v2.ps1"' in text


def test_wave19_deploy_healthcheck_validation_uses_remote_truth_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert '$healthOutDir = "$InstallRoot\\firm_command_center\\var\\health"' in text
    assert '"$EtaEngineDir\\scripts\\health_check.py"' in text
    assert "--allow-remote-supervisor-truth --allow-remote-retune-truth --output-dir $healthOutDir" in text
