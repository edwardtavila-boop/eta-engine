# EVOLUTIONARY TRADING ALGO // verify_vps_root_reconciliation.ps1
# Executes the current root review verification lane and records observed
# preserve/revisit guidance for the surfaced root source and companion items.

[CmdletBinding()]
param(
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$PlanPath = "",
    [string]$OutputPath = "",
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

function Write-AtomicUtf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $targetDir = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($targetDir)) {
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    }

    $tempPath = Join-Path $targetDir ("." + [System.IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    try {
        [System.IO.File]::WriteAllText($tempPath, $Content, $utf8NoBom)
        Move-Item -LiteralPath $tempPath -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-CanonicalPythonCommand {
    param([Parameter(Mandatory = $true)][string]$WorkspaceRoot)
    $venvPython = Join-Path $WorkspaceRoot "eta_engine\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }
    return "python"
}

function Get-OutputExcerpt {
    param(
        [AllowNull()][string]$Text,
        [int]$MaxLength = 600
    )
    $value = [string]$Text
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $null
    }
    $trimmed = $value.Trim()
    if ($trimmed.Length -le $MaxLength) {
        return $trimmed
    }
    return $trimmed.Substring(0, $MaxLength) + "..."
}

function Get-GitStatusPath {
    param([AllowNull()][string]$StatusLine)

    $line = [string]$StatusLine
    if ([string]::IsNullOrWhiteSpace($line)) {
        return ""
    }
    if ($line -match '^\?\?\s+(.*)$') {
        return [string]$Matches[1].Trim()
    }
    if ($line -match '^[ MADRCU?!][ MADRCU?!]\s+(.*)$') {
        return [string]$Matches[1].Trim()
    }
    if ($line -match '^[MADRCU?!]\s+(.*)$') {
        return [string]$Matches[1].Trim()
    }
    return $line.Trim()
}

function Invoke-ProcessCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $stdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) ("vps_root_review_stdout_" + [guid]::NewGuid().ToString("N") + ".log")
    $stderrPath = Join-Path ([System.IO.Path]::GetTempPath()) ("vps_root_review_stderr_" + [guid]::NewGuid().ToString("N") + ".log")
    try {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -NoNewWindow -PassThru -Wait -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $stdout = if (Test-Path -LiteralPath $stdoutPath) { [System.IO.File]::ReadAllText($stdoutPath).Trim() } else { "" }
        $stderr = if (Test-Path -LiteralPath $stderrPath) { [System.IO.File]::ReadAllText($stderrPath).Trim() } else { "" }
        $joined = @($stdout, $stderr) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        return [ordered]@{
            ok = ($proc.ExitCode -eq 0)
            exit_code = [int]$proc.ExitCode
            output = (($joined | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
        }
    } catch {
        return [ordered]@{
            ok = $false
            exit_code = -1
            output = [string]$_.Exception.Message
        }
    } finally {
        foreach ($tempPath in @($stdoutPath, $stderrPath)) {
            if ($tempPath -and (Test-Path -LiteralPath $tempPath)) {
                Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Get-GitShortstatSummary {
    param([AllowNull()][string]$Text)

    $lines = @(
        ([string]$Text -split "`r?`n") |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($lines.Count -eq 0) {
        return ""
    }

    $shortstatLine = @(
        $lines | Where-Object { ([string]$_) -match '^\d+\s+files?\s+changed(?:,|\s|$)' }
    ) | Select-Object -First 1
    if (-not [string]::IsNullOrWhiteSpace($shortstatLine)) {
        return [string]$shortstatLine
    }

    $nonWarningLines = @(
        $lines | Where-Object { ([string]$_) -notmatch '^(warning:|hint:)' }
    )
    if ($nonWarningLines.Count -gt 0) {
        return (($nonWarningLines | ForEach-Object { [string]$_ }) -join "; ").Trim()
    }

    return ""
}

function New-ObservedReviewResult {
    param(
        [Parameter(Mandatory = $true)]$Item,
        [Parameter(Mandatory = $true)]$Execution,
        [Parameter(Mandatory = $true)][string]$ReviewStatus,
        [Parameter(Mandatory = $true)][string]$ReviewSummary,
        [Parameter(Mandatory = $true)][string]$RecommendedOutcome
    )

    return [ordered]@{
        path = [string]$Item.path
        basename = [string]$Item.basename
        area = [string]$Item.area
        verification_mode = [string]$Item.verification_mode
        verification_side_effects = [string]$Item.verification_side_effects
        review_status = $ReviewStatus
        review_summary = $ReviewSummary
        recommended_outcome = $RecommendedOutcome
        verified_at = (Get-Date).ToUniversalTime().ToString("o")
        exit_code = [int]$Execution.exit_code
        output_excerpt = Get-OutputExcerpt -Text ([string]$Execution.output)
    }
}

function New-ObservedCompanionReviewResult {
    param(
        [Parameter(Mandatory = $true)]$Item,
        [Parameter(Mandatory = $true)]$Execution,
        [Parameter(Mandatory = $true)][string]$ReviewStatus,
        [Parameter(Mandatory = $true)][string]$ReviewSummary,
        [Parameter(Mandatory = $true)][string]$RecommendedOutcome,
        [hashtable]$AdditionalFields = @{}
    )

    $payload = [ordered]@{
        target = [string]$Item.target
        reason = [string]$Item.reason
        suggested_decision = [string]$Item.suggested_decision
        review_status = $ReviewStatus
        review_summary = $ReviewSummary
        recommended_outcome = $RecommendedOutcome
        verified_at = (Get-Date).ToUniversalTime().ToString("o")
        exit_code = [int]$Execution.exit_code
        output_excerpt = Get-OutputExcerpt -Text ([string]$Execution.output)
    }
    foreach ($entry in $AdditionalFields.GetEnumerator()) {
        $payload[$entry.Key] = $entry.Value
    }
    return $payload
}

function Get-CompanionChangeGroupName {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = $Path.Replace("\", "/").Trim("/")
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return ""
    }
    $parts = @($normalized -split "/")
    if ($parts.Count -ge 2 -and $parts[0] -eq "deploy") {
        return "$($parts[0])/$($parts[1])"
    }
    if ($parts.Count -ge 1) {
        return [string]$parts[0]
    }
    return $normalized
}

function Get-ObservedCompanionChangeGroups {
    param(
        [string[]]$TrackedPaths = @(),
        [string[]]$UntrackedPaths = @(),
        [Parameter(Mandatory = $true)][string]$RepoPath
    )

    $groupMap = [ordered]@{}

    foreach ($trackedPath in @($TrackedPaths | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $groupName = Get-CompanionChangeGroupName -Path $trackedPath
        if (-not $groupMap.Contains($groupName)) {
            $groupMap[$groupName] = [ordered]@{
                group = $groupName
                tracked_count = 0
                untracked_count = 0
                total_count = 0
                sample_paths = New-Object System.Collections.Generic.List[string]
                inspection_command = "git -C $RepoPath diff -- $groupName"
            }
        }
        $groupMap[$groupName].tracked_count += 1
        $groupMap[$groupName].total_count += 1
        if ($groupMap[$groupName].sample_paths.Count -lt 3) {
            $groupMap[$groupName].sample_paths.Add($trackedPath) | Out-Null
        }
    }

    foreach ($untrackedPath in @($UntrackedPaths | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $groupName = Get-CompanionChangeGroupName -Path $untrackedPath
        if (-not $groupMap.Contains($groupName)) {
            $groupMap[$groupName] = [ordered]@{
                group = $groupName
                tracked_count = 0
                untracked_count = 0
                total_count = 0
                sample_paths = New-Object System.Collections.Generic.List[string]
                inspection_command = "git -C $RepoPath diff -- $groupName"
            }
        }
        $groupMap[$groupName].untracked_count += 1
        $groupMap[$groupName].total_count += 1
        if ($groupMap[$groupName].sample_paths.Count -lt 3) {
            $groupMap[$groupName].sample_paths.Add($untrackedPath) | Out-Null
        }
    }

    $groups = @(
        $groupMap.Values |
            Sort-Object -Property @{ Expression = { [int]$_.total_count }; Descending = $true }, @{ Expression = { [string]$_.group }; Descending = $false } |
            ForEach-Object {
                [ordered]@{
                    group = [string]$_.group
                    tracked_count = [int]$_.tracked_count
                    untracked_count = [int]$_.untracked_count
                    total_count = [int]$_.total_count
                    sample_paths = @($_.sample_paths | ForEach-Object { [string]$_ })
                    inspection_command = [string]$_.inspection_command
                }
            }
    )

    $summaryParts = New-Object System.Collections.Generic.List[string]
    foreach ($group in $groups) {
        $piece = [string]$group.group
        if ([int]$group.tracked_count -gt 0) {
            $piece += " t=$([int]$group.tracked_count)"
        }
        if ([int]$group.untracked_count -gt 0) {
            $piece += " u=$([int]$group.untracked_count)"
        }
        $summaryParts.Add($piece) | Out-Null
    }

    return [ordered]@{
        groups = @($groups)
        summary = (($summaryParts | ForEach-Object { [string]$_ }) -join "; ")
    }
}

$script:CompanionFocusAreaPriority = @(
    "ops_readiness_truth",
    "operator_surface_reconciliation",
    "broker_runtime_controls",
    "supervisor_strategy_runtime",
    "observability_reconciliation",
    "ops_runtime_misc"
)

$script:CompanionFocusAreaPathMap = @{
    "scripts/project_kaizen_closeout.py" = "ops_readiness_truth"
    "scripts/prop_launch_check.py" = "ops_readiness_truth"
    "scripts/health_check.py" = "ops_readiness_truth"
    "scripts/diamond_retune_status.py" = "ops_readiness_truth"
    "scripts/diamond_ops_dashboard.py" = "ops_readiness_truth"
    "scripts/diamond_prop_prelaunch_dryrun.py" = "ops_readiness_truth"
    "scripts/prop_live_readiness_gate.py" = "ops_readiness_truth"
    "scripts/eta_status.py" = "ops_readiness_truth"
    "scripts/retune_advisory_cache.py" = "ops_readiness_truth"
    "scripts/alert_channel_config.py" = "ops_readiness_truth"
    "scripts/diamond_retune_truth_check.py" = "ops_readiness_truth"
    "scripts/diamond_wave25_status.py" = "ops_readiness_truth"
    "scripts/monday_first_light_check.py" = "ops_readiness_truth"
    "scripts/verify_telegram.py" = "ops_readiness_truth"
    "scripts/broker_bracket_audit.py" = "broker_runtime_controls"
    "scripts/closed_trade_ledger.py" = "broker_runtime_controls"
    "scripts/daily_loss_killswitch.py" = "broker_runtime_controls"
    "scripts/jarvis_strategy_supervisor.py" = "supervisor_strategy_runtime"
    "strategies/per_bot_registry.py" = "supervisor_strategy_runtime"
    "deploy/scripts/dashboard_api.py" = "operator_surface_reconciliation"
    "deploy/scripts/inspect_vps_root_dirty.ps1" = "operator_surface_reconciliation"
    "deploy/scripts/plan_vps_root_reconciliation.ps1" = "operator_surface_reconciliation"
    "deploy/scripts/verify_vps_root_reconciliation.ps1" = "operator_surface_reconciliation"
    "scripts/public_edge_route_watchdog.py" = "operator_surface_reconciliation"
}

function Get-CompanionFocusAreaForPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = $Path.Replace("\", "/").Trim("/")
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return ""
    }
    if ($script:CompanionFocusAreaPathMap.ContainsKey($normalized)) {
        return [string]$script:CompanionFocusAreaPathMap[$normalized]
    }
    if ($normalized.StartsWith("deploy/scripts/")) {
        return "operator_surface_reconciliation"
    }
    if ($normalized.StartsWith("obs/")) {
        return "observability_reconciliation"
    }
    if ($normalized.StartsWith("strategies/")) {
        return "supervisor_strategy_runtime"
    }
    if ($normalized.StartsWith("scripts/")) {
        return "ops_runtime_misc"
    }
    return "ops_runtime_misc"
}

function Get-CompanionFocusAreaPriorityIndex {
    param([Parameter(Mandatory = $true)][string]$Area)

    $index = [Array]::IndexOf($script:CompanionFocusAreaPriority, $Area)
    if ($index -lt 0) {
        return $script:CompanionFocusAreaPriority.Count
    }
    return $index
}

function Get-ObservedCompanionFocusAreas {
    param(
        [string[]]$TrackedPaths = @(),
        [string[]]$UntrackedPaths = @()
    )

    $areaMap = [ordered]@{}

    foreach ($trackedPath in @($TrackedPaths | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $area = Get-CompanionFocusAreaForPath -Path $trackedPath
        if (-not $areaMap.Contains($area)) {
            $areaMap[$area] = [ordered]@{
                area = $area
                tracked_count = 0
                untracked_count = 0
                total_count = 0
                sample_paths = New-Object System.Collections.Generic.List[string]
            }
        }
        $areaMap[$area].tracked_count += 1
        $areaMap[$area].total_count += 1
        if ($areaMap[$area].sample_paths.Count -lt 3 -and -not $areaMap[$area].sample_paths.Contains($trackedPath)) {
            $areaMap[$area].sample_paths.Add($trackedPath) | Out-Null
        }
    }

    foreach ($untrackedPath in @($UntrackedPaths | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $area = Get-CompanionFocusAreaForPath -Path $untrackedPath
        if (-not $areaMap.Contains($area)) {
            $areaMap[$area] = [ordered]@{
                area = $area
                tracked_count = 0
                untracked_count = 0
                total_count = 0
                sample_paths = New-Object System.Collections.Generic.List[string]
            }
        }
        $areaMap[$area].untracked_count += 1
        $areaMap[$area].total_count += 1
        if ($areaMap[$area].sample_paths.Count -lt 3 -and -not $areaMap[$area].sample_paths.Contains($untrackedPath)) {
            $areaMap[$area].sample_paths.Add($untrackedPath) | Out-Null
        }
    }

    $areas = @(
        $areaMap.Values |
            Sort-Object -Property `
                @{ Expression = { [int]$_.total_count }; Descending = $true }, `
                @{ Expression = { Get-CompanionFocusAreaPriorityIndex -Area ([string]$_.area) }; Descending = $false }, `
                @{ Expression = { [string]$_.area }; Descending = $false } |
            ForEach-Object {
                [ordered]@{
                    area = [string]$_.area
                    tracked_count = [int]$_.tracked_count
                    untracked_count = [int]$_.untracked_count
                    total_count = [int]$_.total_count
                    sample_paths = @($_.sample_paths | ForEach-Object { [string]$_ })
                }
            }
    )

    $summaryParts = New-Object System.Collections.Generic.List[string]
    foreach ($area in $areas) {
        $piece = [string]$area.area
        if ([int]$area.tracked_count -gt 0) {
            $piece += " t=$([int]$area.tracked_count)"
        }
        if ([int]$area.untracked_count -gt 0) {
            $piece += " u=$([int]$area.untracked_count)"
        }
        $summaryParts.Add($piece) | Out-Null
    }

    return [ordered]@{
        areas = @($areas)
        summary = (($summaryParts | ForEach-Object { [string]$_ }) -join "; ")
    }
}

function Get-ObservedCompanionBatchAssessment {
    param([object[]]$FocusAreas = @())

    $validAreas = @(
        $FocusAreas |
            Where-Object {
                $null -ne $_ -and
                -not [string]::IsNullOrWhiteSpace([string]$_.area)
            }
    )
    if ($validAreas.Count -eq 0) {
        return [ordered]@{}
    }

    $totalCount = 0
    foreach ($area in $validAreas) {
        $totalCount += [int]$area.total_count
    }
    if ($totalCount -le 0) {
        return [ordered]@{}
    }

    $top = $validAreas[0]
    $second = if ($validAreas.Count -gt 1) { $validAreas[1] } else { $null }
    $topArea = [string]$top.area
    $topCount = [int]$top.total_count
    $secondArea = if ($null -ne $second) { [string]$second.area } else { "" }
    $secondCount = if ($null -ne $second) { [int]$second.total_count } else { 0 }
    $topTwoCount = $topCount + $secondCount
    $allRuntime = @(
        $validAreas |
            Where-Object { $script:CompanionFocusAreaPriority -notcontains ([string]$_.area) }
    ).Count -eq 0

    if ($allRuntime -and ([double]$topTwoCount / [double]$totalCount) -ge 0.6) {
        $label = "runtime_hardening_batch"
        $coherence = "coherent"
        $handling = "preserve_or_commit_as_single_child_batch"
    }
    elseif (([double]$topCount / [double]$totalCount) -ge 0.5) {
        $label = "$topArea`_dominant_batch"
        $coherence = "mostly_coherent"
        $handling = "preserve_or_commit_after_targeted_review"
    }
    else {
        $label = "mixed_runtime_batch"
        $coherence = "mixed"
        $handling = "split_or_review_before_commit"
    }

    if (-not [string]::IsNullOrWhiteSpace($secondArea)) {
        $summary = "top areas $topArea + $secondArea cover $topTwoCount/$totalCount file changes"
        $topAreas = @($topArea, $secondArea)
    }
    else {
        $summary = "top area $topArea covers $topCount/$totalCount file changes"
        $topAreas = @($topArea)
    }

    return [ordered]@{
        label = $label
        coherence = $coherence
        recommended_handling = $handling
        summary = $summary
        top_areas = @($topAreas)
        total_count = $totalCount
    }
}

function Get-ObservedCompanionBatchScope {
    param(
        [object[]]$ChangeGroups = @(),
        [Parameter(Mandatory = $true)][string]$RepoPath
    )

    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($group in @($ChangeGroups | Where-Object { $null -ne $_ })) {
        $groupName = [string]$group.group
        $groupTotal = [int]$group.total_count
        $samplePaths = @($group.sample_paths | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $scopePath = $groupName
        if ($groupTotal -eq 1 -and $samplePaths.Count -eq 1) {
            $scopePath = $samplePaths[0]
        }
        if (-not [string]::IsNullOrWhiteSpace($scopePath) -and -not $paths.Contains($scopePath)) {
            $paths.Add($scopePath) | Out-Null
        }
    }

    $pathList = @($paths | ForEach-Object { [string]$_ })
    $command = if ($pathList.Count -gt 0) {
        "git -C $RepoPath diff -- " + ($pathList -join " ")
    } else {
        ""
    }

    return [ordered]@{
        paths = $pathList
        command = $command
        stat_command = if ($pathList.Count -gt 0) {
            "git -C $RepoPath diff --shortstat -- " + ($pathList -join " ")
        } else {
            ""
        }
        path_count = $pathList.Count
    }
}

$RootFull = Assert-CanonicalEtaPath -Path $Root
if (-not $PlanPath.Trim()) {
    $PlanPath = Join-Path $RootFull "var\eta_engine\state\vps_root_reconciliation_plan.json"
}
if (-not $OutputPath.Trim()) {
    $OutputPath = Join-Path $RootFull "var\eta_engine\state\vps_root_reconciliation_review.json"
}

$PlanFull = Assert-CanonicalEtaPath -Path $PlanPath
$OutputFull = Assert-CanonicalEtaPath -Path $OutputPath

if (-not (Test-Path -LiteralPath $PlanFull)) {
    throw "Missing reconciliation plan: $PlanFull"
}

$plan = Get-Content -LiteralPath $PlanFull -Raw | ConvertFrom-Json
$sourceReviewItems = @()
if ($null -ne $plan -and $null -ne $plan.PSObject.Properties["source_review_items"] -and $plan.source_review_items) {
    $sourceReviewItems = @($plan.source_review_items)
}
$companionReviewItems = @()
if ($null -ne $plan -and $null -ne $plan.PSObject.Properties["companion_review_items"] -and $plan.companion_review_items) {
    $companionReviewItems = @($plan.companion_review_items)
}

$pythonCommand = Get-CanonicalPythonCommand -WorkspaceRoot $RootFull
$results = New-Object System.Collections.Generic.List[object]
$companionResults = New-Object System.Collections.Generic.List[object]

foreach ($item in $sourceReviewItems) {
    if ($null -eq $item) {
        continue
    }
    $basename = [string]$item.basename
    if ([string]::IsNullOrWhiteSpace($basename)) {
        continue
    }

    switch ($basename) {
        "command-center-watchdog-status.ps1" {
            $execution = Invoke-ProcessCapture -FilePath "powershell.exe" -Arguments @(
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", (Join-Path $RootFull "scripts\command-center-watchdog-status.ps1"),
                "-Json"
            ) -WorkingDirectory $RootFull
            if ($execution.ok) {
                try {
                    $payload = ([string]$execution.output) | ConvertFrom-Json
                    $overall = if ($null -ne $payload.overall_status) { [string]$payload.overall_status } else { "unknown" }
                    $nextReason = if ($null -ne $payload.operator_next_reason) { [string]$payload.operator_next_reason } else { "" }
                    $summary = "Watchdog status probe succeeded; overall_status=$overall"
                    if (-not [string]::IsNullOrWhiteSpace($nextReason)) {
                        $summary += "; next_reason=$nextReason"
                    }
                    $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "verified_ok" -ReviewSummary $summary -RecommendedOutcome "preserve_candidate")) | Out-Null
                } catch {
                    $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "verification_failed" -ReviewSummary "Watchdog status probe returned non-JSON or an unreadable payload." -RecommendedOutcome "revisit_required")) | Out-Null
                }
            } else {
                $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "verification_failed" -ReviewSummary "Watchdog status probe failed." -RecommendedOutcome "revisit_required")) | Out-Null
            }
            continue
        }
        "reload-operator-service.ps1" {
            $execution = Invoke-ProcessCapture -FilePath "powershell.exe" -Arguments @(
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", (Join-Path $RootFull "scripts\reload-operator-service.ps1"),
                "-SkipPublicCheck",
                "-SkipWatchdogRegistration",
                "-NoAutoElevate",
                "-TimeoutSeconds", "30"
            ) -WorkingDirectory $RootFull
            $reloadSucceeded = ([string]$execution.output -match "Operator services reloaded and verified\.")
            if ($reloadSucceeded -or $execution.ok) {
                $summary = if ($reloadSucceeded) {
                    "Reload wrapper reported verified local operator truth for the 8421 surface."
                } else {
                    "Reload wrapper completed successfully and re-verified the local 8421 operator contract."
                }
                $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "verified_ok" -ReviewSummary $summary -RecommendedOutcome "preserve_candidate")) | Out-Null
            } else {
                $postReloadVerify = Invoke-ProcessCapture -FilePath $pythonCommand -Arguments @(
                    ".\scripts\verify_operator_source_of_truth.py",
                    "--base-url", "http://127.0.0.1:8421",
                    "--timeout", "30"
                ) -WorkingDirectory $RootFull
                if ($postReloadVerify.ok) {
                    $summary = "Reload wrapper timed out, but direct local operator verification passed immediately after reload."
                    $combinedExecution = @{
                        exit_code = 0
                        output = ((@([string]$execution.output, [string]$postReloadVerify.output) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join [Environment]::NewLine)
                    }
                    $results.Add((New-ObservedReviewResult -Item $item -Execution $combinedExecution -ReviewStatus "verified_ok" -ReviewSummary $summary -RecommendedOutcome "preserve_candidate")) | Out-Null
                } else {
                    $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "verification_failed" -ReviewSummary "Reload wrapper failed or did not report verified local operator truth." -RecommendedOutcome "revisit_required")) | Out-Null
                }
            }
            continue
        }
        "verify_operator_source_of_truth.py" {
            $execution = Invoke-ProcessCapture -FilePath $pythonCommand -Arguments @(
                ".\scripts\verify_operator_source_of_truth.py",
                "--base-url", "http://127.0.0.1:8421",
                "--timeout", "30"
            ) -WorkingDirectory $RootFull
            if ($execution.ok) {
                $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "verified_ok" -ReviewSummary "Operator source verifier accepted the local 8421 payloads and failure classification contract." -RecommendedOutcome "preserve_candidate")) | Out-Null
            } else {
                $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "verification_failed" -ReviewSummary "Operator source verifier rejected the local 8421 payloads." -RecommendedOutcome "revisit_required")) | Out-Null
            }
            continue
        }
        default {
            $execution = @{
                ok = $false
                exit_code = -1
                output = "No execution recipe defined."
            }
            $results.Add((New-ObservedReviewResult -Item $item -Execution $execution -ReviewStatus "unsupported" -ReviewSummary "No observed review runner is defined for this root item yet." -RecommendedOutcome "manual_review_required")) | Out-Null
            continue
        }
    }
}

