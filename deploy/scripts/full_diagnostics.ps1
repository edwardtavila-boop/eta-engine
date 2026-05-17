<#
.SYNOPSIS
  ETA Full Systems Diagnostics - run on the VPS for a complete health check.
  One-shot readout covering all processes, tasks, services, ports, IBKR,
  DeepSeek, Cloudflare, logs, and engine health.
#>

$ErrorActionPreference = "SilentlyContinue"
$PASS = 0; $FAIL = 0; $WARN = 0

function Say($label, $ok) {
    if ($ok -eq $true)  { Write-Host "  [PASS]" -ForegroundColor Green  -NoNewline; $global:PASS++ }
    elseif ($ok -eq $null -or $ok -eq -1) { Write-Host "  [WARN]" -ForegroundColor Yellow -NoNewline; $global:WARN++ }
    else                { Write-Host "  [FAIL]" -ForegroundColor Red    -NoNewline; $global:FAIL++ }
    Write-Host " $label" -ForegroundColor White
}

function Convert-GatewayAuthorityEnabled {
    param($Value)
    if ($null -eq $Value) {
        return $false
    }
    if ($Value -is [bool]) {
        return [bool]$Value
    }
    if ($Value -is [System.ValueType]) {
        try {
            return ([double]$Value -ne 0)
        } catch {
            return $false
        }
    }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $false
    }
    return @("1", "true", "yes", "y", "on") -contains $text.Trim().ToLowerInvariant()
}

function Get-HostProfile {
    $markerPath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\gateway_authority.json"
    $isServerHost = $false
    try {
        $isServerHost = ((Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).ProductType -ne 1)
    } catch {}
    $markerMatch = $false
    if (Test-Path $markerPath) {
        try {
            $payload = Get-Content $markerPath -Raw | ConvertFrom-Json -ErrorAction Stop
            $role = [string]$payload.role
            $enabled = if ($null -ne $payload.enabled) { Convert-GatewayAuthorityEnabled -Value $payload.enabled } else { $false }
            $markerComputer = [string]$payload.computer_name
            $roleOk = @("vps", "gateway_authority") -contains $role.Trim().ToLowerInvariant()
            $markerMatch = (
                $enabled -and
                $roleOk -and
                $markerComputer.Equals($env:COMPUTERNAME, [System.StringComparison]::OrdinalIgnoreCase)
            )
        } catch {}
    }
    $name = if ($markerMatch) {
        "authoritative_vps"
    } elseif ($isServerHost) {
        "server_host"
    } else {
        "local_workstation"
    }
    [PSCustomObject]@{
        name = $name
        strict_vps_checks = ($markerMatch -or $isServerHost)
        marker_path = $markerPath
    }
}

function ScopeResult($ok) {
    if ($ok) { return $true }
    if ($script:StrictVpsChecks) { return $false }
    return $null
}

function Get-BotFleetProbe {
    $directError = $null
    foreach ($probe in @(
        @{ name = "direct_8000"; uri = "http://127.0.0.1:8000/api/bot-fleet" },
        @{ name = "proxy_8421"; uri = "http://127.0.0.1:8421/api/bot-fleet" }
    )) {
        try {
            $payload = Invoke-RestMethod -Uri $probe.uri -TimeoutSec 10 -ErrorAction Stop
            return [PSCustomObject]@{
                ok = $true
                source = [string]$probe.name
                uri = [string]$probe.uri
                payload = $payload
                direct_error = $directError
            }
        } catch {
            if (-not $directError) {
                $directError = $_.Exception.Message
            }
        }
    }

    return [PSCustomObject]@{
        ok = $false
        source = ""
        uri = ""
        payload = $null
        direct_error = $directError
    }
}

$hostProfile = Get-HostProfile
$StrictVpsChecks = [bool]$hostProfile.strict_vps_checks

Write-Host "`n=== 0. HOST PROFILE ===" -ForegroundColor Cyan
Say "Host profile: $($hostProfile.name) on $env:COMPUTERNAME" $true
if (-not $StrictVpsChecks) {
    Say "VPS-targeted expectations are advisory on this host" $null
}

Write-Host "`n=== 1. RUNNING PROCESSES ===" -ForegroundColor Cyan
$py = (Get-Process python* -ErrorAction SilentlyContinue).Count
Say "Python processes running ($py found)" (ScopeResult ($py -gt 2))

$java = (Get-Process java* -ErrorAction SilentlyContinue).Count
Say "Java (IBKR Gateway) running ($java found)" (ScopeResult ($java -gt 0))

$cf = (Get-Process cloudflared* -ErrorAction SilentlyContinue).Count
Say "Cloudflared running ($cf found)" (ScopeResult ($cf -gt 0))

$caddy = (Get-Process caddy* -ErrorAction SilentlyContinue).Count
Say "Caddy edge proxy running ($caddy found)" (ScopeResult ($caddy -gt 0))

