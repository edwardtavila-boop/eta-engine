# EVOLUTIONARY TRADING ALGO // plan_vps_root_reconciliation.ps1
# Converts the read-only VPS root inventory into a reviewable reconciliation
# plan. This script only reads inventory data and writes plan artifacts.

[CmdletBinding()]
param(
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$InventoryPath = "",
    [string]$InventoryJson = "",
    [string]$OutputDir = "",
    [switch]$NoWrite
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    if (
        $resolved -ne "C:\EvolutionaryTradingAlgo" -and
        -not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing non-canonical ETA path: $Path"
    }
    return $resolved
}

function Get-Count {
    param(
        [object]$Node,
        [string]$Name
    )
    if ($null -eq $Node) {
        return 0
    }
    $prop = $Node.PSObject.Properties[$Name]
    if ($null -eq $prop -or $null -eq $prop.Value) {
        return 0
    }
    $countProp = $prop.Value.PSObject.Properties["count"]
    if ($null -eq $countProp -or $null -eq $countProp.Value) {
        return 0
    }
    return [int]$countProp.Value
}

function Get-Sample {
    param(
        [object]$Node,
        [string]$Name
    )
    if ($null -eq $Node) {
        return @()
    }
    $prop = $Node.PSObject.Properties[$Name]
    if ($null -eq $prop -or $null -eq $prop.Value) {
        return @()
    }
    $sampleProp = $prop.Value.PSObject.Properties["sample"]
    if ($null -eq $sampleProp -or $null -eq $sampleProp.Value) {
        return @()
    }
    return @($sampleProp.Value)
}

function New-PlanStep {
    param(
        [string]$Id,
        [string]$Title,
        [string]$Risk,
        [string]$Decision,
        [string]$Action,
        [string[]]$Evidence = @()
    )
    return [ordered]@{
        id = $Id
        title = $Title
        risk = $Risk
        decision = $Decision
        action = $Action
        evidence = @($Evidence)
    }
}

function Get-LeafName {
    param([Parameter(Mandatory = $true)][string]$Path)
    $normalized = ($Path -replace "/", "\").Trim()
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return ""
    }
    return [System.IO.Path]::GetFileName($normalized)
}

function New-SourceReviewItem {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ChangeClass
    )
    $basename = Get-LeafName -Path $Path
    if ([string]::IsNullOrWhiteSpace($basename)) {
        return $null
    }

    $area = "root_source_governance"
    $rationale = "Tracked root source/governance $ChangeClass needs manual review before root branch updates."
    $changeSummary = "Tracked root source/governance change requires manual review before root branch updates."
    $verificationCommand = "git -C C:\EvolutionaryTradingAlgo diff -- $Path"
    $verificationGoal = "Inspect the exact tracked root diff before deciding whether to preserve, commit, or revert it."
    $verificationMode = "read_only_diff"
    $verificationSideEffects = "none"
    $suggestedDecision = if ($ChangeClass -eq "deleted") {
        "confirm_delete_or_restore_before_root_update"
    }
    else {
        "preserve_if_intentional_or_commit_before_root_update"
    }

    switch ($basename) {
        "command-center-watchdog-status.ps1" {
            $area = "operator_watchdog_truth"
            $rationale = "Tracks Command Center watchdog truth, task contract drift, and public route health semantics."
            $changeSummary = "Adds runtime dependency-gap probing, watchdog/dashboard task-contract checks, and display-safe operator summaries."
            $verificationCommand = "powershell -ExecutionPolicy Bypass -File .\scripts\command-center-watchdog-status.ps1 -Json"
            $verificationGoal = "Confirm the live watchdog contract, task-contract status, and display-safe operator summary on the authoritative host."
            $verificationMode = "status_probe"
            $verificationSideEffects = "refreshes the canonical watchdog receipt"
            $suggestedDecision = "preserve_if_it_matches_live_watchdog_contract"
            break
        }
        "reload-operator-service.ps1" {
            $area = "operator_reload_runtime"
            $rationale = "Controls canonical 8421 reload behavior and the task-owned Command Center runtime path on the VPS."
            $changeSummary = "Replaces brittle raw 8421 waits with unified local-truth verification and uses the canonical runtime Python."
            $verificationCommand = "powershell -ExecutionPolicy Bypass -File .\scripts\reload-operator-service.ps1 -SkipPublicCheck -SkipWatchdogRegistration -NoAutoElevate -TimeoutSeconds 30"
            $verificationGoal = "Confirm the VPS reload flow exits cleanly and re-verifies the local 8421 operator contract."
            $verificationMode = "runtime_reload"
            $verificationSideEffects = "re-registers dashboard tasks, refreshes live service wiring, and reloads the 8421 operator surface"
            $suggestedDecision = "preserve_if_it_matches_live_8421_reload_flow"
            break
        }
        "verify_operator_source_of_truth.py" {
            $area = "operator_contract_verification"
            $rationale = "Verifies operator truth surfaces, including transient endpoint retries and upstream failure classification."
            $changeSummary = "Adds transient endpoint retries, upstream 5xx classification, and display-summary leakage checks."
            $verificationCommand = "python .\scripts\verify_operator_source_of_truth.py --base-url http://127.0.0.1:8421 --timeout 30"
            $verificationGoal = "Confirm the canonical local operator verifier accepts the live 8421 payloads and failure classification contract."
            $verificationMode = "read_only_contract_probe"
            $verificationSideEffects = "none"
            $suggestedDecision = "preserve_if_it_matches_current_operator_contract"
            break
        }
    }

    return [ordered]@{
        path = $Path
        basename = $basename
        change_class = $ChangeClass
        area = $area
        rationale = $rationale
        change_summary = $changeSummary
        verification_command = $verificationCommand
        verification_goal = $verificationGoal
        verification_mode = $verificationMode
        verification_side_effects = $verificationSideEffects
        suggested_decision = $suggestedDecision
    }
}