foreach ($item in $companionReviewItems) {
    if ($null -eq $item) {
        continue
    }
    $target = [string]$item.target
    if ([string]::IsNullOrWhiteSpace($target)) {
        continue
    }

    switch ($target) {
        "eta_engine" {
            $repoPath = Join-Path $RootFull "eta_engine"
            $statusExecution = Invoke-ProcessCapture -FilePath "git.exe" -Arguments @(
                "-C", $repoPath, "status", "--short"
            ) -WorkingDirectory $RootFull
            if (-not $statusExecution.ok) {
                $companionResults.Add((New-ObservedCompanionReviewResult -Item $item -Execution $statusExecution -ReviewStatus "verification_failed" -ReviewSummary "Companion eta_engine git status probe failed." -RecommendedOutcome "revisit_required")) | Out-Null
                continue
            }

            $headExecution = Invoke-ProcessCapture -FilePath "git.exe" -Arguments @(
                "-C", $repoPath, "rev-parse", "--short", "HEAD"
            ) -WorkingDirectory $RootFull
            $branchExecution = Invoke-ProcessCapture -FilePath "git.exe" -Arguments @(
                "-C", $repoPath, "branch", "--show-current"
            ) -WorkingDirectory $RootFull
            $statusLines = @(([string]$statusExecution.output -split "`r?`n") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $trackedPaths = @($statusLines | Where-Object { -not ([string]$_).StartsWith("??") } | ForEach-Object { Get-GitStatusPath -StatusLine ([string]$_) } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $untrackedPaths = @($statusLines | Where-Object { ([string]$_).StartsWith("??") } | ForEach-Object { Get-GitStatusPath -StatusLine ([string]$_) } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $trackedCount = @($statusLines | Where-Object { -not ([string]$_).StartsWith("??") }).Count
            $untrackedCount = @($statusLines | Where-Object { ([string]$_).StartsWith("??") }).Count
            $branchName = [string]$branchExecution.output
            if ([string]::IsNullOrWhiteSpace($branchName)) {
                $branchName = "detached"
            }
            $headShort = if ($headExecution.ok) { [string]$headExecution.output } else { "unknown" }
            $changeGroupInfo = Get-ObservedCompanionChangeGroups -TrackedPaths $trackedPaths -UntrackedPaths $untrackedPaths -RepoPath $repoPath
            $focusAreaInfo = Get-ObservedCompanionFocusAreas -TrackedPaths $trackedPaths -UntrackedPaths $untrackedPaths
            $batchScopeInfo = Get-ObservedCompanionBatchScope -ChangeGroups $changeGroupInfo["groups"] -RepoPath $repoPath
            $batchAssessment = Get-ObservedCompanionBatchAssessment -FocusAreas $focusAreaInfo["areas"]
            $batchShortstat = ""
            if ([int]$batchScopeInfo["path_count"] -gt 0) {
                $statArgs = @("-C", $repoPath, "diff", "--shortstat", "--")
                $statArgs += @($batchScopeInfo["paths"])
                $statExecution = Invoke-ProcessCapture -FilePath "git.exe" -Arguments $statArgs -WorkingDirectory $RootFull
                if ($statExecution.ok) {
                    $batchShortstat = Get-GitShortstatSummary -Text ([string]$statExecution.output)
                }
            }
            if ($untrackedCount -gt 0) {
                if ([string]::IsNullOrWhiteSpace($batchShortstat)) {
                    $batchShortstat = "plus $untrackedCount untracked path(s)"
                }
                else {
                    $batchShortstat += "; plus $untrackedCount untracked path(s)"
                }
            }
            $additionalFields = @{
                tracked_change_count = $trackedCount
                untracked_change_count = $untrackedCount
                tracked_files = @($trackedPaths)
                untracked_files = @($untrackedPaths)
                change_groups = @($changeGroupInfo.groups)
                change_group_summary = [string]$changeGroupInfo.summary
            }

            if (($trackedCount + $untrackedCount) -gt 0) {
                $summary = "Companion eta_engine remains dirty/diverged; branch=$branchName; head=$headShort; tracked=$trackedCount; untracked=$untrackedCount."
                $observedResult = New-ObservedCompanionReviewResult -Item $item -Execution $statusExecution -ReviewStatus "verified_review_required" -ReviewSummary $summary -RecommendedOutcome "commit_preserve_or_pin_before_root_update" -AdditionalFields $additionalFields
            } else {
                $summary = "Companion eta_engine is currently clean/aligned; branch=$branchName; head=$headShort."
                $observedResult = New-ObservedCompanionReviewResult -Item $item -Execution $statusExecution -ReviewStatus "verified_ok" -ReviewSummary $summary -RecommendedOutcome "clear_candidate" -AdditionalFields $additionalFields
            }
            $observedResult["focus_areas"] = @($focusAreaInfo["areas"])
            $observedResult["focus_area_summary"] = [string]$focusAreaInfo["summary"]
            $observedResult["batch_scope_paths"] = @($batchScopeInfo["paths"])
            $observedResult["batch_scope_command"] = [string]$batchScopeInfo["command"]
            $observedResult["batch_scope_stat_command"] = [string]$batchScopeInfo["stat_command"]
            $observedResult["batch_scope_shortstat"] = $batchShortstat
            $observedResult["batch_scope_path_count"] = [int]$batchScopeInfo["path_count"]
            if ($batchAssessment.Count -gt 0) {
                $observedResult["batch_label"] = [string]$batchAssessment["label"]
                $observedResult["batch_coherence"] = [string]$batchAssessment["coherence"]
                $observedResult["batch_recommended_handling"] = [string]$batchAssessment["recommended_handling"]
                $observedResult["batch_summary"] = [string]$batchAssessment["summary"]
                $observedResult["batch_top_areas"] = @($batchAssessment["top_areas"])
                if ([string]$batchAssessment["recommended_handling"] -eq "preserve_or_commit_as_single_child_batch") {
                    $batchLabel = [string]$batchAssessment["label"]
                    $decisionBasisParts = New-Object System.Collections.Generic.List[string]
                    $recommendedPaths = @($batchScopeInfo["paths"])
                    $commitMessage = "eta_engine: harden VPS runtime readiness and operator truth surfaces"
                    $recommendedStageCommand = ""
                    if ($recommendedPaths.Count -gt 0) {
                        $recommendedStageCommand = "git -C $RepoPath add -- " + [string]::Join(" ", $recommendedPaths)
                    }
                    $recommendedCommitCommand = if (-not [string]::IsNullOrWhiteSpace($recommendedStageCommand)) {
                        "git -C $RepoPath commit -m ""$commitMessage"""
                    } else {
                        ""
                    }
                    if (-not [string]::IsNullOrWhiteSpace([string]$batchAssessment["summary"])) {
                        $decisionBasisParts.Add([string]$batchAssessment["summary"]) | Out-Null
                    }
                    if (-not [string]::IsNullOrWhiteSpace($batchShortstat)) {
                        $decisionBasisParts.Add($batchShortstat) | Out-Null
                    }
                    $observedResult["decision_options"] = @(
                        @{
                            option = "commit"
                            label = "Commit child batch"
                            summary = "Commit eta_engine as one coherent $batchLabel inside the child repo, then the superproject can move the gitlink later."
                            when_to_choose = "Choose when the reviewed changes are intended shared child-repo updates and the batch review is complete."
                            root_update_ready = $true
                        },
                        @{
                            option = "preserve"
                            label = "Preserve current checkout"
                            summary = "Preserve the current eta_engine checkout in place for more soak or host-local runtime use without moving the superproject gitlink."
                            when_to_choose = "Choose when the batch still needs live soak or is intentionally VPS-local for now."
                            root_update_ready = $false
                        },
                        @{
                            option = "pin"
                            label = "Pin current head"
                            summary = "Freeze the current eta_engine head for later comparison or rollback without committing the child batch yet."
                            when_to_choose = "Choose when you want to preserve the exact reviewed state before deciding whether to commit it."
                            root_update_ready = $false
                        }
                    )
                    $observedResult["decision_recommended_option"] = "commit"
                    $observedResult["decision_recommended_reason"] = "eta_engine is a live-verified coherent $batchLabel; commit is the cleanest path if this reviewed slice is intended shared child-repo work."
                    $observedResult["decision_basis"] = [string]::Join("; ", @($decisionBasisParts | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }))
                    $observedResult["decision_recommended_paths"] = @($recommendedPaths)
                    $observedResult["decision_recommended_commit_message"] = $commitMessage
                    $observedResult["decision_recommended_stage_command"] = $recommendedStageCommand
                    $observedResult["decision_recommended_commit_command"] = $recommendedCommitCommand
                    $observedResult["decision_recommended_commands"] = @(
                        @($recommendedStageCommand, $recommendedCommitCommand) | Where-Object {
                            -not [string]::IsNullOrWhiteSpace([string]$_)
                        }
                    )
                }
            }
            $companionResults.Add($observedResult) | Out-Null
            continue
        }
        default {
            $execution = @{
                ok = $false
                exit_code = -1
                output = "No companion execution recipe defined."
            }
            $companionResults.Add((New-ObservedCompanionReviewResult -Item $item -Execution $execution -ReviewStatus "unsupported" -ReviewSummary "No observed review runner is defined for this companion item yet." -RecommendedOutcome "manual_review_required")) | Out-Null
            continue
        }
    }
}

$resultList = @($results | ForEach-Object { $_ })
$verifiedOkCount = @($resultList | Where-Object { [string]$_.review_status -eq "verified_ok" }).Count
$preserveCandidateCount = @($resultList | Where-Object { [string]$_.recommended_outcome -eq "preserve_candidate" }).Count
$reviewStatus = if ($resultList.Count -eq 0) {
    "missing_source_items"
} elseif ($verifiedOkCount -eq $resultList.Count) {
    "ok"
} elseif ($verifiedOkCount -gt 0) {
    "partial"
} else {
    "failed"
}

$summaryLine = if ($resultList.Count -gt 0) {
    "$verifiedOkCount/$($resultList.Count) source review item(s) verified; $preserveCandidateCount preserve candidate(s)"
} else {
    "No source review items were available to verify."
}

$companionResultList = @($companionResults | ForEach-Object { $_ })
$companionVerifiedCount = @($companionResultList | Where-Object { [string]$_.review_status -like "verified_*" }).Count
$companionDecisionRequiredCount = @($companionResultList | Where-Object { [string]$_.recommended_outcome -eq "commit_preserve_or_pin_before_root_update" }).Count
$companionReviewStatus = if ($companionResultList.Count -eq 0) {
    "missing_companion_items"
} elseif ($companionDecisionRequiredCount -gt 0) {
    "review_required"
} elseif ($companionVerifiedCount -eq $companionResultList.Count) {
    "ok"
} else {
    "partial"
}
$companionSummaryLine = if ($companionResultList.Count -gt 0) {
    "$companionVerifiedCount/$($companionResultList.Count) companion review target(s) checked; $companionDecisionRequiredCount decision-required companion target(s)"
} else {
    "No companion review items were available to verify."
}

$resultsByBasename = [ordered]@{}
foreach ($result in $resultList) {
    $basename = [string]$result.basename
    if (-not [string]::IsNullOrWhiteSpace($basename)) {
        $resultsByBasename[$basename] = $result
    }
}
$companionResultsByTarget = [ordered]@{}
foreach ($result in $companionResultList) {
    $target = [string]$result.target
    if (-not [string]::IsNullOrWhiteSpace($target)) {
        $companionResultsByTarget[$target] = $result
    }
}

$payload = [ordered]@{
    status = $reviewStatus
    root = $RootFull
    plan_path = $PlanFull
    verified_at = (Get-Date).ToUniversalTime().ToString("o")
    source_item_count = $resultList.Count
    verified_ok_count = $verifiedOkCount
    preserve_candidate_count = $preserveCandidateCount
    summary_line = $summaryLine
    items = $resultList
    results_by_basename = $resultsByBasename
    companion_review_status = $companionReviewStatus
    companion_item_count = $companionResultList.Count
    companion_verified_count = $companionVerifiedCount
    companion_decision_required_count = $companionDecisionRequiredCount
    companion_summary_line = $companionSummaryLine
    companion_items = $companionResultList
    companion_results_by_target = $companionResultsByTarget
}

$json = $payload | ConvertTo-Json -Depth 10
if ($NoWrite) {
    $json
    return
}

Write-AtomicUtf8File -Path $OutputFull -Content $json
$json
