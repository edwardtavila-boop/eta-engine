$e = "SilentlyContinue"; $p = 0; $f = 0; $w = 0

function S($l, $o) {
    if ($o) {
        Write-Host "  [PASS] $l" -ForegroundColor Green
        $global:p++
    } elseif ($null -eq $o) {
        Write-Host "  [WARN] $l" -ForegroundColor Yellow
        $global:w++
    } else {
        Write-Host "  [FAIL] $l" -ForegroundColor Red
        $global:f++
    }
}

function Get-HostProfileCompact {
    $marker = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\gateway_authority.json"
    $server = $false
    try {
        $server = ((Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).ProductType -ne 1)
    } catch {}
    $match = $false
    if (Test-Path $marker) {
        try {
            $payload = Get-Content $marker -Raw | ConvertFrom-Json -ErrorAction Stop
            $role = [string]$payload.role
            $enabled = if ($null -ne $payload.enabled) { [bool]$payload.enabled } else { $false }
            $computer = [string]$payload.computer_name
            $roleOk = @("vps", "gateway_authority") -contains $role.Trim().ToLowerInvariant()
            $match = (
                $enabled -and
                $roleOk -and
                $computer.Equals($env:COMPUTERNAME, [System.StringComparison]::OrdinalIgnoreCase)
            )
        } catch {}
    }
    $name = if ($match) { "authoritative_vps" } elseif ($server) { "server_host" } else { "local_workstation" }
    [PSCustomObject]@{
        name = $name
        strict_vps_checks = ($match -or $server)
        marker_path = $marker
    }
}

function Get-ScopedResultCompact($o) {
    if ($o) { return $true }
    if ($script:strictVpsChecks) { return $false }
    return $null
}

