# ============================================================================
# cloudflare_setup_named.ps1
# One-shot: create a named Cloudflare Tunnel pointing dashboard.port 8000
# at jarvis.<DOMAIN>, build config.yml, register the scheduled task, start.
#
# Requires one of:
#   (A) cert.pem from `cloudflared login` already at ~\.cloudflared\cert.pem
#   (B) -ApiToken parameter with Cloudflare Tunnel:Edit + DNS:Edit scope
#
# Usage:
#   .\cloudflare_setup_named.ps1 -Domain evolutionarytradingalgo.live -Hostname jarvis
#   .\cloudflare_setup_named.ps1 -Domain evolutionarytradingalgo.live -Hostname jarvis -ApiToken <tok>
# ============================================================================
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Domain,
    [string]$Hostname = "jarvis",
    [int]$LocalPort = 8000,
    [string]$TunnelName = "eta-engine",
    [string]$ApiToken = $null
)

$ErrorActionPreference = "Stop"
function Write-Log { param($m) Write-Host "[cf-setup] $m" -ForegroundColor Cyan }
function Write-OK  { param($m) Write-Host "[ OK ] $m" -ForegroundColor Green }
function Die       { param($m) Write-Host "[FATAL] $m" -ForegroundColor Red; exit 1 }

$cfDir = Join-Path $env:USERPROFILE ".cloudflared"
New-Item -ItemType Directory -Force -Path $cfDir | Out-Null
$certPath   = Join-Path $cfDir "cert.pem"
$configPath = Join-Path $cfDir "config.yml"

# ---------- 1. Authenticate ----------
if ($ApiToken) {
    Write-Log "using API token for tunnel creation"
    # Cloudflared needs cert.pem for CLI ops. If missing, we can still create
    # via REST API and build the creds json manually.
    $useApi = $true
} elseif (Test-Path $certPath) {
    Write-Log "using cert.pem from $certPath"
    $useApi = $false
} else {
    Die "No cert.pem at $certPath and no -ApiToken provided. Run 'cloudflared login' first."
}

# ---------- 2. Create the tunnel ----------
$fqdn = "$Hostname.$Domain"
Write-Log "target FQDN: $fqdn -> http://127.0.0.1:$LocalPort"

