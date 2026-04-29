# ============================================================================
# register_cloudflare_quick.ps1
# Registers Apex-Cloudflare-Tunnel as an AtLogOn Task Scheduler task that
# runs a Quick Tunnel (random *.trycloudflare.com URL, no auth needed).
#
# This is the ephemeral proof-of-life. URL changes on every restart. For
# a persistent named tunnel with your own domain, use cloudflared login
# first, then register_cloudflare_named.ps1 (built after auth exists).
# ============================================================================
[CmdletBinding()]
param(
    [int]$LocalPort = 8000,
    [string]$TaskName = "Apex-Cloudflare-Tunnel"
)

$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$logDir = Join-Path $workspaceRoot "logs\eta_engine"
$stateDir = Join-Path $workspaceRoot "var\cloudflare"
New-Item -ItemType Directory -Force -Path $logDir, $stateDir | Out-Null

$logPath = Join-Path $logDir "cloudflare-tunnel.log"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$cloudflaredExe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflaredExe)) {
    Write-Host "[cf-tunnel] FATAL: cloudflared.exe not found at $cloudflaredExe" -ForegroundColor Red
    exit 2
}

# Write a small wrapper script so stdout/stderr go to our log dir, and the
# extracted trycloudflare.com URL gets parsed into a state file.
$wrapperPath = Join-Path $stateDir "cloudflare_tunnel_wrapper.ps1"
$wrapperContent = @'
param(
    [int]$Port = 8000,
    [string]$LogPath,
    [string]$UrlStatePath
)
# Kill any prior cloudflared processes to avoid port-binding conflicts.
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$exe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$args = @("tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate", "--logfile", $LogPath)

# Start cloudflared in the foreground of this wrapper -- Task Scheduler keeps
# the wrapper alive, wrapper keeps cloudflared alive.
$proc = Start-Process -FilePath $exe -ArgumentList $args -PassThru -NoNewWindow
$proc.WaitForInputIdle() 2>$null

# Poll the log every 2s for up to 30s to extract the trycloudflare.com URL.
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$url = $null
while ($sw.Elapsed.TotalSeconds -lt 30 -and $proc.HasExited -eq $false) {
    Start-Sleep -Seconds 2
    if (Test-Path $LogPath) {
        $m = Select-String -Path $LogPath -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -List
        if ($m) { $url = $m.Matches[0].Value; break }
    }
}
if ($url) {
    $state = @{
        url = $url
        port = $Port
        pid = $proc.Id
        started_at = (Get-Date).ToString("o")
        kind = "quick_tunnel"
    } | ConvertTo-Json
    Set-Content -Path $UrlStatePath -Value $state -Encoding UTF8
}
# Wait for the tunnel process; if it dies, the wrapper dies and Task
# Scheduler restarts us.
$proc.WaitForExit()
exit $proc.ExitCode
'@
Set-Content -Path $wrapperPath -Value $wrapperContent -Encoding UTF8

$urlStatePath = Join-Path $stateDir "cloudflare_tunnel.json"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapperPath`" -Port $LocalPort -LogPath `"$logPath`" -UrlStatePath `"$urlStatePath`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null

Write-Host "[ OK ] registered $TaskName -> localhost:$LocalPort" -ForegroundColor Green
Write-Host "[cf-tunnel] log path:    $logPath"
Write-Host "[cf-tunnel] url state:   $urlStatePath"
Write-Host "[cf-tunnel] next: Start-ScheduledTask -TaskName $TaskName"
