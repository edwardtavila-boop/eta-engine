[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "ETA-Cloudflare-Tunnel",
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$CloudflaredExe = "C:\Program Files (x86)\cloudflared\cloudflared.exe",
    [string]$ConfigPath = "C:\EvolutionaryTradingAlgo\var\cloudflare\eta-engine-cloudflared.yml",
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
        Select-Object TaskName,State,@{Name="UserId";Expression={$_.Principal.UserId}},@{Name="ConfigPath";Expression={$ConfigPath}},@{Name="CloudflaredExe";Expression={$CloudflaredExe}}
}
