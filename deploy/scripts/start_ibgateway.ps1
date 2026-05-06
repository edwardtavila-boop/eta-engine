[CmdletBinding()]
param(
    [string]$GatewayDir = "C:\Jts\ibgateway\1046",
    [string]$LoginProfile = "apexpredatoribkr",
    [string]$LogDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\logs\ibgateway",
    [string]$StateDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\state",
    [int]$ApiPort = 4002,
    [int]$StartupTimeoutSeconds = 600,
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"

function Write-LogLine {
    param([string]$Message)
    $stamp = (Get-Date).ToUniversalTime().ToString("o")
    Add-Content -LiteralPath (Join-Path $LogDir "start_ibgateway.log") -Value "$stamp $Message"
}

function Get-GatewayProcesses {
    Get-Process -Name "ibgateway" -ErrorAction SilentlyContinue
}

function Get-ApiListener {
    Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Get-ProcessIdValue {
    param($Process)
    if ($null -ne $Process.Id) {
        return $Process.Id
    }
    return $Process.ProcessId
}

function New-StartLock {
    param(
        [string]$Path,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            $payload = [System.Text.Encoding]::UTF8.GetBytes(
                "pid=$PID started=$((Get-Date).ToUniversalTime().ToString("o"))`n"
            )
            $stream.Write($payload, 0, $payload.Length)
            $stream.Flush()
            return $stream
        } catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 500
        }
    }

    throw "Gateway start lock is already held: $Path"
}

function Wait-NoGatewayProcesses {
    param([int]$TimeoutSeconds = 60)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $remaining = @(Get-GatewayProcesses)
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Wait-ApiListener {
    param([int]$TimeoutSeconds = 600)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listener = Get-ApiListener
        if ($listener) {
            Write-LogLine "gateway API listener ready port=$ApiPort pid=$($listener.OwningProcess)"
            return $listener
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    return $null
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$exe = Join-Path $GatewayDir "ibgateway.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    Write-LogLine "missing executable: $exe"
    throw "Missing IB Gateway executable: $exe"
}

$lockPath = Join-Path $StateDir "ibgateway_start.lock"
$lockStream = $null

try {
    $lockStream = New-StartLock -Path $lockPath

    $listener = Get-ApiListener
    if ($listener -and -not $ForceRestart) {
        Write-LogLine "existing gateway process running; no start needed port=$ApiPort pid=$($listener.OwningProcess)"
        return
    }

    $existingGateway = @(Get-GatewayProcesses)
    if ($existingGateway.Count -gt 0 -and -not $ForceRestart) {
        $pids = ($existingGateway | ForEach-Object { Get-ProcessIdValue $_ }) -join ","
        Write-LogLine "gateway process running without API listener; waiting for port=$ApiPort pids=$pids"
        $listener = Wait-ApiListener -TimeoutSeconds ([Math]::Min(60, $StartupTimeoutSeconds))
        if ($listener) {
            return
        }
        throw "Gateway process exists but API port $ApiPort did not become ready. pids=$pids"
    }

    if ($ForceRestart) {
        foreach ($proc in $existingGateway) {
            $procId = Get-ProcessIdValue $proc
            Write-LogLine "stopping existing gateway process pid=$procId"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
        if (-not (Wait-NoGatewayProcesses -TimeoutSeconds 60)) {
            $remaining = @(Get-GatewayProcesses | ForEach-Object { Get-ProcessIdValue $_ }) -join ","
            throw "Timed out waiting for IB Gateway processes to exit. remaining=$remaining"
        }
    }

    $arguments = "-login=$LoginProfile"
    Write-LogLine "starting $exe $arguments"
    $started = Start-Process -FilePath $exe `
        -ArgumentList $arguments `
        -WorkingDirectory $GatewayDir `
        -PassThru

    Write-LogLine "start requested pid=$(Get-ProcessIdValue $started)"
    $listener = Wait-ApiListener -TimeoutSeconds $StartupTimeoutSeconds
    if (-not $listener) {
        throw "IB Gateway did not open API port $ApiPort within $StartupTimeoutSeconds seconds"
    }
} catch {
    Write-LogLine "ERROR $($_.Exception.Message)"
    throw
} finally {
    if ($null -ne $lockStream) {
        $lockStream.Close()
        $lockStream.Dispose()
    }
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
