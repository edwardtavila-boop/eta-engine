param(
    [string]$WorkspaceRoot = "C:\EvolutionaryTradingAlgo",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

function Convert-TaskState {
    param([object]$State)
    switch ([int]$State) {
        0 { "Unknown" }
        1 { "Disabled" }
        2 { "Queued" }
        3 { "Ready" }
        4 { "Running" }
        default { "State$State" }
    }
}

function Convert-TaskResult {
    param([object]$Result)
    $n = [int64]$Result
    switch ($n) {
        0 { "success" }
        267009 { "running" }
        267011 { "never_or_no_more_runs" }
        2147942402 { "file_not_found" }
        2147946720 { "operator_refused_request" }
        1 { "generic_failure" }
        2 { "incorrect_function_or_file_not_found" }
        default { "nonzero_$n" }
    }
}

function Get-TaskActionText {
    param([Parameter(Mandatory = $true)][object]$Task)
    return (($Task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " || ")
}

$expectedHealthCheckTokens = @(
    "$WorkspaceRoot\eta_engine\scripts\health_check.py",
    "--allow-remote-supervisor-truth",
    "--allow-remote-retune-truth",
    "$WorkspaceRoot\firm_command_center\var\health"
)

$unsafeDuplicateSupervisorTasks = @(
    # Legacy wrapper observed on the VPS running a second
    # jarvis_strategy_supervisor.py instance and mutating safety state.
    "ETA-PaperLive-Supervisor"
)

$verdictExitTasks = @{
    "ETA-Diamond-FirstLightCheck" = @{
        "1" = "verdict_hold"
        "2" = "verdict_no_go"
    }
    "ETA-Diamond-LaunchReadinessEvery15Min" = @{
        "1" = "verdict_hold"
        "2" = "verdict_no_go"
    }
    "ETA-Diamond-OpsDashboardHourly" = @{
        "2" = "verdict_p0_critical"
    }
    "ETA-WeeklySharpe" = @{
        "1" = "verdict_amber"
        "2" = "verdict_red"
    }
}

$expectedCriticalTasks = @(
    [pscustomobject]@{
        task_name = "ETA-Public-Edge-Route-Watchdog"
        recommended_repair_command = ".\eta_engine\deploy\scripts\repair_eta_public_edge_route_watchdog_admin.cmd"
    }
    [pscustomobject]@{
        task_name = "ETA-WeeklySharpe"
        recommended_repair_command = ".\eta_engine\deploy\scripts\repair_eta_weekly_sharpe_admin.cmd"
    }
)

$rows = Get-ScheduledTask | Where-Object {
    $_.TaskName -like "ETA-*" -or
    $_.TaskName -like "Eta-*" -or
    $_.TaskName -like "Apex-*" -or
    $_.TaskName -like "*Hermes*" -or
    $_.TaskName -like "*Jarvis*"
} | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    $actions = Get-TaskActionText -Task $_
    $resultClass = Convert-TaskResult $info.LastTaskResult
    $stateName = Convert-TaskState $_.State
    $principalUserId = [string]$_.Principal.UserId
    $principalLogonType = [string]$_.Principal.LogonType
    $principalRunLevel = [string]$_.Principal.RunLevel
    $usesLegacy = (
        $actions -like "*C:\eta_engine*" -or
        $actions -like "*C:\apex_predator*" -or
        $actions -like "*AppData\Local\eta_engine*" -or
        $actions -like "*OneDrive*"
    )
    $usesEtaModule = $actions -match '(^|\s)-m\s+eta_engine(\.|$|\s)'
    $usesCanonical = ($actions -like "*$WorkspaceRoot*") -or ($usesEtaModule -and -not $usesLegacy)
    $canonicalBasis = if ($actions -like "*$WorkspaceRoot*") {
        "workspace_path"
    } elseif ($usesEtaModule -and -not $usesLegacy) {
        "eta_module_invocation"
    } else {
        ""
    }
    $expectedVerdictClass = ""
    $allowsNonzeroVerdict = $false
    if ($verdictExitTasks.ContainsKey($_.TaskName)) {
        $taskVerdictMap = $verdictExitTasks[$_.TaskName]
        $resultCodeKey = [string]([int64]$info.LastTaskResult)
        if ($taskVerdictMap.ContainsKey($resultCodeKey)) {
            $expectedVerdictClass = [string]$taskVerdictMap[$resultCodeKey]
            $allowsNonzeroVerdict = $true
        }
    }
    $isCurrentUserInteractivePrincipal = (
        -not [string]::IsNullOrWhiteSpace($principalUserId) -and
        $principalUserId -notin @("SYSTEM", "NT AUTHORITY\\SYSTEM") -and
        $principalLogonType -in @("3", "Interactive", "InteractiveToken")
    )
    $displayResultClass = if ($expectedVerdictClass) {
        $expectedVerdictClass
    } elseif ($_.TaskName -eq "ETA-Public-Edge-Route-Watchdog" -and $isCurrentUserInteractivePrincipal) {
        "interactive_principal_service_control_risk"
    } elseif ($resultClass -eq "operator_refused_request" -and $isCurrentUserInteractivePrincipal) {
        "operator_refused_request_interactive_principal"
    } else {
        $resultClass
    }
    $recommendedRepairCommand = ""
    if ($_.TaskName -eq "ETA-WeeklySharpe" -and $displayResultClass -eq "operator_refused_request_interactive_principal") {
        $recommendedRepairCommand = ".\eta_engine\deploy\scripts\repair_eta_weekly_sharpe_admin.cmd"
    }
    if ($_.TaskName -eq "ETA-Public-Edge-Route-Watchdog" -and $isCurrentUserInteractivePrincipal) {
        $recommendedRepairCommand = ".\eta_engine\deploy\scripts\repair_eta_public_edge_route_watchdog_admin.cmd"
    }
    $healthCheckContractDrift = $false
    $healthCheckContractIssue = ""
    if ($_.TaskName -eq "ETA-HealthCheck") {
        $missingHealthCheckTokens = @(
            $expectedHealthCheckTokens | Where-Object { $actions -notlike "*$_*" }
        )
        if ($missingHealthCheckTokens.Count -gt 0) {
            $healthCheckContractDrift = $true
            $healthCheckContractIssue = "missing_contract_tokens:" + ($missingHealthCheckTokens -join ",")
        }
    }
    $isUnsafeDuplicateSupervisor = (
        $_.TaskName -in $unsafeDuplicateSupervisorTasks -and
        $stateName -ne "Disabled"
    )
    $needsAttention = (
        $usesLegacy -or
        $healthCheckContractDrift -or
        $isUnsafeDuplicateSupervisor -or
        ($_.TaskName -eq "ETA-Public-Edge-Route-Watchdog" -and $isCurrentUserInteractivePrincipal) -or
        ($stateName -ne "Disabled" -and -not $allowsNonzeroVerdict -and $resultClass -notin @("success", "running", "never_or_no_more_runs")) -or
        ($stateName -ne "Disabled" -and -not $usesCanonical -and $_.TaskName -like "ETA-*")
    )
    [pscustomobject]@{
        task_path = $_.TaskPath
        task_name = $_.TaskName
        state = $stateName
        last_task_result = [int64]$info.LastTaskResult
        result_class = $displayResultClass
        last_run_time = $info.LastRunTime
        next_run_time = $info.NextRunTime
        uses_canonical = $usesCanonical
        canonical_basis = $canonicalBasis
        uses_legacy_path = $usesLegacy
        principal_user_id = $principalUserId
        principal_logon_type = $principalLogonType
        principal_run_level = $principalRunLevel
        is_current_user_interactive_principal = $isCurrentUserInteractivePrincipal
        recommended_repair_command = $recommendedRepairCommand
        allows_nonzero_verdict = $allowsNonzeroVerdict
        is_healthcheck_contract_drift = $healthCheckContractDrift
        healthcheck_contract_issue = $healthCheckContractIssue
        is_unsafe_duplicate_supervisor = $isUnsafeDuplicateSupervisor
        unsafe_duplicate_supervisor = if ($isUnsafeDuplicateSupervisor) { "disable_duplicate_and_keep_ETA-Jarvis-Strategy-Supervisor" } else { "" }
        needs_attention = $needsAttention
        actions = $actions
    }
}

$existingTaskNames = @($rows | ForEach-Object { [string]$_.task_name })
foreach ($expectedTask in $expectedCriticalTasks) {
    if ($existingTaskNames -contains $expectedTask.task_name) {
        continue
    }
    $rows += [pscustomobject]@{
        task_path = "\"
        task_name = [string]$expectedTask.task_name
        state = "Missing"
        last_task_result = $null
        result_class = "missing_task"
        last_run_time = $null
        next_run_time = $null
        uses_canonical = $false
        canonical_basis = ""
        uses_legacy_path = $false
        principal_user_id = ""
        principal_logon_type = ""
        principal_run_level = ""
        is_current_user_interactive_principal = $false
        recommended_repair_command = [string]$expectedTask.recommended_repair_command
        allows_nonzero_verdict = $false
        is_healthcheck_contract_drift = $false
        healthcheck_contract_issue = ""
        is_unsafe_duplicate_supervisor = $false
        unsafe_duplicate_supervisor = ""
        needs_attention = $true
        actions = ""
    }
}

$problemRows = @($rows | Where-Object { $_.needs_attention } | Sort-Object task_name)
$schedulerAttentionTaskNames = @(
    $problemRows |
        Where-Object { $_.task_name -in @("ETA-Public-Edge-Route-Watchdog", "ETA-WeeklySharpe") } |
        ForEach-Object { [string]$_.task_name }
)
$schedulerAttentionRepairCommand = if ($schedulerAttentionTaskNames.Count -gt 0) {
    ".\eta_engine\deploy\scripts\repair_eta_scheduler_attention_admin.cmd"
} else {
    ""
}
$payload = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    workspace_root = $WorkspaceRoot
    task_count = @($rows).Count
    needs_attention_count = $problemRows.Count
    scheduler_attention_task_names = $schedulerAttentionTaskNames
    scheduler_attention_repair_command = $schedulerAttentionRepairCommand
    needs_attention = $problemRows
}

if ($Json) {
    $payload | ConvertTo-Json -Depth 8
} else {
    $problemRows | Format-Table task_path, task_name, state, result_class, uses_canonical, uses_legacy_path -AutoSize
}
