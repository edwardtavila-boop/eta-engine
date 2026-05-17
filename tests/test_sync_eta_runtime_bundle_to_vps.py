from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "sync_eta_runtime_bundle_to_vps.ps1"


def test_runtime_bundle_sync_uses_state_backup_root_instead_of_inline_bak_files() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'Join-Path $RemoteRepoRoot "var\\eta_engine\\state\\codex_sync_backups\\eta_runtime_bundle"' in text
    assert "backup root :" in text
    assert "$backupBase = Join-Path $backupRoot $backupTs" in text
    assert "$backupPath = Join-Path $backupBase $relative" in text
    assert "Write-Output (''BACKED|'' + $file + ''|'' + $backupPath)" in text
    assert "$file + ''.bak_'' + $backupTs" not in text


def test_runtime_bundle_sync_keeps_vps_connection_defaults() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'Resolve-Setting -Value $SshHost -EnvName "FIRM_VPS_HOST" -DefaultValue "93.120.38.156"' in text
    assert 'Resolve-Setting -Value $SshUser -EnvName "FIRM_VPS_USER" -DefaultValue "codex-admin"' in text
    assert 'Resolve-Setting -Value $SshKeyPath -EnvName "FIRM_VPS_SSH_KEY_PATH" -DefaultValue ""' in text
