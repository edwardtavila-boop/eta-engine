[CmdletBinding()]
param(
    [string]$GatewayDir = "C:\Jts\ibgateway\1046",
    [string]$LoginProfile = "apexpredatoribkr",
    [string]$LogDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\logs\ibgateway",
    [int]$ApiPort = 4002,
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"

function Write-LogLine {
    param([string]$Message)
    $stamp = (Get-Date).ToUniversalTime().ToString("o")
    Add-Content -LiteralPath (Join-Path $LogDir "start_ibgateway.log") -Value "$stamp $Message"
}

function Get-GatewayProcesses {
    $expectedExe = Join-Path $GatewayDir "ibgateway.exe"
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -ieq "ibgateway.exe") -or
            ($_.ExecutablePath -and ($_.ExecutablePath -ieq $expectedExe))
        }
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$exe = Join-Path $GatewayDir "ibgateway.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    Write-LogLine "missing executable: $exe"
    throw "Missing IB Gateway executable: $exe"
}

$listener = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener -and -not $ForceRestart) {
    Write-LogLine "port $ApiPort already listening; no start needed"
    return
}

if ($ForceRestart) {
    foreach ($proc in Get-GatewayProcesses) {
        Write-LogLine "stopping existing gateway process pid=$($proc.ProcessId)"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 5
}

$arguments = "-login=$LoginProfile"
$cmdArgs = "/c start ""IBGateway"" /D ""$GatewayDir"" ""$exe"" $arguments"

Write-LogLine "starting detached $exe $arguments"
Start-Process -FilePath "cmd.exe" `
    -ArgumentList $cmdArgs `
    -WorkingDirectory $GatewayDir `
    -WindowStyle Hidden

Write-LogLine "start requested"
