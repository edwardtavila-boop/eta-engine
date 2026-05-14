# EVOLUTIONARY TRADING ALGO // set_gateway_authority.ps1
# Marks the current host as the only allowed IBKR Gateway deployment authority.

[CmdletBinding()]
param(
    [ValidateSet("vps", "gateway_authority")]
    [string]$Role = "vps",
    [string]$Path = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\gateway_authority.json",
    [switch]$Apply,
    [switch]$Clear,
    [switch]$AllowDesktopHost
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
    return $resolved
}

$resolvedPath = Assert-CanonicalEtaPath -Path $Path
$os = Get-CimInstance Win32_OperatingSystem
$isDesktop = [int]$os.ProductType -eq 1

if ($Clear) {
    if ($Apply -and (Test-Path -LiteralPath $resolvedPath)) {
        Remove-Item -LiteralPath $resolvedPath -Force
    }
    [PSCustomObject]@{
        status = if ($Apply) { "cleared" } else { "dry_run_clear" }
        path = $resolvedPath
        computer_name = $env:COMPUTERNAME
    } | ConvertTo-Json -Depth 4
    exit 0
}

if ($isDesktop -and -not $AllowDesktopHost) {
    throw (
        "Refusing to mark workstation host '$env:COMPUTERNAME' as Gateway authority. " +
        "Run this on the Windows Server VPS, or pass -AllowDesktopHost only for an explicit break-glass lab."
    )
}

$payload = [ordered]@{
    enabled = $true
    role = $Role
    computer_name = $env:COMPUTERNAME
    user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    os_caption = [string]$os.Caption
    os_product_type = [int]$os.ProductType
    workspace_root = "C:\EvolutionaryTradingAlgo"
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    note = "This host is allowed to own the ETA IBKR Gateway session. Local/home desktops should not carry this marker."
}

if ($Apply) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resolvedPath) | Out-Null
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resolvedPath -Encoding ASCII
    Write-Host "OK: Gateway authority marker written to $resolvedPath"
} else {
    Write-Host "DRY RUN: would write Gateway authority marker to $resolvedPath"
}

$payload | ConvertTo-Json -Depth 4
