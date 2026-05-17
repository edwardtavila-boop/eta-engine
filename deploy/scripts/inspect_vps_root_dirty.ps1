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
$RuntimeOptionalSubmodules = @("mnq_backtest")

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

    # Use ProcessStartInfo instead of native PowerShell invocation so benign
    # Git stderr warnings (for example line-ending normalization on Windows)
    # do not become terminating inventory failures.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "git"
    $quotedArgs = @($Arguments | ForEach-Object {
        if ("$_" -match "\s") {
            '"' + ("$_" -replace '"', '\"') + '"'
        }
        else {
            "$_"
        }
    })
    $psi.Arguments = [string]::Join(" ", $quotedArgs)
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ($proc.ExitCode -ne 0) {
        return @()
    }

    $lines = @($stdout -split "`r?`n")
    return @($lines | Where-Object { $_ -ne $null -and "$_".Trim() -ne "" })
}

function Get-PathCategory {
    param([Parameter(Mandatory = $true)][string]$Path)
    $p = ($Path -replace "\\", "/").Trim()

    if ($p -match "\.bak(\.|$)" -or $p -match "\.bak_[0-9]{8}_[0-9]{6}$") {
        return "local_backup_artifact"
    }
    if ($p -match "^scripts/_check_.*\.py$") {
        return "local_diagnostic_artifact"
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

function Get-CompanionStatusMeaning {
    param([Parameter(Mandatory = $true)][string]$Status)
    $s = "$Status"
    if ($s -match "\?") {
        return "submodule_has_untracked_content"
    }
    if ($s -cmatch "m") {
        return "submodule_has_modified_worktree"
    }
    if ($s -cmatch "M") {
        return "submodule_pointer_changed"
    }
    return "submodule_status_changed"
}

function Get-DirtyCompanionStatus {
    param(
        [AllowEmptyCollection()][string[]]$PorcelainLines = @(),
        [AllowEmptyCollection()][string[]]$OptionalDormantSubmodules = @(),
        [AllowEmptyCollection()][string[]]$UninitializedSubmodulePaths = @(),
        [Parameter(Mandatory = $true)][int]$Limit
    )
    $items = New-Object System.Collections.Generic.List[object]
    foreach ($line in $PorcelainLines) {
        if ($null -eq $line -or "$line".Length -lt 4) {
            continue
        }
        $statusCode = "$line".Substring(0, 2)
        $path = "$line".Substring(3).Trim().Trim('"')
        if ((Get-PathCategory -Path $path) -ne "submodule_or_companion_repo") {
            continue
        }
        $trimmedStatus = $statusCode.Trim()
        $isOptionalDormant = @($OptionalDormantSubmodules) -contains $path
        $isUninitialized = @($UninitializedSubmodulePaths) -contains $path
        if ($isOptionalDormant -and $isUninitialized -and $trimmedStatus -eq "D") {
            continue
        }
        [void]$items.Add([ordered]@{
            path = $path
            status = $trimmedStatus
            meaning = Get-CompanionStatusMeaning -Status $statusCode
            line = "$line"
        })
    }
    return @($items | Select-Object -First $Limit)
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
    $submoduleDrift = @($submodules | Where-Object { $_ -match "^\+" })
    $submoduleUninitialized = @($submodules | Where-Object { $_ -match "^-" })
    $submoduleUninitializedPaths = @(
        $submoduleUninitialized | ForEach-Object {
            $parts = "$_".TrimStart("-").Trim() -split "\s+"
            if ($parts.Length -ge 2) {
                $parts[1].Trim()
            }
        } | Where-Object { $_ -and "$_".Trim() -ne "" }
    )
    $optionalDormantSubmodules = @(
        $submoduleUninitializedPaths | Where-Object { @($RuntimeOptionalSubmodules) -contains $_ }
    )
    $optionalDormantDeletedTracked = @(
        $deletedTracked | Where-Object { @($optionalDormantSubmodules) -contains $_ }
    )
    $effectiveDeletedTracked = @(
        $deletedTracked | Where-Object { @($optionalDormantSubmodules) -notcontains $_ }
    )
    $effectivePorcelain = New-Object System.Collections.Generic.List[string]
    foreach ($line in $porcelain) {
        if ($null -eq $line -or "$line".Trim() -eq "") {
            continue
        }
        if ("$line".Length -lt 4) {
            [void]$effectivePorcelain.Add("$line")
            continue
        }
        $statusCode = "$line".Substring(0, 2).Trim()
        $path = "$line".Substring(3).Trim().Trim('"')
        if ((@($optionalDormantSubmodules) -contains $path) -and $statusCode -eq "D") {
            continue
        }
        [void]$effectivePorcelain.Add("$line")
    }

    $deletedSummary = New-CategorySummary -Paths $effectiveDeletedTracked -Limit $SampleLimit
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
    $dirtyCompanionStatus = @(
        Get-DirtyCompanionStatus `
            -PorcelainLines $porcelain `
            -OptionalDormantSubmodules $RuntimeOptionalSubmodules `
            -UninitializedSubmodulePaths $submoduleUninitializedPaths `
            -Limit $SampleLimit
    )

    $riskLevel = "low"
    $recommendedAction = "Generated/runtime drift only; review before cleanup."
    if ($sourceDeletedCount -gt 0 -or $effectiveDeletedTracked.Count -gt 25) {
        $riskLevel = "high"
        $recommendedAction = "Manual reconciliation required before cleanup: tracked source/governance deletions are present."
    }
    elseif ($unknownCount -gt 0 -or $submoduleDrift.Count -gt 0 -or $dirtyCompanionStatus.Count -gt 0) {
        $riskLevel = "medium"
        $recommendedAction = "Review unknown paths, dirty companion worktrees, and submodule drift before cleanup."
    }
    elseif ($submoduleUninitialized.Count -gt 0) {
        $recommendedAction = "Root working tree is clean; optional submodules are uninitialized and can remain pinned for VPS runtime."
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
            status = $effectivePorcelain.Count
            deleted_tracked = $effectiveDeletedTracked.Count
            optional_dormant_deleted_tracked = $optionalDormantDeletedTracked.Count
            modified_tracked = $modifiedTracked.Count
            untracked = $untracked.Count
            submodule_drift = $submoduleDrift.Count
            submodule_uninitialized = $submoduleUninitialized.Count
            optional_dormant_submodules = $optionalDormantSubmodules.Count
            dirty_companion_repos = $dirtyCompanionStatus.Count
        }
        deleted_tracked = $deletedSummary
        modified_tracked = $modifiedSummary
        untracked = $untrackedSummary
        submodules = [ordered]@{
            drift_count = $submoduleDrift.Count
            sample = @($submoduleDrift | Select-Object -First $SampleLimit)
            uninitialized_count = $submoduleUninitialized.Count
            uninitialized_sample = @($submoduleUninitialized | Select-Object -First $SampleLimit)
            optional_dormant_count = $optionalDormantSubmodules.Count
            optional_dormant_sample = @($optionalDormantSubmodules | Select-Object -First $SampleLimit)
            optional_dormant_deleted_tracked_count = $optionalDormantDeletedTracked.Count
            optional_dormant_deleted_tracked_sample = @($optionalDormantDeletedTracked | Select-Object -First $SampleLimit)
            dirty_worktree_count = $dirtyCompanionStatus.Count
            dirty_worktree_sample = @($dirtyCompanionStatus)
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
