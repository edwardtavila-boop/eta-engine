[CmdletBinding()]
param(
    [string]$EtaEngineDir = "C:\EvolutionaryTradingAlgo\eta_engine",
    [string]$JtsGatewayRoot = "C:\Jts\ibgateway",
    [string]$CanonicalGatewayVersion = "1046",
    [string]$GatewayDir = "C:\Jts\ibgateway\1046",
    [string]$LoginProfile = "apexpredatoribkr",
    [string]$TaskUser = "",
    [string]$TaskPassword = "",
    [string]$Heap = "512m",
    [int]$ParallelGCThreads = 2,
    [int]$ConcGCThreads = 1,
    [int]$ApiPort = 4002,
    [switch]$ApplyVmOptions,
    [switch]$ApplyJtsIni,
    [switch]$RepairTasks,
    [switch]$EnforceSingleSource,
    [switch]$StopLegacyIbkrProcesses,
    [switch]$UseIbc,
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

function Normalize-PathString {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\").ToLowerInvariant()
}

function Get-GatewayExecutableCandidates {
    param([string]$GatewayInstallDir)

    return @(
        Join-Path $GatewayInstallDir "ibgateway.exe"
        Join-Path $GatewayInstallDir "ibgateway1.exe"
    )
}

function Test-GatewayInstallDir {
    param([string]$GatewayInstallDir)

    foreach ($candidate in (Get-GatewayExecutableCandidates -GatewayInstallDir $GatewayInstallDir)) {
        if (Test-Path -LiteralPath $candidate) {
            return $true
        }
    }

    return $false
}

function Resolve-GatewayExecutablePath {
    param([string]$GatewayInstallDir)

    foreach ($candidate in (Get-GatewayExecutableCandidates -GatewayInstallDir $GatewayInstallDir)) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "No runnable IB Gateway executable was found under $GatewayInstallDir"
}

function Resolve-GatewayDir {
    param(
        [string]$RequestedPath,
        [string]$GatewayRoot,
        [string]$RequestedVersion
    )

    if ($RequestedPath) {
        $resolvedPath = [System.IO.Path]::GetFullPath($RequestedPath)
        if (Test-GatewayInstallDir -GatewayInstallDir $resolvedPath) {
            return $resolvedPath
        }
    }

    $envGatewayDir = $env:ETA_IBGATEWAY_DIR
    if ($envGatewayDir) {
        $resolvedEnv = [System.IO.Path]::GetFullPath($envGatewayDir)
        if (Test-GatewayInstallDir -GatewayInstallDir $resolvedEnv) {
            return $resolvedEnv
        }
    }

    if ($RequestedVersion) {
        $versionPath = Join-Path $GatewayRoot $RequestedVersion
        if (Test-GatewayInstallDir -GatewayInstallDir $versionPath) {
            return [System.IO.Path]::GetFullPath($versionPath)
        }
    }

    if (-not (Test-Path -LiteralPath $GatewayRoot)) {
        throw "IB Gateway root not found: $GatewayRoot"
    }

    $candidates = @(
        Get-ChildItem -Path $GatewayRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-GatewayInstallDir -GatewayInstallDir $_.FullName } |
            Sort-Object `
                @{ Expression = {
                    $digits = [regex]::Match($_.Name, '\d+').Value
                    if ($digits) { [int]$digits } else { 0 }
                }; Descending = $true }, `
                @{ Expression = { $_.LastWriteTimeUtc }; Descending = $true }
    )

    if ($candidates.Count -eq 0) {
        throw "No installed IB Gateway directories with ibgateway.exe or ibgateway1.exe were found under $GatewayRoot"
    }

    return $candidates[0].FullName
}

function Resolve-GatewayVersion {
    param([string]$Path)
    return Split-Path -Leaf ([System.IO.Path]::GetFullPath($Path))
}

