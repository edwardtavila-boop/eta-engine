from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSPECT = ROOT / "deploy" / "scripts" / "inspect_vps_root_dirty.ps1"
PLAN = ROOT / "deploy" / "scripts" / "plan_vps_root_reconciliation.ps1"
SYNC = ROOT / "deploy" / "scripts" / "sync_dashboard_api_live.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_vps_ops_hardening_audit.cmd"


def test_vps_root_inventory_classifies_local_backups_outside_source_risk() -> None:
    text = INSPECT.read_text(encoding="utf-8")

    assert 'return "local_backup_artifact"' in text
    assert 'return "local_diagnostic_artifact"' in text
    assert "Get-DirtyCompanionStatus" in text
    assert "dirty_companion_repos" in text
    assert "dirty_worktree_sample" in text
    assert "submodule_uninitialized" in text
    assert "uninitialized_count" in text
    assert 'Where-Object { $_ -match "^-" }' in text
    assert 'Where-Object { $_ -match "^\\+" }' in text
    assert r"\.bak" in text
    assert "scripts/_check_" in text
    assert "cleanup_allowed = $false" in text
    assert "destructive_actions_performed = $false" in text


def test_vps_root_plan_surfaces_backup_artifacts_separately() -> None:
    text = PLAN.read_text(encoding="utf-8")

    assert 'Get-Count -Node $untracked -Name "local_backup_artifact"' in text
    assert 'Get-Count -Node $untracked -Name "local_diagnostic_artifact"' in text
    assert "local_backup_untracked" in text
    assert "local_diagnostic_untracked" in text
    assert "dirty_companion_repos" in text
    assert "submodule_uninitialized" in text
    assert "optional dormant submodules are uninitialized" in text
    assert "Dirty companion worktrees" in text
    assert "dirty_worktree_sample" in text
    assert "Freeze root cleanup until companion repo drift is reviewed" in text
    assert "Confirm no tracked source or governance deletions" in text
    assert "blocked_until_companion_review" in text
    assert "Local backup untracked artifacts" in text
    assert "Local diagnostic untracked artifacts" in text
    assert "cleanup_allowed = $false" in text
    assert "destructive_actions_performed = $false" in text
    assert "approval_gates = $approvalGates" in text
    assert 'cleanup = "blocked_until_manual_approval"' in text
    assert "Root cleanup remains locked; no dirty work detected" in text
    assert "$freezeStepDecision" in text
    assert "blocked_until_source_review" in text
    assert 'credential_rotation = "reserved_for_go_live"' in text
    assert "Recommended action" in text


def test_dashboard_sync_refreshes_read_only_root_review_artifacts() -> None:
    text = SYNC.read_text(encoding="utf-8")

    assert "SkipRootReviewRefresh" in text
    assert "inspect_vps_root_dirty.ps1" in text
    assert "plan_vps_root_reconciliation.ps1" in text
    assert "vps_root_dirty_inventory.json" in text
    assert "vps_root_reconciliation_plan.json" in text
    assert "root_review_refresh" in text
    assert "cleanup_allowed" in text
    assert "destructive_actions_performed" in text


def test_vps_ops_hardening_runner_refreshes_root_review_artifacts() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert "inspect_vps_root_dirty.ps1" in text
    assert "plan_vps_root_reconciliation.ps1" in text
    assert "vps_root_dirty_inventory.json" in text
    assert "vps_root_reconciliation_plan.json" in text
    assert "root_review_refresh" in text
    assert "inspect_exit_code=" in text
    assert "plan_exit_code=" in text