function New-CompanionReviewItem {
    param([Parameter(Mandatory = $true)][string]$Entry)
    $parts = $Entry -split ":", 2
    $target = $parts[0].Trim()
    if ([string]::IsNullOrWhiteSpace($target)) {
        return $null
    }
    $reason = if ($parts.Length -gt 1) { $parts[1].Trim() } else { "dirty_worktree" }
    $rationale = "Dirty/diverged companion repo state must be committed, preserved, or intentionally pinned before the root gitlink is updated."
    if ($target -eq "eta_engine") {
        $rationale = "The authoritative ETA child repo is dirty/diverged and needs a child-repo decision before the root gitlink is updated."
    }
    return [ordered]@{
        target = $target
        reason = $reason
        rationale = $rationale
        suggested_decision = "commit_preserve_or_pin_before_root_update"
    }
}

$RootFull = Assert-CanonicalEtaPath -Path $Root
if (-not $InventoryPath.Trim()) {
    $InventoryPath = Join-Path $RootFull "var\eta_engine\state\vps_root_dirty_inventory.json"
}
if (-not $OutputDir.Trim()) {
    $OutputDir = Join-Path $RootFull "var\eta_engine\state"
}

if ($InventoryJson.Trim()) {
    $inventory = $InventoryJson | ConvertFrom-Json
    $InventoryFull = "<inline>"
}
else {
    $InventoryFull = Assert-CanonicalEtaPath -Path $InventoryPath
    if (-not (Test-Path -LiteralPath $InventoryFull)) {
        throw "Missing VPS root inventory: $InventoryFull"
    }
    $inventory = Get-Content -LiteralPath $InventoryFull -Raw | ConvertFrom-Json
}

$OutputFull = Assert-CanonicalEtaPath -Path $OutputDir

$counts = $inventory.counts
$deleted = $inventory.deleted_tracked
$modified = $inventory.modified_tracked
$untracked = $inventory.untracked
$submodules = $inventory.submodules