function Assert-CanonicalGatewayDir {
    param([string]$Path)
    $expected = Join-Path $JtsGatewayRoot $CanonicalGatewayVersion
    if ((Normalize-PathString -Path $Path) -ne (Normalize-PathString -Path $expected)) {
        throw "Refusing non-canonical IB Gateway path: $Path; expected $expected"
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
    try {
        Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath |
            Set-Content -LiteralPath $backup -Encoding ASCII
        return $backup
    } catch {
        return "backup_failed: $($_.Exception.Message)"
    }
}

function Get-TaskActionText {
    param([string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return ""
    }
    return ($task.Actions | ForEach-Object { ($_.Execute + " " + $_.Arguments).Trim() }) -join " || "
}

function Get-TaskStateText {
    param([string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return "Missing"
    }
    return [string]$task.State
}

function Resolve-TaskUserForTask {
    param([string]$TaskName)

    if (-not [string]::IsNullOrWhiteSpace($TaskUser)) {
        return $TaskUser.Trim()
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task -and -not [string]::IsNullOrWhiteSpace([string]$task.Principal.UserId)) {
        return [string]$task.Principal.UserId
    }

    return ""
}

function Invoke-SchtasksDisableTask {
    param(
        [string]$TaskPath,
        [string]$TaskName
    )
    $fullName = "$TaskPath$TaskName"
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = "schtasks.exe"
    $processInfo.Arguments = "/Change /TN `"$fullName`" /DISABLE"
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($processInfo)
    if (-not $process.WaitForExit(15000)) {
        $process.Kill()
        return [ordered]@{
            ok = $false
            output = "schtasks disable timed out"
        }
    }
    $output = @(
        $process.StandardOutput.ReadToEnd()
        $process.StandardError.ReadToEnd()
    ) -join "`n"
    return [ordered]@{
        ok = ($process.ExitCode -eq 0)
        output = $output
    }
}

function Disable-TaskIfPresent {
    param([string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return "missing"
    }
    try {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        return "disabled"
    } catch {
        $fallback = Invoke-SchtasksDisableTask `
            -TaskPath $task.TaskPath `
            -TaskName $task.TaskName
        if ($fallback.ok) {
            return "disabled_via_schtasks"
        }
        return "failed: $($_.Exception.Message); schtasks: $($fallback.output)"
    }
}

function Enable-TaskIfPresent {
    param([string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return "missing"
    }
    try {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        return "enabled"
    } catch {
        return "failed: $($_.Exception.Message)"
    }
}

function Get-GatewayInstallInventory {
    $expected = Normalize-PathString -Path (Join-Path $JtsGatewayRoot $CanonicalGatewayVersion)
    if (-not (Test-Path -LiteralPath $JtsGatewayRoot)) {
        return @()
    }
    return @(
        Get-ChildItem -Path $JtsGatewayRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                $exe = $null
                if (Test-GatewayInstallDir -GatewayInstallDir $_.FullName) {
                    $exe = Resolve-GatewayExecutablePath -GatewayInstallDir $_.FullName
                }
                [ordered]@{
                    version = $_.Name
                    path = $_.FullName
                    canonical = ((Normalize-PathString -Path $_.FullName) -eq $expected)
                    has_exe = ($null -ne $exe)
                    executable_name = if ($exe) { Split-Path -Leaf $exe } else { $null }
                    last_write_time = $_.LastWriteTime.ToUniversalTime().ToString("o")
                }
            }
    )
}

function Get-PortListenerSnapshot {
    return @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in @($ApiPort, 5000, 5001, 7496, 7497) } |
            Sort-Object LocalPort |
            ForEach-Object {
                [ordered]@{
                    local_address = $_.LocalAddress
                    local_port = $_.LocalPort
                    owning_process = $_.OwningProcess
                }
            }
    )
}

function Stop-LegacyIbkrProcess {
    $canonicalDir = Normalize-PathString -Path $GatewayDir
    return @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $cmd = [string]$_.CommandLine
                $lower = $cmd.ToLowerInvariant()
                $isGateway = $lower.Contains("\ibgateway\")
                $isNonCanonicalGateway = $isGateway -and (-not $lower.Contains($canonicalDir))
                $isClientPortalGateway = $lower.Contains("clientportal.gw")
                $isNonCanonicalGateway -or $isClientPortalGateway
            } |
            ForEach-Object {
                $entry = [ordered]@{
                    process_id = $_.ProcessId
                    name = $_.Name
                    reason = if ([string]$_.CommandLine -match "clientportal\.gw") { "client_portal_gateway" } else { "non_canonical_ibgateway" }
                    stopped = $false
                }
                if ($StopLegacyIbkrProcesses) {
                    try {
                        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                        $entry.stopped = $true
                    } catch {
                        $entry.error = $_.Exception.Message
                    }
                }
                $entry
            }
    )
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

function Get-KeyValueSetting {
    param(
        [string[]]$Lines,
        [string]$Key
    )
    foreach ($line in $Lines) {
        $trimmed = [string]$line
        if ($trimmed.StartsWith("$Key=", [System.StringComparison]::OrdinalIgnoreCase)) {
            return $trimmed.Substring($Key.Length + 1).Trim()
        }
    }
    return ""
}

function Get-VmOptionValue {
    param(
        [string[]]$Lines,
        [string]$Prefix
    )
    foreach ($line in $Lines) {
        $trimmed = ([string]$line).Trim()
        if ($trimmed.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $trimmed.Substring($Prefix.Length).Trim()
        }
    }
    return ""
}

function Get-JtsIniSnapshot {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [ordered]@{
            path = $Path
            exists = $false
            configured = $false
        }
    }
    $lines = @(Get-Content -LiteralPath $Path)
    $localServerPort = Get-KeyValueSetting -Lines $lines -Key "LocalServerPort"
    $trustedIps = Get-KeyValueSetting -Lines $lines -Key "TrustedIPs"
    $apiOnly = Get-KeyValueSetting -Lines $lines -Key "ApiOnly"
    $trustedLocalhost = @(($trustedIps -split ",") | Where-Object { $_.Trim() -eq "127.0.0.1" }).Count -gt 0
    $apiOnlyEnabled = @("true", "yes", "1").Contains($apiOnly.ToLowerInvariant())
    $apiPortConfigured = ($localServerPort -eq [string]$ApiPort)
    return [ordered]@{
        path = $Path
        exists = $true
        local_server_port = $localServerPort
        trusted_ips = $trustedIps
        api_only = $apiOnly
        api_port_configured = $apiPortConfigured
        trusted_localhost = $trustedLocalhost
        api_only_enabled = $apiOnlyEnabled
        configured = ($apiPortConfigured -and $trustedLocalhost -and $apiOnlyEnabled)
    }
}

function Get-VmOptionsSnapshot {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [ordered]@{
            path = $Path
            exists = $false
            configured = $false
        }
    }
    $lines = @(Get-Content -LiteralPath $Path)
    $xmx = Get-VmOptionValue -Lines $lines -Prefix "-Xmx"
    $parallelThreads = Get-VmOptionValue -Lines $lines -Prefix "-XX:ParallelGCThreads="
    $concThreads = Get-VmOptionValue -Lines $lines -Prefix "-XX:ConcGCThreads="
    $lowMemoryProfileConfigured = (
        $xmx -eq $Heap -and
        $parallelThreads -eq [string]$ParallelGCThreads -and
        $concThreads -eq [string]$ConcGCThreads
    )
    return [ordered]@{
        path = $Path
        exists = $true
        xmx = $xmx
        parallel_gc_threads = $parallelThreads
        conc_gc_threads = $concThreads
        low_memory_profile_configured = $lowMemoryProfileConfigured
        configured = $lowMemoryProfileConfigured
    }
}

