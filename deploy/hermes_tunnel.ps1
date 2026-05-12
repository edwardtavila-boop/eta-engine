# Desktop-side SSH tunnel that exposes the VPS's two operator-facing
# services on the operator's localhost:
#
#   8642 -> VPS Hermes API gateway
#   8643 -> VPS jarvis_status_server (direct contact-point page)
#
# Single ssh process carries both forwards. The watcher probes 8642
# (the primary) every 30s; if it's down, the entire ssh process is
# restarted, taking 8643 with it. Both ports come back together.

param(
    [string]$VpsAlias = 'forex-vps',
    [int]$HermesLocalPort = 8642,
    [int]$HermesRemotePort = 8642,
    [int]$StatusLocalPort = 8643,
    [int]$StatusRemotePort = 8643
)

$ErrorActionPreference = 'Continue'
$logPath = "$env:USERPROFILE\.hermes\hermes_tunnel.log"
New-Item -ItemType Directory -Force -Path (Split-Path $logPath -Parent) | Out-Null

function Write-Log {
    param([string]$Msg)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Msg
    Add-Content -Path $logPath -Value $line
}

Write-Log ("tunnel watcher started (local {0}+{1} -> {2}:{3}+{4})" -f `
    $HermesLocalPort, $StatusLocalPort, $VpsAlias, $HermesRemotePort, $StatusRemotePort)

while ($true) {
    $tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port $HermesLocalPort -WarningAction SilentlyContinue -InformationLevel Quiet
    if (-not $tcp) {
        Write-Log "tunnel down; (re)starting ssh with -L $HermesLocalPort + -L $StatusLocalPort"
        # -N: no remote command. -T: no pseudo-tty.
        # -o ServerAliveInterval=30 ServerAliveCountMax=2: probe every 30s,
        #   drop after 2 misses so a flaky network triggers fast reconnect.
        # -L: explicit 127.0.0.1 binds because Windows OpenSSH resolves
        #   "localhost" to IPv6, which doesn't match the VPS servers
        #   (both bind 127.0.0.1 only).
        Start-Process -WindowStyle Hidden -FilePath ssh -ArgumentList @(
            '-N', '-T',
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=2',
            '-o', 'ExitOnForwardFailure=yes',
            '-L', "${HermesLocalPort}:127.0.0.1:${HermesRemotePort}",
            '-L', "${StatusLocalPort}:127.0.0.1:${StatusRemotePort}",
            $VpsAlias
        )
    }
    Start-Sleep -Seconds 30
}