$sourceDeleted = Get-Count -Node $deleted -Name "source_or_governance"
$sourceModified = Get-Count -Node $modified -Name "source_or_governance"
$unknownDeleted = Get-Count -Node $deleted -Name "unknown"
$generatedDeleted = Get-Count -Node $deleted -Name "generated_market_or_research_artifact"
$generatedUntracked = Get-Count -Node $untracked -Name "generated_market_or_research_artifact"
$localBackupUntracked = Get-Count -Node $untracked -Name "local_backup_artifact"
$localDiagnosticUntracked = Get-Count -Node $untracked -Name "local_diagnostic_artifact"
$sourceUntracked = Get-Count -Node $untracked -Name "source_or_governance"
$submoduleDrift = 0
if ($null -ne $counts -and $null -ne $counts.PSObject.Properties["submodule_drift"]) {
    $submoduleDrift = [int]$counts.submodule_drift
}
$submoduleUninitialized = 0
if ($null -ne $counts -and $null -ne $counts.PSObject.Properties["submodule_uninitialized"]) {
    $submoduleUninitialized = [int]$counts.submodule_uninitialized
}
$optionalDormantSubmodules = 0
if ($null -ne $counts -and $null -ne $counts.PSObject.Properties["optional_dormant_submodules"]) {
    $optionalDormantSubmodules = [int]$counts.optional_dormant_submodules
}
$optionalDormantDeletedTracked = 0
if ($null -ne $counts -and $null -ne $counts.PSObject.Properties["optional_dormant_deleted_tracked"]) {
    $optionalDormantDeletedTracked = [int]$counts.optional_dormant_deleted_tracked
}
$dirtyCompanionRepos = 0
if ($null -ne $counts -and $null -ne $counts.PSObject.Properties["dirty_companion_repos"]) {
    $dirtyCompanionRepos = [int]$counts.dirty_companion_repos
}
$dirtyCompanionSample = @()
if (
    $null -ne $submodules -and
    $null -ne $submodules.PSObject.Properties["dirty_worktree_sample"] -and
    $null -ne $submodules.dirty_worktree_sample
) {
    $dirtyCompanionSample = @($submodules.dirty_worktree_sample | ForEach-Object {
        if ($null -ne $_.PSObject.Properties["line"]) {
            "$($_.path):$($_.meaning)"
        }
        else {
            "$_"
        }
    })
}
$sourceReviewItems = New-Object System.Collections.Generic.List[object]
foreach ($path in (Get-Sample -Node $deleted -Name "source_or_governance")) {
    $item = New-SourceReviewItem -Path $path -ChangeClass "deleted"
    if ($null -ne $item) {
        [void]$sourceReviewItems.Add($item)
    }
}
foreach ($path in (Get-Sample -Node $modified -Name "source_or_governance")) {
    $item = New-SourceReviewItem -Path $path -ChangeClass "modified"
    if ($null -ne $item) {
        [void]$sourceReviewItems.Add($item)
    }
}
$companionReviewItems = New-Object System.Collections.Generic.List[object]
foreach ($entry in $dirtyCompanionSample) {
    $item = New-CompanionReviewItem -Entry "$entry"
    if ($null -ne $item) {
        [void]$companionReviewItems.Add($item)
    }
}
$sourceReviewItemsArray = @($sourceReviewItems | ForEach-Object { $_ })
$companionReviewItemsArray = @($companionReviewItems | ForEach-Object { $_ })

$risk = "low"
if ($sourceDeleted -gt 0 -or $unknownDeleted -gt 0) {
    $risk = "high"
}
elseif ($sourceModified -gt 0 -or $submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0 -or $sourceUntracked -gt 0) {
    $risk = "medium"
}

$hasTrackedSourceRisk = $sourceDeleted -gt 0 -or $unknownDeleted -gt 0
$hasTrackedSourceDrift = $sourceModified -gt 0
$hasCompanionRisk = $submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0
$hasGeneratedOrLocalArtifactRisk = (
    $generatedDeleted -gt 0 -or
    $generatedUntracked -gt 0 -or
    $localBackupUntracked -gt 0 -or
    $localDiagnosticUntracked -gt 0
)
$hasRootReconciliationRisk = (
    $hasTrackedSourceRisk -or
    $hasTrackedSourceDrift -or
    $hasCompanionRisk -or
    $sourceUntracked -gt 0 -or
    $hasGeneratedOrLocalArtifactRisk
)

