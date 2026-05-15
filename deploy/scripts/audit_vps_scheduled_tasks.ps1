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
        1 { "generic_failure" }
        2 { "incorrect_function_or_file_not_found" }
        default { "nonzero_$n" }
    }
}

function Get-TaskActionText {
    param([Parameter(Mandatory = $true)][object]$Task)
    return (($Task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " || ")
}

$unsafeDuplicateSupervisorTasks = @(
    # Legacy wrapper observed on the VPS running a second
    # jarvis_strategy_supervisor.py instance and mutating safety state.
    "ETA-PaperLive-Supervisor"
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
    $usesCanonical = $actions -like "*$WorkspaceRoot*"
    $usesLegacy = (
        $actions -like "*C:\eta_engine*" -or
        $actions -like "*C:\apex_predator*" -or
        $actions -like "*AppData\Local\eta_engine*" -or
        $actions -like "*OneDrive*"
    )
    $isUnsafeDuplicateSupervisor = (
        $_.TaskName -in $unsafeDuplicateSupervisorTasks -and
        $stateName -ne "Disabled"
    )
    $needsAttention = (
        $usesLegacy -or
        $isUnsafeDuplicateSupervisor -or
        ($stateName -ne "Disabled" -and $resultClass -notin @("success", "running", "never_or_no_more_runs")) -or
        ($stateName -ne "Disabled" -and -not $usesCanonical -and $_.TaskName -like "ETA-*")
    )
    [pscustomobject]@{
        task_path = $_.TaskPath
        task_name = $_.TaskName
        state = $stateName
        last_task_result = [int64]$info.LastTaskResult
        result_class = $resultClass
        last_run_time = $info.LastRunTime
        next_run_time = $info.NextRunTime
        uses_canonical = $usesCanonical
        uses_legacy_path = $usesLegacy
        is_unsafe_duplicate_supervisor = $isUnsafeDuplicateSupervisor
        unsafe_duplicate_supervisor = if ($isUnsafeDuplicateSupervisor) { "disable_duplicate_and_keep_ETA-Jarvis-Strategy-Supervisor" } else { "" }
        needs_attention = $needsAttention
        actions = $actions
    }
}

$problemRows = @($rows | Where-Object { $_.needs_attention } | Sort-Object task_name)
$payload = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    workspace_root = $WorkspaceRoot
    task_count = @($rows).Count
    needs_attention_count = $problemRows.Count
    needs_attention = $problemRows
}

if ($Json) {
    $payload | ConvertTo-Json -Depth 8
} else {
    $problemRows | Format-Table task_path, task_name, state, result_class, uses_canonical, uses_legacy_path -AutoSize
}