Write-Host "`n=== 2. SCHEDULED TASKS ===" -ForegroundColor Cyan
$tasks = @(
    "ETA-Dashboard","ETA-Jarvis-Live","ETA-Avengers-Fleet",
    "ETA-Executor-DashboardAssemble","ETA-Executor-LogCompact","ETA-Executor-PromptWarmup",
    "ETA-Executor-AuditSummarize","ETA-Executor-LogRotate","ETA-Executor-DiskCleanup",
    "ETA-Executor-PrometheusExport",
    "ETA-Steward-ShadowTick","ETA-Steward-DriftSummary","ETA-Steward-KaizenRetro",
    "ETA-Steward-DistillTrain","ETA-Steward-MetaUpgrade","ETA-Steward-HealthWatchdog",
    "ETA-Steward-SelfTest","ETA-Steward-Backup",
    "ETA-Reasoner-TwinVerdict","ETA-Reasoner-StrategyMine","ETA-Reasoner-CausalReview",
    "ETA-Reasoner-DoctrineReview",
    "ETA-HealthCheck","ETA-Quantum-Daily-Rebalance",
    "ETA-DeepSeek-MachineGate","ETA-DeepSeek-CodexLane","ETA-DeepSeek-Combined",
    "ETA-Hermes-Jarvis-Flush","ApexIbkrGatewayWatchdog",
    "ETA-BTC-Fleet","ETA-MNQ-Supervisor",
    "ETA-Cloudflare-Tunnel","ETA-Cloudflare-Quick-Tunnel","ETA-Dashboard-Live"
)

$running = 0; $stopped = 0; $missing = 0
foreach ($t in $tasks) {
    $info = schtasks /query /tn $t /fo csv 2>$null | ConvertFrom-Csv -ErrorAction SilentlyContinue
    if (-not $info) { $missing++ }
    elseif ($info.Status -eq "Ready") { $running++; $stopped++ }
    elseif ($info.Status -eq "Running") { $running++ }
    else { $stopped++ }
}
Say "Scheduled tasks: $running running, $stopped ready/stopped, $missing missing" (ScopeResult ($missing -eq 0))

if ($missing -gt 0) {
    $missingColor = if ($StrictVpsChecks) { "Red" } else { "Yellow" }
    foreach ($t in $tasks) {
        $info = schtasks /query /tn $t /fo csv 2>$null | ConvertFrom-Csv -ErrorAction SilentlyContinue
        if (-not $info) { Write-Host "         MISSING: $t" -ForegroundColor $missingColor }
    }
}

$taskAuditScript = "C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\audit_vps_scheduled_tasks.ps1"
if (Test-Path $taskAuditScript) {
    try {
        $taskAuditRaw = & powershell -NoProfile -ExecutionPolicy Bypass -File $taskAuditScript -Json 2>$null
        $taskAudit = (($taskAuditRaw -join "`n") | ConvertFrom-Json -ErrorAction Stop)
        $healthDrift = @($taskAudit.needs_attention | Where-Object { $_.task_name -eq "ETA-HealthCheck" }) |
            Select-Object -First 1
        if ($healthDrift) {
            Say "ETA-HealthCheck contract drift: $($healthDrift.healthcheck_contract_issue)" $false
        } else {
            Say "ETA-HealthCheck contract drift" $true
        }
        $schedulerAttentionTasks = @($taskAudit.scheduler_attention_task_names | Where-Object { $_ })
        $schedulerAttentionRepair = [string]$taskAudit.scheduler_attention_repair_command
        if ($schedulerAttentionRepair) {
            $schedulerAttentionLabel = if ($schedulerAttentionTasks.Count -gt 0) {
                " ($($schedulerAttentionTasks -join ", "))"
            } else {
                ""
            }
            Say "ETA scheduler attention repair${schedulerAttentionLabel}: $schedulerAttentionRepair" $null
        }
    } catch {
        Say "ETA-HealthCheck contract audit unavailable" $null
    }
} else {
    Say "ETA-HealthCheck audit script missing" $null
}

Write-Host "`n=== 3. WINDOWS SERVICES ===" -ForegroundColor Cyan
$services = @("FirmCore","FirmWatchdog","FirmCommandCenter","FirmCommandCenterTunnel",
              "FirmCommandCenterEdge","HermesJarvisTelegram")