function Get-BotFleetProbeCompact {
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

$hostProfile = Get-HostProfileCompact
$strictVpsChecks = [bool]$hostProfile.strict_vps_checks

# 0. HOST PROFILE
Write-Host "`n=== HOST PROFILE ===" -ForegroundColor Cyan
S "Host profile: $($hostProfile.name) on $env:COMPUTERNAME" $true
if (-not $strictVpsChecks) {
    S "VPS-targeted expectations are advisory on this host" $null
}

# 1. PROCESSES
Write-Host "`n=== PROCESSES ===" -ForegroundColor Cyan
$py = @(Get-Process python* -ErrorAction $e).Count
S "Python: $py" (Get-ScopedResultCompact ($py -gt 2))
$jv = @(Get-Process java* -ErrorAction $e).Count
S "Java(IBKR): $jv" (Get-ScopedResultCompact ($jv -gt 0))
$cf = @(Get-Process cloudflared* -ErrorAction $e).Count
S "Cloudflared: $cf" (Get-ScopedResultCompact ($cf -gt 0))
$cd = @(Get-Process caddy* -ErrorAction $e).Count
S "Caddy: $cd" (Get-ScopedResultCompact ($cd -gt 0))

# 2. TASKS
Write-Host "`n=== SCHEDULED TASKS ===" -ForegroundColor Cyan
$ts = @(
    "ETA-Dashboard","ETA-Jarvis-Live","ETA-Avengers-Fleet","ETA-Dashboard-Live",
    "ETA-Executor-DashboardAssemble","ETA-Steward-ShadowTick","ETA-Steward-HealthWatchdog",
    "ETA-Reasoner-TwinVerdict","ETA-Hermes-Jarvis-Flush","ApexIbkrGatewayWatchdog",
    "ETA-BTC-Fleet","ETA-MNQ-Supervisor","ETA-HealthCheck"
)
$r = 0; $st = 0; $m = @()
foreach ($t in $ts) {
    $i = schtasks /query /tn $t /fo csv 2>$null | ConvertFrom-Csv -ErrorAction $e
    if (-not $i) { $m += $t }
    elseif ($i.Status -eq "Ready") { $r++; $st++ }
    elseif ($i.Status -eq "Running") { $r++ }
    else { $st++ }
}
S "Tasks: $r running/$st ready/$($m.Count) missing" (Get-ScopedResultCompact ($m.Count -eq 0))
if ($m) {
    $missingColor = if ($strictVpsChecks) { "Red" } else { "Yellow" }
    $m | ForEach-Object { Write-Host "         MISSING: $_" -ForegroundColor $missingColor }
}
$taskAuditScript = "C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\audit_vps_scheduled_tasks.ps1"
if (Test-Path $taskAuditScript) {
    try {
        $taskAuditRaw = & powershell -NoProfile -ExecutionPolicy Bypass -File $taskAuditScript -Json 2>$null
        $taskAudit = (($taskAuditRaw -join "`n") | ConvertFrom-Json -ErrorAction Stop)
        $healthDrift = @($taskAudit.needs_attention | Where-Object { $_.task_name -eq "ETA-HealthCheck" }) | Select-Object -First 1
        if ($healthDrift) { S "ETA-HealthCheck contract drift: $($healthDrift.healthcheck_contract_issue)" $false }
        else { S "ETA-HealthCheck contract drift" $true }
        $schedulerAttentionTasks = @($taskAudit.scheduler_attention_task_names | Where-Object { $_ })
        $schedulerAttentionRepair = [string]$taskAudit.scheduler_attention_repair_command
        if ($schedulerAttentionRepair) {
            $schedulerAttentionLabel = if ($schedulerAttentionTasks.Count -gt 0) { " ($($schedulerAttentionTasks -join ", "))" } else { "" }
            Write-Host "  [WARN] ETA scheduler attention repair${schedulerAttentionLabel}: $schedulerAttentionRepair" -ForegroundColor Yellow
            $global:w++
        }
    } catch {
        S "ETA-HealthCheck contract audit unavailable" $null
    }
} else {
    S "ETA-HealthCheck audit script missing" $null
}

# 3. SERVICES
Write-Host "`n=== SERVICES ===" -ForegroundColor Cyan
$sv = @("FirmCore","FirmWatchdog","FirmCommandCenter","FirmCommandCenterTunnel","HermesJarvisTelegram")
$firmCommandCenterRepair = ".\eta_engine\deploy\scripts\repair_firm_command_center_env_admin.cmd"
foreach ($s in $sv) {
    $x = Get-Service $s -ErrorAction $e
    S "$s ($($x.Status))" (Get-ScopedResultCompact ($x -and $x.Status -eq "Running"))
}
$fccServiceXmlPath = "C:\EvolutionaryTradingAlgo\firm_command_center\services\FirmCommandCenter.xml"
if (Test-Path $fccServiceXmlPath) {
    try {
        [xml]$fccServiceXml = Get-Content $fccServiceXmlPath -Raw
        $fccPython = [string]$fccServiceXml.service.executable
        if ($fccPython) {
            S "FirmCommandCenter runtime Python: $fccPython" (Test-Path $fccPython)
        }
        $fccArguments = [string]$fccServiceXml.service.arguments
        if ($fccArguments) {
            S "FirmCommandCenter entrypoint: $fccArguments" $true
        }
    } catch {
        S "FirmCommandCenter service XML unreadable" $null
    }
}
$fccErrLog = "C:\EvolutionaryTradingAlgo\firm_command_center\var\logs\FirmCommandCenter.err.log"
$watchdogStatusPath = "C:\EvolutionaryTradingAlgo\var\ops\command_center_watchdog_status_latest.json"
if (Test-Path $fccErrLog) {
    $fccBootError = Get-Content $fccErrLog -Tail 40 -ErrorAction $e |
        Where-Object { $_ -match "ModuleNotFoundError|ImportError" } |
        Select-Object -Last 1
    if ($fccBootError) {
        S "FirmCommandCenter bootstrap error: $fccBootError" $null
    }
    $fccMissingModule = Get-Content $fccErrLog -Tail 40 -ErrorAction $e |
        Select-String -Pattern "No module named '([^']+)'" |
        Select-Object -Last 1
    if ($fccMissingModule) {
        $missingModule = $fccMissingModule.Matches[0].Groups[1].Value
        S "FirmCommandCenter dependency gap: missing module $missingModule" $null
        S "FirmCommandCenter env repair: $firmCommandCenterRepair" $null
    }
}
if (Test-Path $watchdogStatusPath) {
    try {
        $watchdogStatus = Get-Content $watchdogStatusPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $watchdogTaskContract = $watchdogStatus.watchdog_task_contract_status
        if ($watchdogTaskContract -and [string]$watchdogTaskContract.status -and [string]$watchdogTaskContract.status -ne "healthy") {
            $watchdogTaskSummary = if ($watchdogTaskContract.summary) { [string]$watchdogTaskContract.summary } else { [string]$watchdogTaskContract.status }
            S "Eta-CommandCenter-Doctor task contract: $watchdogTaskSummary" $null
        }
        $dashboardTaskContract = $watchdogStatus.dashboard_task_contract_status
        if ($dashboardTaskContract -and [string]$dashboardTaskContract.status -and [string]$dashboardTaskContract.status -ne "healthy") {
            $dashboardTaskSummary = if ($dashboardTaskContract.summary) { [string]$dashboardTaskContract.summary } else { [string]$dashboardTaskContract.status }
            S "ETA dashboard task contract: $dashboardTaskSummary" $null
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
            S "Command Center watchdog issue: $operatorIssueSummary" $null
        }
        $operatorNextStep = if ($watchdogStatus.operator_next_step) { [string]$watchdogStatus.operator_next_step } else { "" }
        if (-not [string]::IsNullOrWhiteSpace($operatorNextStep) -and $operatorNextStep -ne "none") {
            $operatorNextReason = if ($watchdogStatus.operator_next_reason) { [string]$watchdogStatus.operator_next_reason } else { "" }
            $operatorNextSummary = if (-not [string]::IsNullOrWhiteSpace($operatorNextReason)) {
                "$operatorNextReason -> $operatorNextStep"
            } else {
                $operatorNextStep
            }
            S "Command Center operator next step: $operatorNextSummary" $null
            if ($watchdogStatus.operator_next_command) {
                S "Command Center operator command: $([string]$watchdogStatus.operator_next_command)" $null
            }
        }
        $localContract = $watchdogStatus.local_contract_status
        if ($localContract -and [string]$localContract.status -and [string]$localContract.status -ne "healthy") {
            $localContractSummary = [string]$localContract.summary
            if (-not [string]::IsNullOrWhiteSpace($localContractSummary)) {
                S "Local 8421 contract symptom: $localContractSummary" $null
            } else {
                S "Local 8421 contract status: $($localContract.status)" $null
            }
            if ([string]$localContract.status -eq "upstream_failure" -and $localContract.probes) {
                $probeCodes = @(
                    "openapi=$($localContract.probes.openapi.status_code)",
                    "diagnostics=$($localContract.probes.diagnostics.status_code)",
                    "card_health=$($localContract.probes.card_health.status_code)"
                ) -join " "
                S "Local 8421 upstream probe HTTP codes: $probeCodes" $null
            }
        }
        $repairPending = [bool]$watchdogStatus.operator_repair_prompt_pending
        if ($repairPending) {
            $pendingCommand = if ($watchdogStatus.operator_repair_pending_command) {
                [string]$watchdogStatus.operator_repair_pending_command
            } else {
                $firmCommandCenterRepair
            }
            S "FirmCommandCenter env repair pending UAC approval: $pendingCommand" $null
        }
    } catch {
        S "Command Center watchdog status unreadable" $null
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
            S "ETA readiness public fallback: $fallbackReason" $null
        }
        if ($readinessStatus) {
            S "ETA readiness status: $readinessStatus" $null
        }
        if ($readinessPrimaryBlocker) {
            S "ETA readiness primary blocker: $readinessPrimaryBlocker" $null
        }
        if ($readinessDetail) {
            S "ETA readiness detail: $readinessDetail" $null
        }
        if ($readinessPrimaryAction) {
            S "ETA readiness primary action: $readinessPrimaryAction" $null
        }
        if ($bracketsSummary) {
            S "ETA readiness brackets: $bracketsSummary" $null
        }
        if ($bracketsNextAction) {
            S "ETA readiness brackets next: $bracketsNextAction" $null
        }
        if ($readinessCheckedAtUtc) {
            try {
                $readinessCheckedAtStamp = [datetimeoffset]::Parse($readinessCheckedAtUtc)
                $readinessAgeS = [int][Math]::Max(0, ((Get-Date).ToUniversalTime() - $readinessCheckedAtStamp.UtcDateTime).TotalSeconds)
                $readinessFreshness = if ($readinessAgeS -le 300) { "fresh" } else { "stale" }
                S "ETA readiness receipt freshness: $readinessFreshness (${readinessAgeS}s old)" $null
                if ($readinessAgeS -gt 300) {
                    S "ETA readiness refresh command: .\scripts\eta-readiness-snapshot.ps1" $null
                }
            } catch {
            }
        }
        if ($publicLiveRetuneGeneratedAtUtc) {
            S "ETA readiness public retune generated: $publicLiveRetuneGeneratedAtUtc" $null
        }
        if ($publicLiveRetuneSyncDriftDisplay) {
            S "ETA readiness public retune sync drift: $publicLiveRetuneSyncDriftDisplay" $null
        }
        if ($currentPublicRetuneGeneratedAtUtc -and $currentPublicRetuneGeneratedAtUtc -ne $publicLiveRetuneGeneratedAtUtc) {
            S "ETA readiness current public retune generated: $currentPublicRetuneGeneratedAtUtc" $null
        }
        if ($currentPublicRetuneOutcomeLine -and (
            $currentPublicRetuneGeneratedAtUtc -ne $publicLiveRetuneGeneratedAtUtc -or
            $currentPublicRetuneSyncDriftDisplay
        )) {
            S "ETA readiness current public retune outcome: $currentPublicRetuneOutcomeLine" $null
        }
        if ($currentPublicRetuneSyncDriftDisplay) {
            S "ETA readiness current public retune sync drift: $currentPublicRetuneSyncDriftDisplay" $null
        }
        if ($cachedLocalRetuneGeneratedAtUtc) {
            S "ETA readiness cached local retune generated: $cachedLocalRetuneGeneratedAtUtc" $null
        }
        if ($fallbackBrokerOpenOrderCount -gt 0) {
            S "ETA readiness broker open orders: $fallbackBrokerOpenOrderCount" $null
        }
        if ($fallbackLiveBrokerOpenOrderCount -gt 0) {
            S "ETA readiness live broker_state open orders: $fallbackLiveBrokerOpenOrderCount" $null
        }
        if ($fallbackStaleDisplay) {
            S "ETA readiness stale broker orders: $fallbackStaleDisplay" $null
        } elseif ($fallbackStaleCount -gt 0) {
            $symbolsLabel = if ($fallbackStaleSymbols.Count -gt 0) {
                " ($($fallbackStaleSymbols -join ", "))"
            } else {
                ""
            }
            S "ETA readiness stale broker orders: $fallbackStaleCount$symbolsLabel" $null
        }
        if ($fallbackStaleRelationDisplay) {
            S "ETA readiness stale-order pressure: $fallbackStaleRelationDisplay" $null
        }
        if ($publicLiveBrokerDegradedDisplay) {
            S "ETA readiness public broker_state degraded: $publicLiveBrokerDegradedDisplay" $null
        }
        if (
            $currentPublicLiveBrokerDegradedDisplay -and
            $currentPublicLiveBrokerDegradedDisplay -ne $publicLiveBrokerDegradedDisplay
        ) {
            S "ETA readiness current live broker_state degraded: $currentPublicLiveBrokerDegradedDisplay" $null
        }
        if ($fallbackBrokerOrderDriftDisplay) {
            S "ETA readiness broker-order drift: $fallbackBrokerOrderDriftDisplay" $null
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
            S "ETA readiness dashboard API runtime drift: $dashboardApiRuntimeDriftDisplay" $null
        }
        if ($dashboardApiRuntimeRetuneDriftDisplay) {
            S "ETA readiness dashboard API runtime retune drift: $dashboardApiRuntimeRetuneDriftDisplay" $null
        }
        if ($dashboardApiRuntimeProbeDisplay) {
            S "ETA readiness dashboard API runtime probe: $dashboardApiRuntimeProbeDisplay" $null
        }
        if (($dashboardApiRuntimeDriftDisplay -or $dashboardApiRuntimeRetuneDriftDisplay -or $dashboardApiRuntimeProbeDisplay) -and $dashboardApiRuntimeRefreshCommand) {
            S "ETA readiness dashboard API runtime refresh: $dashboardApiRuntimeRefreshCommand" $null
            if ($dashboardApiRuntimeRefreshRequiresElevation) {
                S "ETA readiness dashboard API runtime refresh requires elevation: true" $null
            }
        } elseif ($currentLiveBrokerOpenOrderDriftDisplay) {
            S "ETA readiness dashboard API runtime drift: $currentLiveBrokerOpenOrderDriftDisplay" $null
        } elseif ($fallbackLiveBrokerOpenOrderCount -gt 0 -and $currentLiveBrokerOpenOrderCount -le 0) {
            S (
                "ETA readiness dashboard API runtime drift: 8421 master/status is still blank for " +
                "current_live_broker_open_order_count while readiness receipt has $fallbackLiveBrokerOpenOrderCount"
            ) $null
        } elseif (
            $fallbackLiveBrokerOpenOrderCount -gt 0 -and
            $currentLiveBrokerOpenOrderCount -gt 0 -and
            $currentLiveBrokerOpenOrderCount -ne $fallbackLiveBrokerOpenOrderCount
        ) {
            S (
                "ETA readiness dashboard API runtime drift: 8421 master/status reports " +
                "$currentLiveBrokerOpenOrderCount current live broker open orders while readiness receipt has " +
                "$fallbackLiveBrokerOpenOrderCount"
            ) $null
        }
        if ($retuneDriftDisplay) {
            S ("ETA readiness retune mirror drift: {0}" -f $retuneDriftDisplay) $null
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
            S "ETA readiness current local retune generated: $currentLocalRetuneGeneratedAtUtc" $null
        }
        if ($localRetuneSyncDriftDisplay) {
            S ("ETA readiness local retune sync drift: {0}" -f $localRetuneSyncDriftDisplay) $null
        }
        if ($fallbackAction) {
            S "ETA readiness fallback action: $fallbackAction" $null
        }
    } catch {
        S "ETA readiness snapshot unreadable" $null
    }
}

