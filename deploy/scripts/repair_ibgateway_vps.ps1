[CmdletBinding()]
param(
    [string]$EtaEngineDir = "C:\EvolutionaryTradingAlgo\eta_engine",
    [string]$GatewayDir = "C:\Jts\ibgateway\1046",
    [string]$LoginProfile = "apexpredatoribkr",
    [string]$Heap = "512m",
    [int]$ParallelGCThreads = 2,
    [int]$ConcGCThreads = 1,
    [int]$ApiPort = 4002,
    [switch]$ApplyVmOptions,
    [switch]$ApplyJtsIni,
    [switch]$RepairTasks,
    [switch]$RestartGateway
)

$ErrorActionPreference = "Stop"

$BackupDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\backups\ibgateway"
$StatePath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibgateway_repair.json"
$Starter = Join-Path $EtaEngineDir "deploy\scripts\start_ibgateway.ps1"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

function Backup-File {
    param([string]$Path, [string]$Stamp)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $leaf = Split-Path -Leaf $Path
    $backup = Join-Path $BackupDir "$Stamp-$leaf"
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    return $backup
}

function Backup-Task {
    param([string]$TaskName, [string]$Stamp)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return $null
    }
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $backup = Join-Path $BackupDir "$Stamp-$TaskName.xml"
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $backup -Encoding ASCII
    return $backup
}

function Set-OrAppendLine {
    param(
        [string[]]$Lines,
        [string]$Pattern,
        [string]$Replacement
    )
    $found = $false
    $updated = foreach ($line in $Lines) {
        if ($line -match $Pattern) {
            $found = $true
            $Replacement
        } else {
            $line
        }
    }
    if (-not $found) {
        $updated += $Replacement
    }
    return @($updated)
}

function Update-VmOptions {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing IB Gateway vmoptions: $Path"
    }
    $lines = @(Get-Content -LiteralPath $Path)
    $lines = Set-OrAppendLine -Lines $lines -Pattern '^-Xmx' -Replacement "-Xmx$Heap"
    $lines = Set-OrAppendLine -Lines $lines -Pattern '^-XX:ParallelGCThreads=' -Replacement "-XX:ParallelGCThreads=$ParallelGCThreads"
    $lines = Set-OrAppendLine -Lines $lines -Pattern '^-XX:ConcGCThreads=' -Replacement "-XX:ConcGCThreads=$ConcGCThreads"
    Set-Content -LiteralPath $Path -Value $lines -Encoding ASCII
}

function Update-JtsIni {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing IB Gateway jts.ini: $Path"
    }
    $lines = @(Get-Content -LiteralPath $Path)
    $lines = Set-OrAppendLine -Lines $lines -Pattern '^LocalServerPort=' -Replacement "LocalServerPort=$ApiPort"
    $lines = Set-OrAppendLine -Lines $lines -Pattern '^TrustedIPs=' -Replacement "TrustedIPs=127.0.0.1"
    $lines = Set-OrAppendLine -Lines $lines -Pattern '^ApiOnly=' -Replacement "ApiOnly=true"
    Set-Content -LiteralPath $Path -Value $lines -Encoding ASCII
}

function Set-TaskActionIfPresent {
    param(
        [string]$TaskName,
        [string]$Arguments
    )
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument $Arguments `
        -WorkingDirectory $EtaEngineDir
    if ($task) {
        try {
            Set-ScheduledTask -TaskName $TaskName -Action $action -ErrorAction Stop | Out-Null
            return "updated"
        } catch {
            return "failed: $($_.Exception.Message)"
        }
    }
    $trigger = if ($TaskName -eq "ETA-IBGateway-DailyRestart") {
        New-ScheduledTaskTrigger -Daily -At "04:00"
    } else {
        New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10)
    }
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $env:USERNAME `
        -RunLevel Limited | Out-Null
    return "created"
}

Assert-CanonicalEtaPath -Path $EtaEngineDir
Assert-CanonicalEtaPath -Path $BackupDir
Assert-CanonicalEtaPath -Path $StatePath
if (-not (Test-Path -LiteralPath $Starter)) {
    throw "Missing canonical starter: $Starter"
}

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$vmOptionsPath = Join-Path $GatewayDir "ibgateway.vmoptions"
$jtsIniPath = Join-Path $GatewayDir "jts.ini"
$result = [ordered]@{
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    gateway_dir = $GatewayDir
    login_profile = $LoginProfile
    api_port = $ApiPort
    heap = $Heap
    parallel_gc_threads = $ParallelGCThreads
    conc_gc_threads = $ConcGCThreads
    backups = @{}
    tasks = @{}
    restarted = $false
}

if ($ApplyVmOptions) {
    $result.backups.vmoptions = Backup-File -Path $vmOptionsPath -Stamp $stamp
    Update-VmOptions -Path $vmOptionsPath
}

if ($ApplyJtsIni) {
    $result.backups.jts_ini = Backup-File -Path $jtsIniPath -Stamp $stamp
    Update-JtsIni -Path $jtsIniPath
}

if ($RepairTasks) {
    $baseArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Starter`" -GatewayDir `"$GatewayDir`" -LoginProfile `"$LoginProfile`" -ApiPort $ApiPort"
    $result.backups."ETA-IBGateway" = Backup-Task -TaskName "ETA-IBGateway" -Stamp $stamp
    $result.backups."ETA-IBGateway-RunNow" = Backup-Task -TaskName "ETA-IBGateway-RunNow" -Stamp $stamp
    $result.backups."ETA-IBGateway-DailyRestart" = Backup-Task -TaskName "ETA-IBGateway-DailyRestart" -Stamp $stamp
    $result.tasks."ETA-IBGateway" = Set-TaskActionIfPresent -TaskName "ETA-IBGateway" -Arguments $baseArgs
    $result.tasks."ETA-IBGateway-RunNow" = Set-TaskActionIfPresent -TaskName "ETA-IBGateway-RunNow" -Arguments $baseArgs
    $result.tasks."ETA-IBGateway-DailyRestart" = Set-TaskActionIfPresent -TaskName "ETA-IBGateway-DailyRestart" -Arguments "$baseArgs -ForceRestart"
}

if ($RestartGateway) {
    & $Starter -GatewayDir $GatewayDir -LoginProfile $LoginProfile -ApiPort $ApiPort -ForceRestart
    $result.restarted = $true
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatePath) | Out-Null
($result | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $StatePath -Encoding ASCII
$result | ConvertTo-Json -Depth 5