$firmCommandCenterRepair = ".\eta_engine\deploy\scripts\repair_firm_command_center_env_admin.cmd"
foreach ($svc in $services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if (-not $s) { Say "Service $svc" (ScopeResult $false) }
    else { Say "Service $svc ($($s.Status))" (ScopeResult ($s.Status -eq "Running")) }
}
$fccServiceXmlPath = "C:\EvolutionaryTradingAlgo\firm_command_center\services\FirmCommandCenter.xml"
if (Test-Path $fccServiceXmlPath) {
    try {
        [xml]$fccServiceXml = Get-Content $fccServiceXmlPath -Raw
        $fccPython = [string]$fccServiceXml.service.executable
        if ($fccPython) {
            Say "FirmCommandCenter runtime Python: $fccPython" (Test-Path $fccPython)
        }
        $fccArguments = [string]$fccServiceXml.service.arguments
        if ($fccArguments) {
            Say "FirmCommandCenter entrypoint: $fccArguments" $true
        }
    } catch {
        Say "FirmCommandCenter service XML unreadable" $null
    }
}
$fccErrLog = "C:\EvolutionaryTradingAlgo\firm_command_center\var\logs\FirmCommandCenter.err.log"
$watchdogStatusPath = "C:\EvolutionaryTradingAlgo\var\ops\command_center_watchdog_status_latest.json"
if (Test-Path $fccErrLog) {
    $fccBootError = Get-Content $fccErrLog -Tail 40 -ErrorAction SilentlyContinue |
        Where-Object { $_ -match "ModuleNotFoundError|ImportError" } |
        Select-Object -Last 1
    if ($fccBootError) {
        Say "FirmCommandCenter bootstrap error: $fccBootError" $null
    }
    $fccMissingModule = Get-Content $fccErrLog -Tail 40 -ErrorAction SilentlyContinue |
        Select-String -Pattern "No module named '([^']+)'" |
        Select-Object -Last 1
    if ($fccMissingModule) {
        $missingModule = $fccMissingModule.Matches[0].Groups[1].Value
        Say "FirmCommandCenter dependency gap: missing module $missingModule" $null
        Say "FirmCommandCenter env repair: $firmCommandCenterRepair" $null
    }
}
if (Test-Path $watchdogStatusPath) {
    try {
        $watchdogStatus = Get-Content $watchdogStatusPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $watchdogTaskContract = $watchdogStatus.watchdog_task_contract_status
        if ($watchdogTaskContract -and [string]$watchdogTaskContract.status -and [string]$watchdogTaskContract.status -ne "healthy") {
            $watchdogTaskSummary = if ($watchdogTaskContract.summary) { [string]$watchdogTaskContract.summary } else { [string]$watchdogTaskContract.status }
            Say "Eta-CommandCenter-Doctor task contract: $watchdogTaskSummary" $null
        }
        $dashboardTaskContract = $watchdogStatus.dashboard_task_contract_status
        if ($dashboardTaskContract -and [string]$dashboardTaskContract.status -and [string]$dashboardTaskContract.status -ne "healthy") {
            $dashboardTaskSummary = if ($dashboardTaskContract.summary) { [string]$dashboardTaskContract.summary } else { [string]$dashboardTaskContract.status }
            Say "ETA dashboard task contract: $dashboardTaskSummary" $null
        }
        $operatorIssueSummary = if ($watchdogStatus.display_issue_summary) {
            [string]$watchdogStatus.display_issue_summary
        } elseif ($watchdogStatus.display_summary) {
            [string]$watchdogStatus.display_summary
        } elseif ($watchdogStatus.issue_summary) {
            ([string]$watchdogStatus.issue_summary) -replace '; findings=.*$', ''
        } else {
            ""
        }
        if (-not [string]::IsNullOrWhiteSpace($operatorIssueSummary)) {
            Say "Command Center watchdog issue: $operatorIssueSummary" $null
        }
        $operatorNextStep = if ($watchdogStatus.operator_next_step) { [string]$watchdogStatus.operator_next_step } else { "" }
        if (-not [string]::IsNullOrWhiteSpace($operatorNextStep) -and $operatorNextStep -ne "none") {
            $operatorNextReason = if ($watchdogStatus.operator_next_reason) { [string]$watchdogStatus.operator_next_reason } else { "" }
            $operatorNextSummary = if (-not [string]::IsNullOrWhiteSpace($operatorNextReason)) {
                "$operatorNextReason -> $operatorNextStep"
            } else {
                $operatorNextStep
            }
            Say "Command Center operator next step: $operatorNextSummary" $null
            if ($watchdogStatus.operator_next_command) {
                Say "Command Center operator command: $([string]$watchdogStatus.operator_next_command)" $null
            }
        }
        $localContract = $watchdogStatus.local_contract_status
        if ($localContract -and [string]$localContract.status -and [string]$localContract.status -ne "healthy") {
            $localContractSummary = [string]$localContract.summary
            if (-not [string]::IsNullOrWhiteSpace($localContractSummary)) {
                Say "Local 8421 contract symptom: $localContractSummary" $null
            } else {
                Say "Local 8421 contract status: $($localContract.status)" $null
            }
            if ([string]$localContract.status -eq "upstream_failure" -and $localContract.probes) {
                $probeCodes = @(
                    "openapi=$($localContract.probes.openapi.status_code)",
                    "diagnostics=$($localContract.probes.diagnostics.status_code)",
                    "card_health=$($localContract.probes.card_health.status_code)"
                ) -join " "
                Say "Local 8421 upstream probe HTTP codes: $probeCodes" $null
            }
        }
        $repairPending = [bool]$watchdogStatus.operator_repair_prompt_pending
        if ($repairPending) {
            $pendingCommand = if ($watchdogStatus.operator_repair_pending_command) {
                [string]$watchdogStatus.operator_repair_pending_command
            } else {
                $firmCommandCenterRepair
            }
            Say "FirmCommandCenter env repair pending UAC approval: $pendingCommand" $null
        }
    } catch {
        Say "Command Center watchdog status unreadable" $null
    }
}
$etaReadinessPath = "C:\EvolutionaryTradingAlgo\var\ops\eta_readiness_snapshot_latest.json"
$localRetuneStatusPath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\diamond_retune_status_latest.json"
if (Test-Path $etaReadinessPath) {
    try {
        $etaReadiness = Get-Content $etaReadinessPath -Raw | ConvertFrom-Json
        $readinessCheckedAtUtc = ""
        if ($null -ne $etaReadiness.checked_at_utc -and
            -not [string]::IsNullOrWhiteSpace([string]$etaReadiness.checked_at_utc)) {
            $readinessCheckedAtUtc = [string]$etaReadiness.checked_at_utc
        } elseif ($null -ne $etaReadiness.checked_at -and
            -not [string]::IsNullOrWhiteSpace([string]$etaReadiness.checked_at)) {
            $readinessCheckedAtUtc = [string]$etaReadiness.checked_at
        }
        $fallbackReason = [string]$etaReadiness.public_fallback_reason
        $readinessStatus = [string]$etaReadiness.status
        $readinessPrimaryBlocker = [string]$etaReadiness.primary_blocker
        $readinessDetail = [string]$etaReadiness.detail
        $readinessPrimaryAction = [string]$etaReadiness.primary_action
        $bracketsSummary = [string]$etaReadiness.brackets_summary
        $bracketsNextAction = [string]$etaReadiness.brackets_next_action
        $fallbackAction = [string]$etaReadiness.public_fallback_primary_action
        $fallbackLiveBrokerOpenOrderCount = [int]$etaReadiness.public_live_broker_open_order_count
        $fallbackBrokerOpenOrderCount = [int]$etaReadiness.public_fallback_broker_open_order_count
        $fallbackBrokerOrderDriftDisplay = [string]$etaReadiness.public_fallback_broker_open_order_drift_display
        $dashboardApiRuntimeDriftDisplay = [string]$etaReadiness.dashboard_api_runtime_drift_display
        $dashboardApiRuntimeRetuneDriftDisplay = [string]$etaReadiness.dashboard_api_runtime_retune_drift_display
        $dashboardApiRuntimeProbeDisplay = [string]$etaReadiness.dashboard_api_runtime_probe_display
        $dashboardApiRuntimeRefreshCommand = [string]$etaReadiness.dashboard_api_runtime_refresh_command
        $dashboardApiRuntimeRefreshRequiresElevation = [bool]$etaReadiness.dashboard_api_runtime_refresh_requires_elevation
        $fallbackStaleDisplay = [string]$etaReadiness.public_fallback_stale_flat_open_order_display
        $fallbackStaleRelationDisplay = [string]$etaReadiness.public_fallback_stale_flat_open_order_relation_display
        $publicLiveBrokerDegradedDisplay = [string]$etaReadiness.public_live_broker_degraded_display
        $currentPublicLiveBrokerDegradedDisplay = ""
        if ($fallbackReason) {
            try {
                $liveBrokerState = Invoke-RestMethod -Uri "https://ops.evolutionarytradingalgo.com/api/live/broker_state" -Headers @{
                    Accept = "application/json"
                    "User-Agent" = "ETA-Operator/1.0"
                } -TimeoutSec 10 -ErrorAction Stop
                if ($liveBrokerState -and -not [bool]$liveBrokerState.ready) {
                    $degradedReason = [string]$liveBrokerState.broker_snapshot_source
                    if ([string]::IsNullOrWhiteSpace($degradedReason)) {
                        $degradedReason = [string]$liveBrokerState.broker_snapshot_state
                    }
                    if ([string]::IsNullOrWhiteSpace($degradedReason)) {
                        $degradedReason = [string]$liveBrokerState.source
                    }
                    if (-not [string]::IsNullOrWhiteSpace($degradedReason)) {
                        $currentPublicLiveBrokerDegradedDisplay = "live broker_state now degraded: $degradedReason"
                        $liveBrokerSource = [string]$liveBrokerState.source
                        if (-not [string]::IsNullOrWhiteSpace($liveBrokerSource) -and $liveBrokerSource -ne $degradedReason) {
                            $currentPublicLiveBrokerDegradedDisplay += "; via $liveBrokerSource"
                        }
                    }
                }
            } catch {
            }
        }
        $publicLiveRetuneGeneratedAtUtc = [string]$etaReadiness.public_live_retune_generated_at_utc
        $publicLiveRetuneSyncDriftDisplay = [string]$etaReadiness.public_live_retune_sync_drift_display
        $currentPublicRetuneGeneratedAtUtc = ""
        $currentPublicRetuneOutcomeLine = ""
        $currentPublicRetuneSyncDriftDisplay = ""
        if ($fallbackReason -or ($readinessAgeS -gt 300)) {
            try {
                $currentPublicRetune = Invoke-RestMethod -Uri "https://ops.evolutionarytradingalgo.com/api/jarvis/diamond_retune_status" -Headers @{
                    "Accept" = "application/json"
                    "User-Agent" = "ETA-Operator/1.0"
                } -TimeoutSec 10 -ErrorAction Stop
                $currentPublicRetuneGeneratedAtUtc = [string]$currentPublicRetune.generated_at_utc
                if ([string]::IsNullOrWhiteSpace($currentPublicRetuneGeneratedAtUtc)) {
                    $currentPublicRetuneGeneratedAtUtc = [string]$currentPublicRetune.generated_at
                }
                $currentPublicRetuneOutcomeLine = [string]$currentPublicRetune.focus_active_experiment_outcome_line
                $receiptPublicRetuneOutcomeLine = [string]$etaReadiness.public_live_retune_focus_active_experiment_outcome_line
                if (
                    -not [string]::IsNullOrWhiteSpace($currentPublicRetuneGeneratedAtUtc) -and
                    $currentPublicRetuneGeneratedAtUtc -ne $publicLiveRetuneGeneratedAtUtc
                ) {
                    $cachedPublicRetuneStamp = if ([string]::IsNullOrWhiteSpace($publicLiveRetuneGeneratedAtUtc)) {
                        "no public retune timestamp"
                    } else {
                        $publicLiveRetuneGeneratedAtUtc
                    }
                    $currentPublicRetuneSyncDriftDisplay = (
                        "public retune truth now refreshed at {0} after readiness cached {1}" -f
                        $currentPublicRetuneGeneratedAtUtc,
                        $cachedPublicRetuneStamp
                    )
                } elseif (
                    -not [string]::IsNullOrWhiteSpace($currentPublicRetuneOutcomeLine) -and
                    $currentPublicRetuneOutcomeLine -ne $receiptPublicRetuneOutcomeLine
                ) {
                    $cachedPublicRetuneOutcome = if ([string]::IsNullOrWhiteSpace($receiptPublicRetuneOutcomeLine)) {
                        "no public retune outcome"
                    } else {
                        $receiptPublicRetuneOutcomeLine
                    }
                    $currentPublicRetuneSyncDriftDisplay = (
                        "public retune outcome now says {0} vs readiness cached {1}" -f
                        $currentPublicRetuneOutcomeLine,
                        $cachedPublicRetuneOutcome
                    )
                }
            } catch {
            }
        }
        $cachedLocalRetuneGeneratedAtUtc = [string]$etaReadiness.local_retune_generated_at_utc
        $currentLocalRetuneGeneratedAtUtc = [string]$etaReadiness.current_local_retune_generated_at_utc
        $retuneDriftDisplay = [string]$etaReadiness.retune_focus_active_experiment_drift_display
        $localRetuneSyncDriftDisplay = [string]$etaReadiness.local_retune_sync_drift_display
        $fallbackStaleCount = [int]$etaReadiness.public_fallback_stale_flat_open_order_count
        $fallbackStaleSymbols = @()
        if ($etaReadiness.public_fallback_stale_flat_open_order_symbols -is [System.Collections.IEnumerable] -and
            -not ($etaReadiness.public_fallback_stale_flat_open_order_symbols -is [string])) {
            $fallbackStaleSymbols = @(
                $etaReadiness.public_fallback_stale_flat_open_order_symbols |
                ForEach-Object { [string]$_ } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
            )
        }
        if ($fallbackReason) {
            Say "ETA readiness public fallback: $fallbackReason" $null
        }
        if ($readinessStatus) {
            Say "ETA readiness status: $readinessStatus" $null
        }
        if ($readinessPrimaryBlocker) {
            Say "ETA readiness primary blocker: $readinessPrimaryBlocker" $null
        }
        if ($readinessDetail) {
            Say "ETA readiness detail: $readinessDetail" $null
        }
        if ($readinessPrimaryAction) {
            Say "ETA readiness primary action: $readinessPrimaryAction" $null
        }
        if ($bracketsSummary) {
            Say "ETA readiness brackets: $bracketsSummary" $null
        }
        if ($bracketsNextAction) {
            Say "ETA readiness brackets next: $bracketsNextAction" $null
        }
        if ($readinessCheckedAtUtc) {
            try {
                $readinessCheckedAtStamp = [datetimeoffset]::Parse($readinessCheckedAtUtc)
                $readinessAgeS = [int][Math]::Max(0, ((Get-Date).ToUniversalTime() - $readinessCheckedAtStamp.UtcDateTime).TotalSeconds)
                $readinessFreshness = if ($readinessAgeS -le 300) { "fresh" } else { "stale" }
                Say "ETA readiness receipt freshness: $readinessFreshness (${readinessAgeS}s old)" $null
                if ($readinessAgeS -gt 300) {
                    Say "ETA readiness refresh command: .\scripts\eta-readiness-snapshot.ps1" $null
                }
            } catch {
            }
        }
        if ($publicLiveRetuneGeneratedAtUtc) {
            Say "ETA readiness public retune generated: $publicLiveRetuneGeneratedAtUtc" $null
        }
        if ($publicLiveRetuneSyncDriftDisplay) {
            Say "ETA readiness public retune sync drift: $publicLiveRetuneSyncDriftDisplay" $null
        }
        if ($currentPublicRetuneGeneratedAtUtc -and $currentPublicRetuneGeneratedAtUtc -ne $publicLiveRetuneGeneratedAtUtc) {
            Say "ETA readiness current public retune generated: $currentPublicRetuneGeneratedAtUtc" $null
        }
        if ($currentPublicRetuneOutcomeLine -and (
            $currentPublicRetuneGeneratedAtUtc -ne $publicLiveRetuneGeneratedAtUtc -or
            $currentPublicRetuneSyncDriftDisplay
        )) {
            Say "ETA readiness current public retune outcome: $currentPublicRetuneOutcomeLine" $null
        }
        if ($currentPublicRetuneSyncDriftDisplay) {
            Say "ETA readiness current public retune sync drift: $currentPublicRetuneSyncDriftDisplay" $null
        }
        if ($cachedLocalRetuneGeneratedAtUtc) {
            Say "ETA readiness cached local retune generated: $cachedLocalRetuneGeneratedAtUtc" $null
        }
        if ($fallbackBrokerOpenOrderCount -gt 0) {
            Say "ETA readiness broker open orders: $fallbackBrokerOpenOrderCount" $null
        }
        if ($fallbackLiveBrokerOpenOrderCount -gt 0) {
            Say "ETA readiness live broker_state open orders: $fallbackLiveBrokerOpenOrderCount" $null
        }
        if ($fallbackStaleDisplay) {
            Say "ETA readiness stale broker orders: $fallbackStaleDisplay" $null
        } elseif ($fallbackStaleCount -gt 0) {
            $symbolsLabel = if ($fallbackStaleSymbols.Count -gt 0) {
                " ($($fallbackStaleSymbols -join ", "))"
            } else {
                ""
            }
            Say "ETA readiness stale broker orders: $fallbackStaleCount$symbolsLabel" $null
        }
        if ($fallbackStaleRelationDisplay) {
            Say "ETA readiness stale-order pressure: $fallbackStaleRelationDisplay" $null
        }
        if ($publicLiveBrokerDegradedDisplay) {
            Say "ETA readiness public broker_state degraded: $publicLiveBrokerDegradedDisplay" $null
        }
        if (
            $currentPublicLiveBrokerDegradedDisplay -and
            $currentPublicLiveBrokerDegradedDisplay -ne $publicLiveBrokerDegradedDisplay
        ) {
            Say "ETA readiness current live broker_state degraded: $currentPublicLiveBrokerDegradedDisplay" $null
        }
        if ($fallbackBrokerOrderDriftDisplay) {
            Say "ETA readiness broker-order drift: $fallbackBrokerOrderDriftDisplay" $null
        }
        $masterStatus = $null
        try {
            $masterStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8421/api/master/status" -TimeoutSec 10 -ErrorAction Stop
        } catch {
            $masterStatus = $null
        }
        $currentLiveBrokerOpenOrderCount = 0
        $currentLiveBrokerOpenOrderDriftDisplay = ""
        if ($masterStatus) {
            $currentLiveBrokerOpenOrderCount = [int]$masterStatus.current_live_broker_open_order_count
            $currentLiveBrokerOpenOrderDriftDisplay = [string]$masterStatus.current_live_broker_open_order_drift_display
        }
        if ($dashboardApiRuntimeDriftDisplay) {
            Say "ETA readiness dashboard API runtime drift: $dashboardApiRuntimeDriftDisplay" $null
        }
        if ($dashboardApiRuntimeRetuneDriftDisplay) {
            Say "ETA readiness dashboard API runtime retune drift: $dashboardApiRuntimeRetuneDriftDisplay" $null
        }
        if ($dashboardApiRuntimeProbeDisplay) {
            Say "ETA readiness dashboard API runtime probe: $dashboardApiRuntimeProbeDisplay" $null
        }
        if (($dashboardApiRuntimeDriftDisplay -or $dashboardApiRuntimeRetuneDriftDisplay -or $dashboardApiRuntimeProbeDisplay) -and $dashboardApiRuntimeRefreshCommand) {
            Say "ETA readiness dashboard API runtime refresh: $dashboardApiRuntimeRefreshCommand" $null
            if ($dashboardApiRuntimeRefreshRequiresElevation) {
                Say "ETA readiness dashboard API runtime refresh requires elevation: true" $null
            }
        } elseif ($currentLiveBrokerOpenOrderDriftDisplay) {
            Say "ETA readiness dashboard API runtime drift: $currentLiveBrokerOpenOrderDriftDisplay" $null
        } elseif ($fallbackLiveBrokerOpenOrderCount -gt 0 -and $currentLiveBrokerOpenOrderCount -le 0) {
            Say (
                "ETA readiness dashboard API runtime drift: 8421 master/status is still blank for " +
                "current_live_broker_open_order_count while readiness receipt has $fallbackLiveBrokerOpenOrderCount"
            ) $null
        } elseif (
            $fallbackLiveBrokerOpenOrderCount -gt 0 -and
            $currentLiveBrokerOpenOrderCount -gt 0 -and
            $currentLiveBrokerOpenOrderCount -ne $fallbackLiveBrokerOpenOrderCount
        ) {
            Say (
                "ETA readiness dashboard API runtime drift: 8421 master/status reports " +
                "$currentLiveBrokerOpenOrderCount current live broker open orders while readiness receipt has " +
                "$fallbackLiveBrokerOpenOrderCount"
            ) $null
        }
        if ($retuneDriftDisplay) {
            Say ("ETA readiness retune mirror drift: {0}" -f $retuneDriftDisplay) $null
        }
        if ((-not $localRetuneSyncDriftDisplay -or -not $currentLocalRetuneGeneratedAtUtc) -and (Test-Path $localRetuneStatusPath)) {
            try {
                $localRetuneStatus = Get-Content $localRetuneStatusPath -Raw | ConvertFrom-Json
                if (-not $currentLocalRetuneGeneratedAtUtc) {
                    if ($null -ne $localRetuneStatus.generated_at_utc -and
                        -not [string]::IsNullOrWhiteSpace([string]$localRetuneStatus.generated_at_utc)) {
                        $currentLocalRetuneGeneratedAtUtc = [string]$localRetuneStatus.generated_at_utc
                    } elseif ($null -ne $localRetuneStatus.generated_at -and
                        -not [string]::IsNullOrWhiteSpace([string]$localRetuneStatus.generated_at)) {
                        $currentLocalRetuneGeneratedAtUtc = [string]$localRetuneStatus.generated_at
                    }
                }
                if ((-not $localRetuneSyncDriftDisplay) -and $currentLocalRetuneGeneratedAtUtc -and $cachedLocalRetuneGeneratedAtUtc) {
                    try {
                        $currentLocalRetuneStamp = [datetimeoffset]::Parse($currentLocalRetuneGeneratedAtUtc)
                        $cachedLocalRetuneStamp = [datetimeoffset]::Parse($cachedLocalRetuneGeneratedAtUtc)
                        if ($currentLocalRetuneStamp -gt $cachedLocalRetuneStamp) {
                            $localRetuneSyncDriftDisplay = (
                                "local retune snapshot refreshed at {0} after readiness cached {1}" -f
                                $currentLocalRetuneGeneratedAtUtc,
                                $cachedLocalRetuneGeneratedAtUtc
                            )
                        } elseif ($currentLocalRetuneGeneratedAtUtc -ne $cachedLocalRetuneGeneratedAtUtc) {
                            $localRetuneSyncDriftDisplay = (
                                "local retune snapshot timestamp {0} differs from readiness cached {1}" -f
                                $currentLocalRetuneGeneratedAtUtc,
                                $cachedLocalRetuneGeneratedAtUtc
                            )
                        }
                    } catch {
                        if ($currentLocalRetuneGeneratedAtUtc -ne $cachedLocalRetuneGeneratedAtUtc) {
                            $localRetuneSyncDriftDisplay = (
                                "local retune snapshot timestamp {0} differs from readiness cached {1}" -f
                                $currentLocalRetuneGeneratedAtUtc,
                                $cachedLocalRetuneGeneratedAtUtc
                            )
                        }
                    }
                } elseif ((-not $localRetuneSyncDriftDisplay) -and $currentLocalRetuneGeneratedAtUtc) {
                    $localRetuneSyncDriftDisplay = (
                        "local retune snapshot refreshed at {0} but readiness cached no local retune timestamp" -f
                        $currentLocalRetuneGeneratedAtUtc
                    )
                }
            } catch {
                $localRetuneSyncDriftDisplay = ""
            }
        }
        if (
            $currentLocalRetuneGeneratedAtUtc -and
            $currentLocalRetuneGeneratedAtUtc -ne $cachedLocalRetuneGeneratedAtUtc
        ) {
            Say "ETA readiness current local retune generated: $currentLocalRetuneGeneratedAtUtc" $null
        }
        if ($localRetuneSyncDriftDisplay) {
            Say ("ETA readiness local retune sync drift: {0}" -f $localRetuneSyncDriftDisplay) $null
        }
        if ($fallbackAction) {
            Say "ETA readiness fallback action: $fallbackAction" $null
        }
    } catch {
        Say "ETA readiness snapshot unreadable" $null
    }
}

