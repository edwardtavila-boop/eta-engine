# Desktop-side SSH tunnel that exposes the VPS's Hermes Agent API on
# the operator's localhost:8642. The tunnel is restarted if SSH ever
# drops (every 30s health check).

param(
    [string]$VpsAlias = 'forex-vps',
    [int]$LocalPort = 8642,
    [int]$RemotePort = 8642
)

$ErrorActionPreference = 'Continue'
$logPath = "$env:USERPROFILE\.hermes\hermes_tunnel.log"
New-Item -ItemType Directory -Force -Path (Split-Path $logPath -Parent) | Out-Null

function Write-Log {
    param([string]$Msg)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Msg
    Add-Content -Path $logPath -Value $line
}

Write-Log "tunnel watcher started (local $LocalPort -> $VpsAlias`:$RemotePort)"

while ($true) {
    $tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port $LocalPort -WarningAction SilentlyContinue -InformationLevel Quiet
    if (-not $tcp) {
        Write-Log "tunnel down; (re)starting ssh -L $LocalPort`:localhost:$RemotePort"
        # -N: no remote command. -T: no pseudo-tty.
        # -o ServerAliveInterval=30 ServerAliveCountMax=2: probe every 30s, drop after 2 misses
        Start-Process -WindowStyle Hidden -FilePath ssh -ArgumentList @(
            '-N', '-T',
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=2',
            '-o', 'ExitOnForwardFailure=yes',
            # Explicit IPv4 target: Windows OpenSSH resolves "localhost" to IPv6
            # which doesn't match the VPS Hermes API server (binds 127.0.0.1 only).
            '-L', "${LocalPort}:127.0.0.1:${RemotePort}",
            $VpsAlias
        )
    }
    Start-Sleep -Seconds 30
}