# 4. PORTS
Write-Host "`n=== PORTS ===" -ForegroundColor Cyan
$ports = @{4002="IBKR TWS API";8000="Dashboard API";8421="Dashboard proxy";8422="FM status"}
foreach ($port in $ports.Keys) {
    $n = netstat -ano 2>$null | Select-String ":$port .*LISTENING"
    S "Port $port ($($ports[$port]))" (Get-ScopedResultCompact ($n -ne $null))
}

# 5. IBKR
Write-Host "`n=== IBKR GATEWAY ===" -ForegroundColor Cyan
$ibkr = netstat -ano 2>$null | Select-String ":4002 .*LISTENING"
S "IBKR TWS API (port 4002)" (Get-ScopedResultCompact ($ibkr -ne $null))

# 6. DEEPSEEK
Write-Host "`n=== DEEPSEEK KEY ===" -ForegroundColor Cyan
$envPath = "C:\EvolutionaryTradingAlgo\eta_engine\.env"
if (Test-Path $envPath) {
    $k = Select-String -Path $envPath -Pattern "DEEPSEEK_API_KEY=(\S{10})" | ForEach-Object { $_.Matches.Groups[1].Value }
    S "DeepSeek key: $k..." ($k -ne $null)
} else {
    S ".env not found" $false
}

