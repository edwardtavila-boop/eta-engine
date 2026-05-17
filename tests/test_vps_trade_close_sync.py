from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "sync_trade_closes_from_vps.ps1"


def test_trade_close_sync_script_targets_canonical_vps_and_local_paths() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'Resolve-Setting -Value $SshHost -EnvName "FIRM_VPS_HOST" -DefaultValue "93.120.38.156"' in text
    assert 'Resolve-Setting -Value $SshUser -EnvName "FIRM_VPS_USER" -DefaultValue "codex-admin"' in text
    assert 'Resolve-Setting -Value $SshKeyPath -EnvName "FIRM_VPS_SSH_KEY_PATH" -DefaultValue ""' in text
    assert 'Join-Path $WorkspaceRoot "var\\eta_engine\\state\\jarvis_intel\\trade_closes.jsonl"' in text
    assert 'Join-Path $RemoteRepoRoot "var\\eta_engine\\state\\jarvis_intel\\trade_closes.jsonl"' in text
    assert "Copy-FromRemote" in text
    assert "scp failed for $RemotePath" in text
    assert ".bak_" in text


def test_trade_close_sync_script_refreshes_local_receipts() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert '"eta_engine.scripts.closed_trade_ledger"' in text
    assert '"eta_engine.scripts.diamond_edge_audit"' in text
    assert '"eta_engine.scripts.diamond_leaderboard"' in text
    assert '"eta_engine.scripts.diamond_retune_status"' in text
    assert '"eta_engine.scripts.diamond_ops_dashboard"' in text
    assert '& $PythonExe -m $ModuleName --json | Out-Null' in text
    assert 'AllowNonZeroExit = $true' in text
    assert 'non-zero verdict preserved for {0} (exit={1})' in text


def test_trade_close_sync_script_avoids_legacy_write_roots() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