$freezeTitle = "Root cleanup remains locked; no dirty work detected"
$freezeAction = "No root cleanup is needed; keep destructive cleanup disabled and continue read-only inventory and live probes."
if ($hasTrackedSourceRisk) {
    $freezeTitle = "Freeze root cleanup until source deletions are reviewed"
    $freezeAction = "Keep root cleanup disabled; preserve the current VPS working tree until the operator approves a source restore plan."
}
elseif ($hasTrackedSourceDrift -and $hasCompanionRisk) {
    $freezeTitle = "Freeze root cleanup until tracked source changes and companion drift are reviewed"
    $freezeAction = "Keep root cleanup disabled; review tracked root source/governance modifications alongside dirty companion worktrees before branch updates or cleanup."
}
elseif ($hasTrackedSourceDrift) {
    $freezeTitle = "Freeze root cleanup until tracked source changes are reviewed"
    $freezeAction = "Keep root cleanup disabled; review tracked root source/governance modifications before branch updates or cleanup."
}
elseif ($hasCompanionRisk) {
    $freezeTitle = "Freeze root cleanup until companion repo drift is reviewed"
    $freezeAction = "Keep root cleanup disabled; preserve dirty companion worktrees and current submodule pins until each companion repo is committed, intentionally pinned, or otherwise approved."
}
elseif ($sourceUntracked -gt 0) {
    $freezeTitle = "Freeze root cleanup until untracked source files are classified"
    $freezeAction = "Keep root cleanup disabled; classify source/governance untracked files before generated-artifact cleanup or branch updates."
}
elseif ($hasGeneratedOrLocalArtifactRisk) {
    $freezeTitle = "Freeze root cleanup until generated artifacts are classified"
    $freezeAction = "Keep root cleanup disabled; archive or ignore generated/local artifacts only after source and companion repo state is confirmed safe."
}
$freezeStepDecision = if ($hasRootReconciliationRisk) { "manual_review_required" } else { "clear" }
$freezeStepRisk = if ($hasRootReconciliationRisk) { $risk } else { "low" }

$sourceStepTitle = "Confirm no tracked source or governance deletions"
$sourceStepRisk = "low"
$sourceStepDecision = "clear"
$sourceStepAction = "No tracked source/governance deletions were found in the current inventory; continue with companion repo and generated-artifact review."
if ($hasTrackedSourceRisk) {
    $sourceStepTitle = "Review tracked source and governance deletions first"
    $sourceStepRisk = "high"
    $sourceStepDecision = "manual_review_required"
    $sourceStepAction = "Compare the deleted tracked source/governance files against the intended root branch before restoring or replacing the VPS root."
}
elseif ($hasTrackedSourceDrift) {
    $sourceStepTitle = "Review tracked source and governance modifications"
    $sourceStepRisk = "medium"
    $sourceStepDecision = "manual_review_required"
    $sourceStepAction = "Review tracked root source/governance modifications and decide whether they should be committed, preserved as local runtime edits, or reverted before branch updates."
}

$submoduleStepDecision = if ($hasCompanionRisk) { "manual_review_required" } else { "clear" }
$submoduleStepRisk = if ($hasCompanionRisk) { "medium" } else { "low" }
$submoduleStepAction = if ($hasCompanionRisk) {
    if ($optionalDormantSubmodules -gt 0) {
        "Review dirty companion worktrees and submodule drift; optional dormant submodules can remain pinned for VPS runtime while each active companion repo is committed, preserved, or intentionally pinned."
    }
    else {
        "Review dirty companion worktrees and submodule drift; choose whether each companion repo should follow the root branch, its own live branch, or remain pinned for VPS runtime."
    }
}
else {
    if ($submoduleUninitialized -gt 0) {
        "No dirty companion worktrees or submodule pointer drift were found; optional dormant submodules are uninitialized and can remain pinned for VPS runtime."
    }
    else {
        "No dirty companion worktrees or submodule drift were found in the current inventory."
    }
}

$generatedStepDecision = if ($hasGeneratedOrLocalArtifactRisk -or $sourceUntracked -gt 0) { "manual_review_required" } else { "clear" }
$generatedStepRisk = if ($hasGeneratedOrLocalArtifactRisk -or $sourceUntracked -gt 0) { "medium" } else { "low" }
$generatedStepAction = if ($hasGeneratedOrLocalArtifactRisk -or $sourceUntracked -gt 0) {
    "Archive or ignore generated market/research artifacts, local backup artifacts, and local diagnostics only after source/governance files are safe."
}
else {
    "No generated/local artifact cleanup is queued by the current inventory."
}

$approvalGates = [ordered]@{
    cleanup = "blocked_until_manual_approval"
    branch_update = if ($hasTrackedSourceRisk -or $hasTrackedSourceDrift -or $sourceUntracked -gt 0) { "blocked_until_source_review" } elseif ($hasCompanionRisk) { "blocked_until_companion_review" } else { "clear" }
    submodule_alignment = if ($submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0) { "manual_review_required" } else { "clear" }
    generated_artifact_cleanup = if ($hasGeneratedOrLocalArtifactRisk -or $sourceUntracked -gt 0) { "blocked_until_source_safe" } else { "clear" }
    credential_rotation = "reserved_for_go_live"
}

