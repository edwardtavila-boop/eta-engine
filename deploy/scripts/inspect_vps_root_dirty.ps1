# EVOLUTIONARY TRADING ALGO // inspect_vps_root_dirty.ps1
# Read-only VPS root drift inventory. This script never cleans, resets,
# checks out, removes, moves, or stages files. It exists to make the live
# root state safe to reason about before any human-approved reconciliation.

[CmdletBinding()]
param(
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [int]$SampleLimit = 25,
    [string]$OutputPath = ""
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

function Invoke-GitLines {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $lines = @(& git @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return @()
    }
    return @($lines | Where-Object { $_ -ne $null -and "$_".Trim() -ne "" })
}

function Get-PathCategory {
    param([Parameter(Mandatory = $true)][string]$Path)
    $p = ($Path -replace "\\", "/").Trim()

    if ($p -match "\.bak(\.|$)" -or $p -match "\.bak_[0-9]{8}_[0-9]{6}$") {
        return "local_backup_artifact"
    }
    if ($p -match "^(eta|eta_engine|firm|mnq_bot|mnq_backtest|mnq_eta_bot|tradingview-mcp|website)$") {
        return "submodule_or_companion_repo"
    }
    if ($p -match "^(data|lab|reports/lab_reports|reports/strategy_reviews|reports/verdict_patterns)/" -or
        $p -match "^dashboard\.html$" -or
        $p -match "^tmp_.*\.py$") {
        return "generated_market_or_research_artifact"
    }
    if ($p -match "^(var|logs|state|run|tmp)/") {
        return "runtime_state_or_log"
    }
    if ($p -match "^(\.github|apps|docs|legal|scripts|tests)/" -or
        $p -match "^(\.gitattributes|\.gitmodules|AGENTS\.md|CLAUDE\.md|DEPLOY\.md|INTEGRATION\.md|README\.md|ROADMAP\.md|SECURITY\.md|STATUS\.md|ACTIONS_FOR_EDWARD\.md)$") {
        return "source_or_governance"
    }
    return "unknown"
}

function New-CategorySummary {
    param(
        [AllowEmptyCollection()][string[]]$Paths = @(),
        [Parameter(Mandatory = $true)][int]$Limit
    )
    $groups = @{}
    foreach ($path in $Paths) {
        $category = Get-PathCategory -Path $path
        if (-not $groups.ContainsKey($category)) {
            $groups[$category] = New-Object System.Collections.Generic.List[string]
        }
        [void]$groups[$category].Add($path)
    }

    $summary = [ordered]@{}
    foreach ($category in ($groups.Keys | Sort-Object)) {
        $items = @($groups[$category])
        $summary[$category] = [ordered]@{
            count = $items.Count
            sample = @($items | Select-Object -First $Limit)
        }
    }
    return $summary
}

$RootFull = Assert-CanonicalEtaPath -Path $Root
$SampleLimit = [Math]::Max(1, $SampleLimit)

Push-Location -LiteralPath $RootFull
try {
    $branch = ((Invoke-GitLines -Arguments @("branch", "--show-current")) | Select-Object -First 1)
    $head = ((Invoke-GitLines -Arguments @("rev-parse", "--short", "HEAD")) | Select-Object -First 1)
    $porcelain = Invoke-GitLines -Arguments @("status", "--porcelain=v1")
    $deletedTracked = Invoke-GitLines -Arguments @("diff", "--name-only", "--diff-filter=D")
    $modifiedTracked = Invoke-GitLines -Arguments @("diff", "--name-only", "--diff-filter=M")
    $untracked = Invoke-GitLines -Arguments @("ls-files", "--others", "--exclude-standard")
    $submodules = Invoke-GitLines -Arguments @("submodule", "status")
    $submoduleDrift = @($submodules | Where-Object { $_ -match "^[+-]" })

    $deletedSummary = New-CategorySummary -Paths $deletedTracked -Limit $SampleLimit
    $modifiedSummary = New-CategorySummary -Paths $modifiedTracked -Limit $SampleLimit
    $untrackedSummary = New-CategorySummary -Paths $untracked -Limit $SampleLimit

    $sourceDeletedCount = 0
    if ($deletedSummary.Contains("source_or_governance")) {
        $sourceDeletedCount = [int]$deletedSummary["source_or_governance"].count
    }
    $unknownCount = 0
    if ($untrackedSummary.Contains("unknown")) {
        $unknownCount += [int]$untrackedSummary["unknown"].count
    }
    if ($modifiedSummary.Contains("unknown")) {
        $unknownCount += [int]$modifiedSummary["unknown"].count
    }

    $riskLevel = "low"
    $recommendedAction = "Generated/runtime drift only; review before cleanup."
    if ($sourceDeletedCount -gt 0 -or $deletedTracked.Count -gt 25) {
        $riskLevel = "high"
        $recommendedAction = "Manual reconciliation required before cleanup: tracked source/governance deletions are present."
    }
    elseif ($unknownCount -gt 0 -or $submoduleDrift.Count -gt 0) {
        $riskLevel = "medium"
        $recommendedAction = "Review unknown paths and submodule drift before cleanup."
    }

    $result = [ordered]@{
        status = "ok"
        mode = "read_only_inventory"
        root = $RootFull
        branch = "$branch".Trim()
        head = "$head".Trim()
        risk_level = $riskLevel
        recommended_action = $recommendedAction
        counts = [ordered]@{
            status = $porcelain.Count
            deleted_tracked = $deletedTracked.Count
            modified_tracked = $modifiedTracked.Count
            untracked = $untracked.Count
            submodule_drift = $submoduleDrift.Count
        }
        deleted_tracked = $deletedSummary
        modified_tracked = $modifiedSummary
        untracked = $untrackedSummary
        submodules = [ordered]@{
            drift_count = $submoduleDrift.Count
            sample = @($submoduleDrift | Select-Object -First $SampleLimit)
        }
        safety = [ordered]@{
            destructive_actions_performed = $false
            cleanup_allowed = $false
            note = "Inventory only. Cleanup, reset, checkout, delete, and move actions are intentionally out of scope."
        }
    }
}
finally {
    Pop-Location
}

$json = $result | ConvertTo-Json -Depth 8 -Compress
if ($OutputPath.Trim()) {
    $OutputFull = Assert-CanonicalEtaPath -Path $OutputPath
    $OutputDir = Split-Path -Parent $OutputFull
    if ($OutputDir -and -not (Test-Path -LiteralPath $OutputDir)) {
        New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
    }
    Set-Content -LiteralPath $OutputFull -Value $json -Encoding UTF8
}
$json