Write-Host "`n=== 4. PORT LISTENING ===" -ForegroundColor Cyan
$ports = @{
    4002 = "IBKR TWS API"
    8000 = "Dashboard API"
    8421 = "Dashboard proxy"
    8422 = "Force Multiplier status"
}
foreach ($p in $ports.Keys) {
    $listening = netstat -ano 2>$null | Select-String "LISTENING" | Select-String ":$p "
    Say "Port $p ($($ports[$p]))" (ScopeResult ($listening -ne $null))
}

Write-Host "`n=== 5. IBKR GATEWAY CONNECTIVITY ===" -ForegroundColor Cyan
$ibkrTws = netstat -ano 2>$null | Select-String "LISTENING" | Select-String ":4002 "
Say "IBKR TWS API listening on 127.0.0.1:4002" (ScopeResult ($ibkrTws -ne $null))

Write-Host "`n=== 6. DEEPSEEK API KEY ===" -ForegroundColor Cyan
$envFile = Join-Path $env:USERPROFILE "eta_engine\.env"
if (Test-Path $envFile) {
    $key = Select-String -Path $envFile -Pattern "DEEPSEEK_API_KEY=(\S+)" | ForEach-Object { $_.Matches.Groups[1].Value }
    if ($key) { Say "DeepSeek API key present in .env" $true }
    else { Say "DeepSeek API key missing in .env" $false }
} else {
    $envFileAlt = "C:\EvolutionaryTradingAlgo\eta_engine\.env"
    if (Test-Path $envFileAlt) {
        $key = Select-String -Path $envFileAlt -Pattern "DEEPSEEK_API_KEY=(\S+)" | ForEach-Object { $_.Matches.Groups[1].Value }
        if ($key) { Say "DeepSeek API key present in .env" $true }
        else { Say "DeepSeek API key missing" $false }
    } else { Say ".env file not found" $false }
}