if ($useApi) {
    # REST API path -- works with cert.pem OR just an API token
    Write-Log "creating tunnel via Cloudflare API"
    # Find account
    $headers = @{ "Authorization" = "Bearer $ApiToken"; "Content-Type" = "application/json" }
    $zones = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/zones?name=$Domain" -Headers $headers
    if (-not $zones.success -or $zones.result.Count -eq 0) {
        Die "zone $Domain not found on your CF account"
    }
    $zoneId = $zones.result[0].id
    $accountId = $zones.result[0].account.id
    Write-Log "zone_id=$zoneId account_id=$accountId"

    # Tunnel secret = random 32-byte hex, base64-encoded
    $secretBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($secretBytes)
    $secret = [Convert]::ToBase64String($secretBytes)

    # Check if tunnel exists already
    $existing = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/accounts/$accountId/cfd_tunnel?name=$TunnelName&is_deleted=false" -Headers $headers
    if ($existing.result -and $existing.result.Count -gt 0) {
        $tunnelId = $existing.result[0].id
        Write-Log "tunnel $TunnelName already exists (id=$tunnelId) -- reusing"
    } else {
        $body = @{ name = $TunnelName; tunnel_secret = $secret; config_src = "local" } | ConvertTo-Json
        $resp = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/accounts/$accountId/cfd_tunnel" `
            -Method POST -Headers $headers -Body $body
        if (-not $resp.success) { Die "tunnel create failed: $($resp.errors | ConvertTo-Json -Depth 5)" }
        $tunnelId = $resp.result.id
        Write-OK "created tunnel $TunnelName (id=$tunnelId)"
    }

    # Write credentials file
    $creds = @{
        AccountTag   = $accountId
        TunnelID     = $tunnelId
        TunnelName   = $TunnelName
        TunnelSecret = $secret
    } | ConvertTo-Json
    $credsPath = Join-Path $cfDir "$tunnelId.json"
    Set-Content -Path $credsPath -Value $creds -Encoding ASCII
    Write-OK "wrote credentials: $credsPath"

    # Create DNS CNAME
    $cnameTarget = "$tunnelId.cfargotunnel.com"
    $existingDns = Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/zones/$zoneId/dns_records?name=$fqdn" -Headers $headers
    if ($existingDns.result -and $existingDns.result.Count -gt 0) {
        $recId = $existingDns.result[0].id
        $body = @{ type = "CNAME"; name = $fqdn; content = $cnameTarget; proxied = $true; ttl = 1 } | ConvertTo-Json
        Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/zones/$zoneId/dns_records/$recId" `
            -Method PUT -Headers $headers -Body $body | Out-Null
        Write-OK "updated DNS CNAME $fqdn -> $cnameTarget"
    } else {
        $body = @{ type = "CNAME"; name = $fqdn; content = $cnameTarget; proxied = $true; ttl = 1 } | ConvertTo-Json
        Invoke-RestMethod -Uri "https://api.cloudflare.com/client/v4/zones/$zoneId/dns_records" `
            -Method POST -Headers $headers -Body $body | Out-Null
        Write-OK "created DNS CNAME $fqdn -> $cnameTarget"
    }
} else {
    # cert.pem path
    Write-Log "creating tunnel via cloudflared CLI"
    $out = & cloudflared tunnel list --name $TunnelName -o json 2>&1 | Out-String
    if ($out -match '"id":\s*"([^"]+)"') {
        $tunnelId = $matches[1]
        Write-Log "tunnel $TunnelName already exists (id=$tunnelId) -- reusing"
    } else {
        & cloudflared tunnel create $TunnelName 2>&1 | Out-Null
        $out = & cloudflared tunnel list --name $TunnelName -o json 2>&1 | Out-String
        if ($out -match '"id":\s*"([^"]+)"') { $tunnelId = $matches[1] }
        else { Die "tunnel create failed" }
        Write-OK "created tunnel $TunnelName (id=$tunnelId)"
    }
    # Credentials file was auto-written by cloudflared at $cfDir\$tunnelId.json
    & cloudflared tunnel route dns --overwrite-dns $TunnelName $fqdn 2>&1 | Out-Null
    Write-OK "DNS route $fqdn -> $TunnelName"
}

# ---------- 3. Write config.yml ----------
$credsFile = Join-Path $cfDir "$tunnelId.json"
$configYml = @"
tunnel: $tunnelId
credentials-file: $credsFile
logfile: $env:LOCALAPPDATA\eta_engine\logs\cloudflare-tunnel.log
no-autoupdate: true
ingress:
  - hostname: $fqdn
    service: http://127.0.0.1:$LocalPort
  - service: http_status:404
"@
Set-Content -Path $configPath -Value $configYml -Encoding ASCII
Write-OK "wrote config.yml"

# ---------- 4. Register scheduled task ----------
$stateDir = Join-Path $env:LOCALAPPDATA "eta_engine\state"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

$stateFile = Join-Path $stateDir "cloudflare_tunnel.json"
@{
    kind       = "named_tunnel"
    name       = $TunnelName
    tunnel_id  = $tunnelId
    fqdn       = $fqdn
    url        = "https://$fqdn"
    port       = $LocalPort
    created_at = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8

# Kill old quick tunnel task, install new named one
Unregister-ScheduledTask -TaskName "Apex-Cloudflare-Tunnel" -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "cloudflared" `
    -Argument "tunnel --config `"$configPath`" run $TunnelName"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "Apex-Cloudflare-Tunnel" -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null
Write-OK "task Apex-Cloudflare-Tunnel registered for named tunnel"

# ---------- 5. Start it ----------
# Stop any existing quick tunnel processes first
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "Apex-Cloudflare-Tunnel"

Write-OK "DONE"
Write-Host ""
Write-Host "  URL:     https://$fqdn" -ForegroundColor Green
Write-Host "  Tunnel:  $TunnelName ($tunnelId)"
Write-Host "  Config:  $configPath"
Write-Host "  Creds:   $credsFile"
Write-Host ""
