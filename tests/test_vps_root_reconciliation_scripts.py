from pathlib import Path


def test_inspect_vps_root_dirty_disables_native_stderr_error_promotion() -> None:
    script = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\inspect_vps_root_dirty.ps1").read_text(
        encoding="utf-8"
    )

    assert "System.Diagnostics.ProcessStartInfo" in script
    assert "$psi.RedirectStandardError = $true" in script
    assert "$psi.Arguments = [string]::Join(\" \", $quotedArgs)" in script
    assert "Git stderr warnings" in script


def test_inspect_vps_root_dirty_tracks_optional_dormant_submodules_separately() -> None:
    script = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\inspect_vps_root_dirty.ps1").read_text(
        encoding="utf-8"
    )

    assert '$RuntimeOptionalSubmodules = @("mnq_backtest")' in script
    assert "optional_dormant_submodules" in script
    assert "optional_dormant_deleted_tracked" in script
    assert "optional_dormant_count" in script
    assert "$effectiveDeletedTracked = @(" in script
    assert "$effectivePorcelain = New-Object System.Collections.Generic.List[string]" in script
    assert "if ($isOptionalDormant -and $isUninitialized -and $trimmedStatus -eq \"D\")" in script


def test_plan_vps_root_reconciliation_mentions_optional_dormant_submodules() -> None:
    script = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\plan_vps_root_reconciliation.ps1").read_text(
        encoding="utf-8"
    )

    assert "optional_dormant_submodules" in script
    assert "optional_dormant_deleted_tracked" in script
    assert "optional dormant submodules can remain pinned for VPS runtime" in script
    assert '"optional_dormant_submodules=$optionalDormantSubmodules"' in script
    assert '"optional_dormant_deleted_tracked=$optionalDormantDeletedTracked"' in script
    assert '"- Optional dormant submodules: $optionalDormantSubmodules"' in script


def test_plan_vps_root_reconciliation_tracks_modified_source_governance() -> None:
    script = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\plan_vps_root_reconciliation.ps1").read_text(
        encoding="utf-8"
    )

    assert '$sourceModified = Get-Count -Node $modified -Name "source_or_governance"' in script
    assert "source_or_governance_modified" in script
    assert "Review tracked root source/governance modifications" in script
    assert "source_review_items" in script
    assert "command-center-watchdog-status.ps1" in script
    assert "operator_watchdog_truth" in script
    assert "operator_reload_runtime" in script
    assert "operator_contract_verification" in script
    assert "change_summary" in script
    assert "runtime dependency-gap probing" in script
    assert "unified local-truth verification" in script
    assert "display-summary leakage checks" in script
    assert "verification_mode" in script
    assert "verification_side_effects" in script
    assert "refreshes the canonical watchdog receipt" in script
    assert "re-registers dashboard tasks" in script
    assert '"read_only_contract_probe"' in script
    assert "companion_review_items" in script
    assert "commit_preserve_or_pin_before_root_update" in script


def test_verify_vps_root_reconciliation_records_observed_verdicts() -> None:
    script = Path(
        r"C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\verify_vps_root_reconciliation.ps1"
    ).read_text(encoding="utf-8")

    assert "vps_root_reconciliation_review.json" in script
    assert "preserve_candidate" in script
    assert "revisit_required" in script
    assert "companion_review_status" in script
    assert "companion_summary_line" in script
    assert "companion_results_by_target" in script
    assert "verified_review_required" in script
    assert "commit_preserve_or_pin_before_root_update" in script
    assert "command-center-watchdog-status.ps1" in script
    assert "reload-operator-service.ps1" in script
    assert "verify_operator_source_of_truth.py" in script
    assert "Companion eta_engine remains dirty/diverged" in script
    assert "tracked_change_count" in script
    assert "untracked_change_count" in script
    assert "tracked_files" in script
    assert "untracked_files" in script
    assert "change_groups" in script
    assert "change_group_summary" in script
    assert "Get-ObservedCompanionChangeGroups" in script
    assert "Get-CompanionChangeGroupName" in script
    assert "Get-ObservedCompanionFocusAreas" in script
    assert "Get-ObservedCompanionBatchAssessment" in script
    assert "Get-GitShortstatSummary" in script
    assert "focus_areas" in script
    assert "focus_area_summary" in script
    assert "batch_label" in script
    assert "batch_coherence" in script
    assert "batch_recommended_handling" in script
    assert "batch_summary" in script
    assert "Get-ObservedCompanionBatchScope" in script
    assert "batch_scope_paths" in script
    assert "batch_scope_command" in script
    assert "batch_scope_stat_command" in script
    assert "batch_scope_shortstat" in script
    assert "batch_scope_path_count" in script
    assert "decision_options" in script
    assert "decision_recommended_option" in script
    assert "decision_recommended_reason" in script
    assert "decision_basis" in script
    assert "decision_recommended_paths" in script
    assert "decision_recommended_commit_message" in script
    assert "decision_recommended_stage_command" in script
    assert "decision_recommended_commit_command" in script
    assert "decision_recommended_commands" in script
    assert "harden VPS runtime readiness and operator truth surfaces" in script
    assert "root_update_ready" in script
    assert "files?\\s+changed" in script
    assert "warning:" in script
    assert "ops_readiness_truth" in script
    assert "operator_surface_reconciliation" in script
    assert "runtime_hardening_batch" in script
    assert "preserve_or_commit_as_single_child_batch" in script
    assert 'inspection_command = "git -C $RepoPath diff -- $groupName"' in script
    assert "Watchdog status probe succeeded" in script
    assert "Reload wrapper completed successfully" in script
    assert "Reload wrapper timed out, but direct local operator verification passed immediately after reload." in script
    assert "Operator source verifier accepted the local 8421 payloads" in script