Write-Host "`n=== 7. DASHBOARD / API HEALTH ===" -ForegroundColor Cyan
try {
    $botFleetProbe = Get-BotFleetProbe
    if (-not $botFleetProbe.ok) {
        throw $botFleetProbe.direct_error
    }
    $data = $botFleetProbe.payload
    $botCount = ($data | Get-Member -MemberType NoteProperty).Count
    $apiLabel = if ($botFleetProbe.source -eq "proxy_8421") {
        "Bot fleet API: OK ($botCount bots returned via proxy 8421 after direct 8000 miss)"
    } else {
        "Bot fleet API: OK ($botCount bots returned)"
    }
    Say $apiLabel (ScopeResult $true)

    $active = 0; $errors = 0
    foreach ($prop in $data.PSObject.Properties) {
        $status = $prop.Value.status
        if ($status -eq "active" -or $status -eq "paper_sim") { $active++ }
        if ($status -eq "error") { $errors++ }
    }
    Write-Host "         Active/sim: $active  |  Error: $errors" -ForegroundColor Gray
    Say "Bots not in error state" (ScopeResult ($errors -eq 0))
    if ($botFleetProbe.source -eq "proxy_8421" -and $botFleetProbe.direct_error) {
        Say "Dashboard direct API probe missed; proxy recovered: $($botFleetProbe.direct_error)" $null
    }
} catch {
    Say "Dashboard API unreachable (direct 8000 and proxy 8421)" (ScopeResult $false)
}

