[CmdletBinding()]
param(
    [string]$LoginId = "",
    [string]$Password = "",
    [string]$CredentialJsonPath = "C:\EvolutionaryTradingAlgo\eta_engine\secrets\ibkr_credentials.json",
    [string]$PasswordFilePath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt",
    [switch]$SkipPasswordFile,
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

function Protect-SecretFilePath {
    param([string]$Path)

    try {
        $aclArgs = @(
            "`"$Path`"",
            "/inheritance:r",
            "/grant:r",
            "$env:USERNAME`:F",
            "/grant:r",
            "SYSTEM`:F"
        )
        Start-Process -FilePath "icacls.exe" -ArgumentList $aclArgs -Wait -NoNewWindow | Out-Null
    } catch {
        Write-Output "IBC_PASSWORD_FILE_ACL=warning"
    }
}

function Write-PasswordFile {
    param(
        [string]$Path,
        [string]$Secret
    )

    Assert-CanonicalEtaPath -Path $Path
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $Path) {
        try {
            $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            $acl = Get-Acl -LiteralPath $Path
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
                $identity,
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                [System.Security.AccessControl.AccessControlType]::Allow
            )
            $acl.SetAccessRule($rule)
            Set-Acl -LiteralPath $Path -AclObject $acl
        } catch {
            try {
                $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
                Start-Process -FilePath "icacls.exe" -ArgumentList @("`"$Path`"", "/grant:r", "$identity`:(F)") -Wait -NoNewWindow | Out-Null
            } catch {
                Write-Output "IBC_PASSWORD_FILE_WRITABLE=warning"
            }
        }
        try {
            Set-ItemProperty -LiteralPath $Path -Name IsReadOnly -Value $false -ErrorAction Stop
        } catch {
            Write-Output "IBC_PASSWORD_FILE_READONLY=warning"
        }
    }
    Set-Content -LiteralPath $Path -Value $Secret -Encoding UTF8 -NoNewline
    Protect-SecretFilePath -Path $Path
}

function Set-SecretEnvironmentVariable {
    param(
        [string]$Name,
        [string]$Value
    )

    try {
        [Environment]::SetEnvironmentVariable($Name, $Value, "Machine")
        return "machine"
    } catch {
        [Environment]::SetEnvironmentVariable($Name, $Value, "User")
        return "user"
    }
}

Assert-CanonicalEtaPath -Path $CredentialJsonPath
Assert-CanonicalEtaPath -Path $PasswordFilePath

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

if (-not $SkipPasswordFile) {
    Write-PasswordFile -Path $PasswordFilePath -Secret $resolvedPassword
}

$loginEnvScope = Set-SecretEnvironmentVariable -Name "ETA_IBC_LOGIN_ID" -Value $resolvedLoginId
$passwordEnvScope = Set-SecretEnvironmentVariable -Name "ETA_IBC_PASSWORD" -Value $resolvedPassword

Write-Output "ETA_IBC_LOGIN_ID=seeded"
Write-Output "ETA_IBC_PASSWORD=seeded"
Write-Output "ETA_IBC_LOGIN_ID_SCOPE=$loginEnvScope"
Write-Output "ETA_IBC_PASSWORD_SCOPE=$passwordEnvScope"
if (-not $SkipPasswordFile) {
    Write-Output "IBC_PASSWORD_FILE=seeded"
}