# 7. DASHBOARD API
Write-Host "`n=== DASHBOARD API ===" -ForegroundColor Cyan
try {
    $botFleetProbe = Get-BotFleetProbeCompact
    if (-not $botFleetProbe.ok) {
        throw $botFleetProbe.direct_error
    }
    $a = $botFleetProbe.payload
    $c = ($a | Get-Member -MemberType NoteProperty).Count
    $err = 0
    foreach ($pr in $a.PSObject.Properties) {
        if ($pr.Value.status -eq "error") { $err++ }
    }
    $probeLabel = if ($botFleetProbe.source -eq "proxy_8421") {
        "Bot fleet: $c bots, $err errors (via proxy 8421 after direct 8000 miss)"
    } else {
        "Bot fleet: $c bots, $err errors"
    }
    S $probeLabel (Get-ScopedResultCompact ($err -eq 0))
    if ($botFleetProbe.source -eq "proxy_8421" -and $botFleetProbe.direct_error) {
        S "Dashboard direct API probe missed; proxy recovered: $($botFleetProbe.direct_error)" $null
    }
} catch {
    S "Dashboard API unreachable (direct 8000 and proxy 8421)" (Get-ScopedResultCompact $false)
}

# 8. HEARTBEAT
Write-Host "`n=== HEARTBEAT ===" -ForegroundColor Cyan
$hp = "C:\EvolutionaryTradingAlgo\eta_engine\data\runtime_supervisor_health.json"
if (Test-Path $hp) {
    $hb = Get-Content $hp -Raw | ConvertFrom-Json
    $age = [math]::Round(((Get-Date) - [datetime]$hb.last_heartbeat).TotalMinutes, 1)
    S "Last beat: ${age}m ago" (Get-ScopedResultCompact ($age -lt 30))
} else {
    S "Health file missing" (Get-ScopedResultCompact $false)
}