Write-Host "`n=== 8. ENGINE LOGS (recent entries) ===" -ForegroundColor Cyan
$logDirs = @(
    "C:\EvolutionaryTradingAlgo\eta_engine\var\logs",
    "C:\EvolutionaryTradingAlgo\firm_command_center\var\logs"
)
$anyLogs = $false
foreach ($dir in $logDirs) {
    if (-not (Test-Path $dir)) { continue }
    $logs = Get-ChildItem $dir -Filter "*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 3
    foreach ($log in $logs) {
        $anyLogs = $true
        $age = [int]((Get-Date) - $log.LastWriteTime).TotalMinutes
        $fresh = $age -lt 60
        $mark = if ($fresh) { "[PASS]" } else { "[WARN]" }
        $color = if ($fresh) { "Green" } else { "Yellow" }
        Write-Host "  $mark" -ForegroundColor $color -NoNewline
        Write-Host " $($log.Name) (modified ${age}m ago)" -ForegroundColor White
        $lines = Get-Content $log.FullName -Tail 5 -ErrorAction SilentlyContinue |
                 Where-Object { $_.Trim() -ne "" } | Select-Object -Last 2
        foreach ($l in $lines) { Write-Host "           $l" -ForegroundColor DarkGray }
    }
}
if (-not $anyLogs) {
    Say "No recent engine logs found under canonical log roots" (ScopeResult $false)
}

