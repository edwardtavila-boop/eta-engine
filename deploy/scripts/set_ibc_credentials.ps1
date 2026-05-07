[CmdletBinding()]
param(
    [string]$LoginId = "",
    [string]$Password = "",
    [string]$CredentialJsonPath = "C:\EvolutionaryTradingAlgo\eta_engine\secrets\ibkr_credentials.json",
    [switch]$PromptForPassword
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

function Resolve-JsonLoginId {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }

    try {
        $json = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        foreach ($key in @("username", "user", "login", "ib_login_id", "user_id")) {
            $value = $json.$key
            if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
                return [string]$value
            }
        }
    } catch {
        return ""
    }

    return ""
}

function Read-SecurePasswordFromPrompt {
    $secure = Read-Host -Prompt "IBKR password" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

Assert-CanonicalEtaPath -Path $CredentialJsonPath

$resolvedLoginId = $LoginId
if ([string]::IsNullOrWhiteSpace($resolvedLoginId)) {
    $resolvedLoginId = Resolve-JsonLoginId -Path $CredentialJsonPath
}
if ([string]::IsNullOrWhiteSpace($resolvedLoginId)) {
    $resolvedLoginId = [Environment]::GetEnvironmentVariable("ETA_IBC_LOGIN_ID", "Machine")
}

$resolvedPassword = $Password
if ([string]::IsNullOrWhiteSpace($resolvedPassword)) {
    $resolvedPassword = [Environment]::GetEnvironmentVariable("ETA_IBC_PASSWORD", "Machine")
}
if ([string]::IsNullOrWhiteSpace($resolvedPassword) -and $PromptForPassword) {
    $resolvedPassword = Read-SecurePasswordFromPrompt
}

if ([string]::IsNullOrWhiteSpace($resolvedLoginId)) {
    throw "Missing IBKR login id. Pass -LoginId or populate secrets\ibkr_credentials.json with username."
}
if ([string]::IsNullOrWhiteSpace($resolvedPassword)) {
    throw "Missing IBKR password. Pass -Password or rerun with -PromptForPassword."
}

[Environment]::SetEnvironmentVariable("ETA_IBC_LOGIN_ID", $resolvedLoginId, "Machine")
[Environment]::SetEnvironmentVariable("ETA_IBC_PASSWORD", $resolvedPassword, "Machine")

Write-Output "ETA_IBC_LOGIN_ID=seeded"
Write-Output "ETA_IBC_PASSWORD=seeded"
