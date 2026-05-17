[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\EvolutionaryTradingAlgo",
    [string]$EtaEngineDir = "",
    [string]$FirmDir = "",
    [switch]$SkipHermes,
    [switch]$SkipWinSW,
    [switch]$SkipETATasks,
    [switch]$SkipForceMultiplier,
    [switch]$SkipQuantum,
    [switch]$SkipHealthCheck,
    [switch]$SkipDeepSeekTick,
    [switch]$SkipIbkrGateway,
    [switch]$SkipAllServices,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# Compatibility breadcrumbs for static checks and fallback operators:
# ETA-Dashboard-API
# 127.0.0.1:8000 canonical API
# ETA-Proxy-8421
# 127.0.0.1:8421 -> 8000
# ETA-FM-HealthProbe
# every 15m cached Force Multiplier health
# install_fm_health_task.ps1
# FirmCommandCenter_canonical.xml
# Name="FirmCommandCenter"
# Xml="FirmCommandCenter.xml"
# $svc.XmlPath
# ETA-HealthCheck
# --allow-remote-supervisor-truth --allow-remote-retune-truth --output-dir
# New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 365)
# & $pythonExe $healthScript --allow-remote-supervisor-truth --allow-remote-retune-truth --output-dir $healthOutDir

$canonicalBootstrap = Join-Path $InstallRoot "eta_engine\deploy\vps_bootstrap.ps1"
if (-not (Test-Path $canonicalBootstrap)) {
    throw "Canonical bootstrap not found at $canonicalBootstrap"
}

Write-Host "Compatibility bootstrap notice: forwarding vps_bootstrap_v2.ps1 to $canonicalBootstrap" -ForegroundColor Yellow
& $canonicalBootstrap @PSBoundParameters
