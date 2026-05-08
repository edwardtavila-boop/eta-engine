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
$sourceUntracked = Get-Count -Node $untracked -Name "source_or_governance"
$submoduleDrift = 0
if ($null -ne $counts -and $null -ne $counts.PSObject.Properties["submodule_drift"]) {
    $submoduleDrift = [int]$counts.submodule_drift
}

$risk = "low"
if ($sourceDeleted -gt 0 -or $unknownDeleted -gt 0) {
    $risk = "high"
}
elseif ($submoduleDrift -gt 0 -or $sourceUntracked -gt 0) {
    $risk = "medium"
}

$steps = @(
    New-PlanStep `
        -Id "freeze-and-backup" `
        -Title "Freeze root cleanup until source deletions are reviewed" `
        -Risk $risk `
        -Decision "manual_review_required" `
        -Action "Keep root cleanup disabled; preserve the current VPS working tree until the operator approves a source restore plan." `
        -Evidence @("inventory=$InventoryFull", "status_count=$($counts.status)")
    New-PlanStep `
        -Id "restore-source-governance" `
        -Title "Review tracked source and governance deletions first" `
        -Risk "high" `
        -Decision "manual_review_required" `
        -Action "Compare the deleted tracked source/governance files against the intended root branch before restoring or replacing the VPS root." `
        -Evidence (@("source_or_governance_deleted=$sourceDeleted") + (Get-Sample -Node $deleted -Name "source_or_governance"))
    New-PlanStep `
        -Id "align-submodules" `
        -Title "Align companion repositories after source state is understood" `
        -Risk "medium" `
        -Decision "manual_review_required" `
        -Action "Review submodule drift and choose whether each companion repo should follow the root branch, its own live branch, or remain pinned for VPS runtime." `
        -Evidence (@("submodule_drift=$submoduleDrift") + @($submodules.sample))
    New-PlanStep `
        -Id "classify-generated-artifacts" `
        -Title "Separate generated market/research artifacts from source" `
        -Risk "medium" `
        -Decision "manual_review_required" `
        -Action "Archive or ignore generated market/research artifacts and local backup artifacts only after source/governance files are safe." `
        -Evidence (@("generated_deleted=$generatedDeleted", "generated_untracked=$generatedUntracked", "local_backup_untracked=$localBackupUntracked") + (Get-Sample -Node $untracked -Name "generated_market_or_research_artifact") + (Get-Sample -Node $untracked -Name "local_backup_artifact"))
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
    counts = $counts
    summary = [ordered]@{
        source_or_governance_deleted = $sourceDeleted
        unknown_deleted = $unknownDeleted
        generated_deleted = $generatedDeleted
        generated_untracked = $generatedUntracked
        local_backup_untracked = $localBackupUntracked
        source_or_governance_untracked = $sourceUntracked
        submodule_drift = $submoduleDrift
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
    ""
    "## Summary"
    ""
    "- Source/governance tracked deletions: $sourceDeleted"
    "- Unknown tracked deletions: $unknownDeleted"
    "- Generated tracked deletions: $generatedDeleted"
    "- Generated untracked artifacts: $generatedUntracked"
    "- Local backup untracked artifacts: $localBackupUntracked"
    "- Source/governance untracked files: $sourceUntracked"
    "- Submodule drift entries: $submoduleDrift"
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
