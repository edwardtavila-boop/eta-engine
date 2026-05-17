from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "register_cloudflare_named_tunnel_task.ps1"


def test_named_tunnel_registration_prefers_installed_service() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "PreferInstalledService = $true" in text
    assert "Get-CimInstance Win32_Service" in text
    assert "Name='Cloudflared'" in text
    assert "SkippedServiceOwner" in text
    assert "Sync-ShadowCloudflaredConfig" in text
    assert "ShadowConfigSynced" in text
    assert "Unregister-ScheduledTask -TaskName $TaskName" in text
    assert "Start-Service -Name $cloudflaredService.Name" in text


def test_named_tunnel_registration_avoids_legacy_paths() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text


def test_named_tunnel_registration_syncs_shadow_user_config() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'Join-Path $credentialDir "config.yml"' in text
    assert "credentials-file:" in text
    assert "Set-Content -LiteralPath $shadowConfigPath -Value $canonicalText -Encoding ASCII" in text
