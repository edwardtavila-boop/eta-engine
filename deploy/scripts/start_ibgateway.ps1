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
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and
            ($_.CommandLine -like "*$GatewayDir*" -or $_.CommandLine -like "*ibgateway*")
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

$stdout = Join-Path $LogDir "ibgateway_stdout.log"
$stderr = Join-Path $LogDir "ibgateway_stderr.log"
$arguments = "-login=$LoginProfile"

Write-LogLine "starting $exe $arguments"
Start-Process -FilePath $exe `
    -ArgumentList $arguments `
    -WorkingDirectory $GatewayDir `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden

Write-LogLine "start requested"
