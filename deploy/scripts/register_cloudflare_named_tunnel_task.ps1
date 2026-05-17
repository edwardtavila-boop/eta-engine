[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "ETA-Cloudflare-Tunnel",
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$CloudflaredExe = "C:\Program Files (x86)\cloudflared\cloudflared.exe",
    [string]$ConfigPath = "C:\EvolutionaryTradingAlgo\var\cloudflare\eta-engine-cloudflared.yml",
    [bool]$PreferInstalledService = $true,
    [switch]$Start,
    [switch]$RestartExistingProcess
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

function Sync-ShadowCloudflaredConfig {
    param([Parameter(Mandatory = $true)][string]$CanonicalConfigPath)

    $canonicalText = Get-Content -LiteralPath $CanonicalConfigPath -Raw
    $credentialLine = [regex]::Match($canonicalText, '(?mi)^credentials-file:\s*(.+)\s*$')
    if (-not $credentialLine.Success) {
        return $null
    }

    $credentialsPath = $credentialLine.Groups[1].Value.Trim()
    if ([string]::IsNullOrWhiteSpace($credentialsPath)) {
        return $null
    }

    $credentialDir = Split-Path -Parent $credentialsPath
    if ([string]::IsNullOrWhiteSpace($credentialDir)) {
        return $null
    }

    $shadowConfigPath = Join-Path $credentialDir "config.yml"
    if ([System.StringComparer]::OrdinalIgnoreCase.Equals($shadowConfigPath, $CanonicalConfigPath)) {
        return $null
    }

    New-Item -ItemType Directory -Force -Path $credentialDir | Out-Null
    Set-Content -LiteralPath $shadowConfigPath -Value $canonicalText -Encoding ASCII
    return $shadowConfigPath
}

$RootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
if ($RootFull -ne "C:\EvolutionaryTradingAlgo") {
    throw "Expected canonical ETA root C:\EvolutionaryTradingAlgo, got: $RootFull"
}

Assert-CanonicalEtaPath -Path $ConfigPath
if (-not (Test-Path -LiteralPath $CloudflaredExe)) {
    throw "Missing cloudflared executable: $CloudflaredExe"
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Missing Cloudflare tunnel config: $ConfigPath"
}

$logDir = Join-Path $RootFull "logs\cloudflare"
Assert-CanonicalEtaPath -Path $logDir
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$cloudflaredService = Get-CimInstance Win32_Service -Filter "Name='Cloudflared'" -ErrorAction SilentlyContinue
if ($PreferInstalledService -and $cloudflaredService) {
    $servicePath = [string]$cloudflaredService.PathName
    $serviceOwnsConfig = $servicePath.IndexOf($ConfigPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if ($serviceOwnsConfig) {
        $shadowConfigPath = Sync-ShadowCloudflaredConfig -CanonicalConfigPath $ConfigPath
        $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($existingTask) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        }

        if ($Start -and $cloudflaredService.State -ne "Running") {
            Start-Service -Name $cloudflaredService.Name
            Start-Sleep -Seconds 2
            $cloudflaredService = Get-CimInstance Win32_Service -Filter "Name='Cloudflared'" -ErrorAction SilentlyContinue
        }

        [pscustomobject]@{
            TaskName = $TaskName
            State = "SkippedServiceOwner"
            ServiceName = $cloudflaredService.Name
            ServiceState = $cloudflaredService.State
            UserId = "NT AUTHORITY\SYSTEM"
            ConfigPath = $ConfigPath
            CloudflaredExe = $CloudflaredExe
            ShadowConfigPath = $shadowConfigPath
            ShadowConfigSynced = -not [string]::IsNullOrWhiteSpace($shadowConfigPath)
            ScheduledTaskRemoved = [bool]$existingTask
        }
        return
    }
}

if ($RestartExistingProcess) {
    Get-Process cloudflared -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction `
    -Execute $CloudflaredExe `
    -Argument "tunnel --config `"$ConfigPath`" run" `
    -WorkingDirectory $RootFull
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

if ($PSCmdlet.ShouldProcess($TaskName, "Register named Cloudflare tunnel task")) {
    $shadowConfigPath = Sync-ShadowCloudflaredConfig -CanonicalConfigPath $ConfigPath
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description "ETA named Cloudflare tunnel for protected operator hosts." `
        -Force | Out-Null

    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }

    Get-ScheduledTask -TaskName $TaskName |
        Select-Object TaskName,State,@{Name="UserId";Expression={$_.Principal.UserId}},@{Name="ConfigPath";Expression={$ConfigPath}},@{Name="CloudflaredExe";Expression={$CloudflaredExe}},@{Name="ShadowConfigPath";Expression={$shadowConfigPath}}
}