Write-Host "`n=== 9. PYTHON MODULE SMOKE ===" -ForegroundColor Cyan
$venvPython = if ($fccPython -and (Test-Path $fccPython)) {
    $fccPython
} else {
    "C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe"
}
$fallbackPython = $null
if (-not (Test-Path $venvPython)) {
    $fallbackPython = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
}
$smokePython = if (Test-Path $venvPython) { $venvPython } else { $fallbackPython }
if ($smokePython) {
    $test = & $smokePython -c "import sys; sys.path.insert(0, r'C:\EvolutionaryTradingAlgo'); sys.path.insert(0, r'C:\EvolutionaryTradingAlgo\eta_engine'); from eta_engine.strategies import per_bot_registry; from eta_engine.scripts import workspace_roots; print('import OK')" 2>&1
    if ($test -eq "import OK") { Say "Core Python imports (per_bot_registry, workspace_roots)" $true }
    else { Say "Core Python imports failed: $test" $false }
} else {
    Say "Python runtime not found for module smoke" (ScopeResult $false)
}

Write-Host "`n=== 10. DISK SPACE ===" -ForegroundColor Cyan
$disk = Get-PSDrive C
$freeGB = [math]::Round($disk.Free / 1GB, 1)
Say "Free disk space: ${freeGB}GB" ($freeGB -gt 5)

