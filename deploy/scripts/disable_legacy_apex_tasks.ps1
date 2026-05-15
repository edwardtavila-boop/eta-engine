param(
    [string]$WorkspaceRoot = "C:\EvolutionaryTradingAlgo",
    [string]$BackupRoot = "",
    [switch]$Apply
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

$WorkspaceRoot = Assert-CanonicalEtaPath -Path $WorkspaceRoot

if (-not $BackupRoot) {
    $BackupRoot = Join-Path $WorkspaceRoot "var\eta_engine\state\scheduled_task_backups"
}
$BackupRoot = Assert-CanonicalEtaPath -Path $BackupRoot

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$backupDir = Join-Path $BackupRoot "legacy_apex_tasks_$stamp"

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

function Get-TaskActionText {
    param([Parameter(Mandatory = $true)][object]$Task)
    return (($Task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " || ")
}

function Get-TaskBackupFileName {
    param([Parameter(Mandatory = $true)][object]$Task)
    $raw = "$($Task.TaskPath.Trim('\'))__$($Task.TaskName)"
    if ($raw -eq "__$($Task.TaskName)") {
        $raw = $Task.TaskName
    }
    return (($raw -replace '[\\/:*?"<>|]', '_') + ".xml")
}

$candidates = Get-ScheduledTask | Where-Object {
    $_.TaskName -like "Apex-*" -or
    (Get-TaskActionText -Task $_) -like "*C:\eta_engine*" -or
    (Get-TaskActionText -Task $_) -like "*AppData\Local\eta_engine*" -or
    (Get-TaskActionText -Task $_) -like "*C:\apex_predator*"
} | Sort-Object TaskPath, TaskName

$results = @()
if ($Apply -and @($candidates).Count -gt 0) {
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
}

foreach ($task in $candidates) {
    $actions = Get-TaskActionText -Task $task
    $stateBefore = Convert-TaskState $task.State
    $backupPath = $null
    $status = "would_disable"
    if ($Apply) {
        $backupPath = Join-Path $backupDir (Get-TaskBackupFileName -Task $task)
        Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Set-Content -Path $backupPath -Encoding UTF8
        if ($stateBefore -ne "Disabled") {
            Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Out-Null
            $status = "disabled"
        } else {
            $status = "already_disabled"
        }
    }
    $results += [pscustomobject]@{
        task_path = $task.TaskPath
        task_name = $task.TaskName
        state_before = $stateBefore
        status = $status
        backup_path = $backupPath
        actions = $actions
    }
}

[pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    applied = [bool]$Apply
    backup_dir = $(if ($Apply) { $backupDir } else { $null })
    candidate_count = @($candidates).Count
    results = $results
} | ConvertTo-Json -Depth 6
