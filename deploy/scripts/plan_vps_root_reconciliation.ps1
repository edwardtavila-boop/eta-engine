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

$risk = "low"
if ($sourceDeleted -gt 0 -or $unknownDeleted -gt 0) {
    $risk = "high"
}
elseif ($submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0 -or $sourceUntracked -gt 0) {
    $risk = "medium"
}

$hasTrackedSourceRisk = $sourceDeleted -gt 0 -or $unknownDeleted -gt 0
$hasCompanionRisk = $submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0
$hasGeneratedOrLocalArtifactRisk = (
    $generatedDeleted -gt 0 -or
    $generatedUntracked -gt 0 -or
    $localBackupUntracked -gt 0 -or
    $localDiagnosticUntracked -gt 0
)
$hasRootReconciliationRisk = (
    $hasTrackedSourceRisk -or
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

$submoduleStepDecision = if ($hasCompanionRisk) { "manual_review_required" } else { "clear" }
$submoduleStepRisk = if ($hasCompanionRisk) { "medium" } else { "low" }
$submoduleStepAction = if ($hasCompanionRisk) {
    "Review dirty companion worktrees and submodule drift; choose whether each companion repo should follow the root branch, its own live branch, or remain pinned for VPS runtime."
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
    branch_update = if ($hasTrackedSourceRisk -or $sourceUntracked -gt 0) { "blocked_until_source_review" } elseif ($hasCompanionRisk) { "blocked_until_companion_review" } else { "clear" }
    submodule_alignment = if ($submoduleDrift -gt 0 -or $dirtyCompanionRepos -gt 0) { "manual_review_required" } else { "clear" }
    generated_artifact_cleanup = if ($hasGeneratedOrLocalArtifactRisk -or $sourceUntracked -gt 0) { "blocked_until_source_safe" } else { "clear" }
    credential_rotation = "reserved_for_go_live"
}

$recommendedAction = "Rerun the read-only inventory and live probes; no root cleanup is authorized by this plan."
if ($sourceDeleted -gt 0 -or $unknownDeleted -gt 0) {
    $recommendedAction = "Review tracked source/governance deletions before branch updates, cleanup, or root replacement."
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
        -Evidence (@("source_or_governance_deleted=$sourceDeleted") + (Get-Sample -Node $deleted -Name "source_or_governance"))
    New-PlanStep `
        -Id "align-submodules" `
        -Title "Align companion repositories after source state is understood" `
        -Risk $submoduleStepRisk `
        -Decision $submoduleStepDecision `
        -Action $submoduleStepAction `
        -Evidence (@("submodule_drift=$submoduleDrift", "submodule_uninitialized=$submoduleUninitialized", "dirty_companion_repos=$dirtyCompanionRepos") + @($submodules.sample) + @($dirtyCompanionSample))
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
        source_or_governance_untracked = $sourceUntracked
        submodule_drift = $submoduleDrift
        submodule_uninitialized = $submoduleUninitialized
        dirty_companion_repos = $dirtyCompanionRepos
    }
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
    "- Optional dormant submodules: $submoduleUninitialized"
    "- Dirty companion worktrees: $dirtyCompanionRepos"
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