function Invoke-SchtasksActionChange {
    param(
        [string]$TaskName,
        [string]$Execute,
        [string]$Arguments,
        [string]$TaskRunAsUser = "",
        [string]$TaskRunAsPassword = ""
    )
    $taskPath = "\$TaskName"
    $taskRun = "$Execute $Arguments"
    $escapedTaskPath = $taskPath.Replace('"', '\"')
    $escapedTaskRun = $taskRun.Replace('"', '\"')
    $schtasksArgs = "/Change /TN `"$escapedTaskPath`" /TR `"$escapedTaskRun`""
    if (-not [string]::IsNullOrWhiteSpace($TaskRunAsUser) -and -not [string]::IsNullOrWhiteSpace($TaskRunAsPassword)) {
        $escapedTaskUser = $TaskRunAsUser.Replace('"', '\"')
        $escapedTaskPassword = $TaskRunAsPassword.Replace('"', '\"')
        $schtasksArgs += " /RU `"$escapedTaskUser`" /RP `"$escapedTaskPassword`""
    }
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = "schtasks.exe"
    $processInfo.Arguments = $schtasksArgs
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($processInfo)
    if (-not $process.WaitForExit(15000)) {
        $process.Kill()
        return [ordered]@{
            ok = $false
            output = "schtasks timed out"
        }
    }
    $output = @(
        $process.StandardOutput.ReadToEnd()
        $process.StandardError.ReadToEnd()
    ) -join "`n"
    return [ordered]@{
        ok = ($process.ExitCode -eq 0)
        output = $output
    }
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
            $fallback = Invoke-SchtasksActionChange `
                -TaskName $TaskName `
                -Execute "powershell.exe" `
                -Arguments $Arguments `
                -TaskRunAsUser (Resolve-TaskUserForTask -TaskName $TaskName) `
                -TaskRunAsPassword $TaskPassword
            if ($fallback.ok) {
                return "updated_via_schtasks"
            }
            return "failed: $($_.Exception.Message); schtasks: $($fallback.output)"
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
$requestedGatewayDir = $GatewayDir
$requestedGatewayVersion = $CanonicalGatewayVersion
$GatewayDir = Resolve-GatewayDir -RequestedPath $GatewayDir -GatewayRoot $JtsGatewayRoot -RequestedVersion $CanonicalGatewayVersion
$CanonicalGatewayVersion = Resolve-GatewayVersion -Path $GatewayDir
Assert-CanonicalGatewayDir -Path $GatewayDir
if (-not (Test-Path -LiteralPath $Starter)) {
    throw "Missing canonical starter: $Starter"
}

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$vmOptionsPath = Join-Path $GatewayDir "ibgateway.vmoptions"
$jtsIniPath = Join-Path $GatewayDir "jts.ini"
$result = [ordered]@{
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    requested_gateway_dir = $requestedGatewayDir
    requested_gateway_version = $requestedGatewayVersion
    gateway_dir = $GatewayDir
    canonical_gateway_version = $CanonicalGatewayVersion
    login_profile = $LoginProfile
    api_port = $ApiPort
    heap = $Heap
    parallel_gc_threads = $ParallelGCThreads
    conc_gc_threads = $ConcGCThreads
    launcher_mode = if ($UseIbc) { "ibc" } else { "direct" }
    backups = @{}
    tasks = @{}
    gateway_config = [ordered]@{
        jts_ini = @{}
        vmoptions = @{}
    }
    single_source = [ordered]@{
        inventory = @()
        non_canonical_installs = @()
        legacy_tasks = @{}
        legacy_processes = @()
        task_actions = @{}
        task_states = @{}
        gateway_task_canonical = $false
        port_listeners = @()
    }
    restarted = $false
    restart_error = ""
}
$restartFailed = $false

$inventory = @(Get-GatewayInstallInventory)
$result.single_source.inventory = $inventory
$result.single_source.non_canonical_installs = @($inventory | Where-Object { -not $_.canonical })
$result.single_source.port_listeners = @(Get-PortListenerSnapshot)

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
    if ($UseIbc) {
        $baseArgs += " -UseIbc"
    }
    $result.backups."ETA-IBGateway" = Backup-Task -TaskName "ETA-IBGateway" -Stamp $stamp
    $result.backups."ETA-IBGateway-RunNow" = Backup-Task -TaskName "ETA-IBGateway-RunNow" -Stamp $stamp
    $result.backups."ETA-IBGateway-DailyRestart" = Backup-Task -TaskName "ETA-IBGateway-DailyRestart" -Stamp $stamp
    $result.tasks."ETA-IBGateway" = Set-TaskActionIfPresent -TaskName "ETA-IBGateway" -Arguments $baseArgs
    $result.tasks."ETA-IBGateway-RunNow" = Set-TaskActionIfPresent -TaskName "ETA-IBGateway-RunNow" -Arguments $baseArgs
    $result.tasks."ETA-IBGateway-DailyRestart" = Set-TaskActionIfPresent -TaskName "ETA-IBGateway-DailyRestart" -Arguments "$baseArgs -ForceRestart"
}

if ($EnforceSingleSource) {
    foreach ($legacyTaskName in @("ApexIbkrGatewayReauth", "IBGatewayInstallAtLogon", "ApexIbkrGatewayWatchdog")) {
        $result.backups.$legacyTaskName = Backup-Task -TaskName $legacyTaskName -Stamp $stamp
        $result.single_source.legacy_tasks.$legacyTaskName = Disable-TaskIfPresent -TaskName $legacyTaskName
    }
    if ($UseIbc) {
        $result.single_source.legacy_tasks."ETA-IBGateway" = Enable-TaskIfPresent -TaskName "ETA-IBGateway"
    } else {
        $etaGatewayActionBeforeDisable = [string](Get-TaskActionText -TaskName "ETA-IBGateway")
        $directGatewayExecutables = @(
            Join-Path $GatewayDir "ibgateway.exe"
            Join-Path $GatewayDir "ibgateway1.exe"
        )
        if ($directGatewayExecutables.Where({ $etaGatewayActionBeforeDisable.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase) }).Count -gt 0) {
            $result.single_source.legacy_tasks."ETA-IBGateway" = Disable-TaskIfPresent -TaskName "ETA-IBGateway"
        }
    }
    $result.single_source.legacy_processes = @(Stop-LegacyIbkrProcess)
}

foreach ($gatewayTaskName in @("ETA-IBGateway", "ETA-IBGateway-RunNow", "ETA-IBGateway-DailyRestart")) {
    $result.single_source.task_actions.$gatewayTaskName = Get-TaskActionText -TaskName $gatewayTaskName
    $result.single_source.task_states.$gatewayTaskName = Get-TaskStateText -TaskName $gatewayTaskName
}
$etaGatewayAction = [string]$result.single_source.task_actions."ETA-IBGateway"
$runNowAction = [string]$result.single_source.task_actions."ETA-IBGateway-RunNow"
$dailyRestartAction = [string]$result.single_source.task_actions."ETA-IBGateway-DailyRestart"
$etaGatewayState = [string]$result.single_source.task_states."ETA-IBGateway"
$ibcFlagExpected = if ($UseIbc) { $true } else { $false }
$runNowIsCanonical = (
    $runNowAction.Contains("start_ibgateway.ps1") -and
    $runNowAction.Contains($GatewayDir) -and
    ($runNowAction.Contains("-UseIbc") -eq $ibcFlagExpected)
)
$dailyRestartIsCanonical = (
    $dailyRestartAction.Contains("start_ibgateway.ps1") -and
    $dailyRestartAction.Contains($GatewayDir) -and
    ($dailyRestartAction.Contains("-UseIbc") -eq $ibcFlagExpected)
)
$etaGatewayIsCanonical = (
    $etaGatewayAction.Contains("start_ibgateway.ps1") -and
    $etaGatewayAction.Contains($GatewayDir) -and
    ($etaGatewayAction.Contains("-UseIbc") -eq $ibcFlagExpected) -and
    (-not (
        $etaGatewayAction.StartsWith((Join-Path $GatewayDir "ibgateway.exe"), [System.StringComparison]::OrdinalIgnoreCase) -or
        $etaGatewayAction.StartsWith((Join-Path $GatewayDir "ibgateway1.exe"), [System.StringComparison]::OrdinalIgnoreCase)
    ))
)
$etaGatewayIsCanonicalOrDisabled = (
    $etaGatewayState -eq "Disabled" -or
    $etaGatewayIsCanonical
)
if ($UseIbc) {
    $result.single_source.gateway_task_canonical = (
        $etaGatewayState -ne "Disabled" -and
        $etaGatewayIsCanonical -and
        $runNowIsCanonical -and
        $dailyRestartIsCanonical
    )
} else {
    $result.single_source.gateway_task_canonical = (
        $etaGatewayIsCanonicalOrDisabled -and
        $runNowIsCanonical -and
        $dailyRestartIsCanonical
    )
}
$result.gateway_config.jts_ini = Get-JtsIniSnapshot -Path $jtsIniPath
$result.gateway_config.vmoptions = Get-VmOptionsSnapshot -Path $vmOptionsPath
$result.single_source.port_listeners = @(Get-PortListenerSnapshot)

if ($RestartGateway) {
    try {
        & $Starter -GatewayDir $GatewayDir -LoginProfile $LoginProfile -ApiPort $ApiPort -UseIbc:$UseIbc -ForceRestart
        $result.restarted = $true
    } catch {
        $result.restart_error = $_.Exception.Message
        $restartFailed = $true
    }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatePath) | Out-Null
($result | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $StatePath -Encoding ASCII
$result | ConvertTo-Json -Depth 5
if ($restartFailed) {
    throw $result.restart_error
}