$recommendedAction = "Rerun the read-only inventory and live probes; no root cleanup is authorized by this plan."
if ($sourceDeleted -gt 0 -or $unknownDeleted -gt 0) {
    $recommendedAction = "Review tracked source/governance deletions before branch updates, cleanup, or root replacement."
}
elseif ($sourceModified -gt 0 -and ($submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0)) {
    $recommendedAction = "Review tracked root source/governance modifications and dirty companion worktrees before updating the superproject root."
}
elseif ($sourceModified -gt 0) {
    $recommendedAction = "Review tracked root source/governance modifications before branch updates or cleanup."
}
elseif ($submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0) {
    $recommendedAction = "Review dirty companion worktrees and commit, preserve, or intentionally pin them before updating the superproject root."
}
elseif ($sourceUntracked -gt 0) {
    $recommendedAction = "Classify source/governance untracked files before any generated-artifact cleanup."
}

$steps = @(
    New-PlanStep `
        -Id "freeze-and-backup" `
        -Title $freezeTitle `
        -Risk $freezeStepRisk `
        -Decision $freezeStepDecision `
        -Action $freezeAction `
        -Evidence @("inventory=$InventoryFull", "status_count=$($counts.status)")
    New-PlanStep `
        -Id "restore-source-governance" `
        -Title $sourceStepTitle `
        -Risk $sourceStepRisk `
        -Decision $sourceStepDecision `
        -Action $sourceStepAction `
        -Evidence (@(
            "source_or_governance_deleted=$sourceDeleted",
            "source_or_governance_modified=$sourceModified"
        ) + (Get-Sample -Node $deleted -Name "source_or_governance") + (Get-Sample -Node $modified -Name "source_or_governance"))
    New-PlanStep `
        -Id "align-submodules" `
        -Title "Align companion repositories after source state is understood" `
        -Risk $submoduleStepRisk `
        -Decision $submoduleStepDecision `
        -Action $submoduleStepAction `
        -Evidence (@(
            "submodule_drift=$submoduleDrift",
            "submodule_uninitialized=$submoduleUninitialized",
            "optional_dormant_submodules=$optionalDormantSubmodules",
            "optional_dormant_deleted_tracked=$optionalDormantDeletedTracked",
            "dirty_companion_repos=$dirtyCompanionRepos"
        ) + @($submodules.sample) + @($dirtyCompanionSample))
    New-PlanStep `
        -Id "classify-generated-artifacts" `
        -Title "Separate generated market/research artifacts from source" `
        -Risk $generatedStepRisk `
        -Decision $generatedStepDecision `
        -Action $generatedStepAction `
        -Evidence (@("generated_deleted=$generatedDeleted", "generated_untracked=$generatedUntracked", "local_backup_untracked=$localBackupUntracked", "local_diagnostic_untracked=$localDiagnosticUntracked") + (Get-Sample -Node $untracked -Name "generated_market_or_research_artifact") + (Get-Sample -Node $untracked -Name "local_backup_artifact") + (Get-Sample -Node $untracked -Name "local_diagnostic_artifact"))
    New-PlanStep `
        -Id "verify-after-reconciliation" `
        -Title "Verify live ops after any approved reconciliation" `
        -Risk "medium" `
        -Decision "verification_required" `
        -Action "After any approved manual root work, rerun the dashboard sync, bot-fleet probe, master status probe, and read-only root inventory." `
        -Evidence @("dashboard_probe=/api/bot-fleet", "master_probe=/api/master/status")
)

$plan = [ordered]@{
    status = "ok"
    mode = "review_plan_only"
    root = $RootFull
    inventory_path = $InventoryFull
    output_dir = $OutputFull
    risk_level = $risk
    cleanup_allowed = $false
    destructive_actions_performed = $false
    recommended_action = $recommendedAction
    approval_gates = $approvalGates
    counts = $counts
    summary = [ordered]@{
        source_or_governance_deleted = $sourceDeleted
        unknown_deleted = $unknownDeleted
        generated_deleted = $generatedDeleted
        generated_untracked = $generatedUntracked
        local_backup_untracked = $localBackupUntracked
        local_diagnostic_untracked = $localDiagnosticUntracked
        source_or_governance_modified = $sourceModified
        source_or_governance_untracked = $sourceUntracked
        submodule_drift = $submoduleDrift
        submodule_uninitialized = $submoduleUninitialized
        optional_dormant_submodules = $optionalDormantSubmodules
        optional_dormant_deleted_tracked = $optionalDormantDeletedTracked
        dirty_companion_repos = $dirtyCompanionRepos
    }
    source_review_items = $sourceReviewItemsArray
    companion_review_items = $companionReviewItemsArray
    steps = $steps
}