Write-Host "`n=== 11. ENGINE HEARTBEAT ===" -ForegroundColor Cyan
$healthPath = "C:\EvolutionaryTradingAlgo\eta_engine\data\runtime_supervisor_health.json"
if (Test-Path $healthPath) {
    try {
        $health = Get-Content $healthPath -Raw | ConvertFrom-Json
        $lastBeat = $health.last_heartbeat
        if ($lastBeat) {
            $ageMin = [math]::Round(((Get-Date) - [datetime]$lastBeat).TotalMinutes, 1)
            $fresh = $ageMin -lt 15
            $mark = if ($fresh) { "PASS" } else { "WARN" }
            Say "Last heartbeat: $lastBeat (${ageMin}m ago) [$mark]" (ScopeResult $fresh)
        } else { Say "Heartbeat field missing" (ScopeResult $false) }
    } catch { Say "Health JSON parse failed" (ScopeResult $false) }
} else {
    Say "Health file not found: $healthPath" (ScopeResult $false)
}

Write-Host "`n=== 12. JARVIS STATE ===" -ForegroundColor Cyan
$jarvisPath = "C:\EvolutionaryTradingAlgo\eta_engine\data\jarvis_memory.json"
if (Test-Path $jarvisPath) {
    $ageMin = [math]::Round(((Get-Date) - (Get-Item $jarvisPath).LastWriteTime).TotalMinutes, 1)
    $fresh = $ageMin -lt 30
    $mark = if ($fresh) { "PASS" } else { "WARN" }
    Say "Jarvis memory updated ${ageMin}m ago [$mark]" (ScopeResult $fresh)
    try {
        $j = Get-Content $jarvisPath -Raw | ConvertFrom-Json
        $mode = $j.eta_mode
        $stress = $j.system_stress
        if ($mode) { Write-Host "         Mode: $mode" -ForegroundColor Gray }
        if ($null -ne $stress) { Write-Host "         Stress: $stress" -ForegroundColor Gray }
    } catch {}
} else {
    Say "Jarvis memory file not found" (ScopeResult $false)
}

Write-Host "`n=== DIAGNOSTICS SUMMARY ===" -ForegroundColor Cyan
Write-Host "  PASS: $PASS  |  WARN: $WARN  |  FAIL: $FAIL" -ForegroundColor White
if (-not $StrictVpsChecks) {
    Write-Host "  VERDICT: LOCAL_WORKSTATION (VPS expectations advisory)" -ForegroundColor Yellow
} elseif ($FAIL -eq 0 -and $WARN -le 2) {
    Write-Host "  VERDICT: HEALTHY" -ForegroundColor Green
} elseif ($FAIL -eq 0) {
    Write-Host "  VERDICT: DEGRADED (warnings present)" -ForegroundColor Yellow
} else {
    Write-Host "  VERDICT: UNHEALTHY (failures above)" -ForegroundColor Red
}
Write-Host ""