# 9. JARVIS MODE
Write-Host "`n=== JARVIS MODE ===" -ForegroundColor Cyan
$m = Select-String -Path $envPath -Pattern "ETA_MODE=(.+)" 2>$null | ForEach-Object { $_.Matches.Groups[1].Value }
if ($m) { S "Mode: $m" $true } else { S "ETA_MODE not set" $false }

# 10. DISK
Write-Host "`n=== DISK ===" -ForegroundColor Cyan
$d = Get-PSDrive C
$free = [math]::Round($d.Free / 1GB, 1)
S "Free: ${free}GB" ($free -gt 5)

# 11. LOG WATCH
Write-Host "`n=== RECENT LOGS ===" -ForegroundColor Cyan
$logDirs = @(
    "C:\EvolutionaryTradingAlgo\eta_engine\var\logs",
    "C:\EvolutionaryTradingAlgo\firm_command_center\var\logs"
)
$shownLogs = 0
foreach ($logDir in $logDirs) {
    foreach ($l in (Get-ChildItem $logDir -Filter "*.log" -ErrorAction $e | Sort-Object LastWriteTime -Descending | Select-Object -First 2)) {
        $shownLogs++
        $age = [int]((Get-Date) - $l.LastWriteTime).TotalMinutes
        Write-Host "  $($l.Name) (${age}m)" -ForegroundColor $(if ($age -lt 60) { "Green" } else { "Yellow" })
    }
}
if ($shownLogs -eq 0) { S "No recent logs found under canonical log roots" (Get-ScopedResultCompact $false) }

# SUMMARY
Write-Host "`n=== SUMMARY: PASS=$p WARN=$w FAIL=$f ===" -ForegroundColor Cyan
if (-not $strictVpsChecks) {
    Write-Host "VERDICT: LOCAL_WORKSTATION (VPS expectations advisory)" -ForegroundColor Yellow
} elseif ($f -eq 0 -and $w -le 2) {
    Write-Host "VERDICT: HEALTHY" -ForegroundColor Green
} elseif ($f -eq 0) {
    Write-Host "VERDICT: DEGRADED" -ForegroundColor Yellow
} else {
    Write-Host "VERDICT: UNHEALTHY" -ForegroundColor Red
}