$markdown = @(
    "# VPS Root Reconciliation Plan"
    ""
    "- Status: $($plan.status)"
    "- Mode: $($plan.mode)"
    "- Root: $($plan.root)"
    "- Inventory: $($plan.inventory_path)"
    "- Risk: $($plan.risk_level)"
    "- Cleanup allowed: false"
    "- Destructive actions performed: false"
    "- Recommended action: $recommendedAction"
    ""
    "## Summary"
    ""
    "- Source/governance tracked deletions: $sourceDeleted"
    "- Unknown tracked deletions: $unknownDeleted"
    "- Generated tracked deletions: $generatedDeleted"
    "- Generated untracked artifacts: $generatedUntracked"
    "- Local backup untracked artifacts: $localBackupUntracked"
    "- Local diagnostic untracked artifacts: $localDiagnosticUntracked"
    "- Source/governance untracked files: $sourceUntracked"
    "- Submodule drift entries: $submoduleDrift"
    "- Optional dormant submodules: $optionalDormantSubmodules"
    "- Optional dormant deleted tracked rows excluded from review: $optionalDormantDeletedTracked"
    "- Dirty companion worktrees: $dirtyCompanionRepos"
    ""
    "## Review focus"
    ""
    "- Source review items: $($sourceReviewItemsArray.Count)"
    "- Companion review items: $($companionReviewItemsArray.Count)"
    ""
    "## Approval gates"
    ""
    "- Cleanup: $($approvalGates.cleanup)"
    "- Branch update: $($approvalGates.branch_update)"
    "- Submodule alignment: $($approvalGates.submodule_alignment)"
    "- Generated artifact cleanup: $($approvalGates.generated_artifact_cleanup)"
    "- Credential rotation: $($approvalGates.credential_rotation)"
    ""
    "## Steps"
)
foreach ($step in $steps) {
    $markdown += ""
    $markdown += "### $($step.id): $($step.title)"
    $markdown += ""
    $markdown += "- Risk: $($step.risk)"
    $markdown += "- Decision: $($step.decision)"
    $markdown += "- Action: $($step.action)"
    if ($step.evidence.Count -gt 0) {
        $markdown += "- Evidence:"
        foreach ($item in $step.evidence) {
            $markdown += "  - $item"
        }
    }
}
if ($sourceReviewItemsArray.Count -gt 0) {
    $markdown += ""
    $markdown += "## Source review items"
    foreach ($item in $sourceReviewItemsArray) {
        $markdown += ""
        $markdown += "- $($item.basename) [$($item.change_class)]"
        $markdown += "  - Path: $($item.path)"
        $markdown += "  - Area: $($item.area)"
        $markdown += "  - Rationale: $($item.rationale)"
        $markdown += "  - Suggested decision: $($item.suggested_decision)"
    }
}
if ($companionReviewItemsArray.Count -gt 0) {
    $markdown += ""
    $markdown += "## Companion review items"
    foreach ($item in $companionReviewItemsArray) {
        $markdown += ""
        $markdown += "- $($item.target)"
        $markdown += "  - Reason: $($item.reason)"
        $markdown += "  - Rationale: $($item.rationale)"
        $markdown += "  - Suggested decision: $($item.suggested_decision)"
    }
}
$markdownText = ($markdown -join [Environment]::NewLine)

if (-not $NoWrite) {
    if (-not (Test-Path -LiteralPath $OutputFull)) {
        New-Item -ItemType Directory -Path $OutputFull -Force | Out-Null
    }
    $jsonPath = Join-Path $OutputFull "vps_root_reconciliation_plan.json"
    $mdPath = Join-Path $OutputFull "vps_root_reconciliation_plan.md"
    ($plan | ConvertTo-Json -Depth 8 -Compress) | Set-Content -LiteralPath $jsonPath -Encoding UTF8
    $markdownText | Set-Content -LiteralPath $mdPath -Encoding UTF8
    $plan["artifacts"] = [ordered]@{
        json = $jsonPath
        markdown = $mdPath
    }
}

$plan | ConvertTo-Json -Depth 8 -Compress
