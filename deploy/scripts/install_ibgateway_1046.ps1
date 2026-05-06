[CmdletBinding()]
param(
    [string]$InstallerUrl = "https://download2.interactivebrokers.com/installers/ibgateway/latest-standalone/ibgateway-latest-standalone-windows-x64.exe",
    [string]$DownloadDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\downloads\ibgateway",
    [string]$GatewayDir = "C:\Jts\ibgateway\1046",
    [string]$StatePath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibgateway_install.json",
    [string]$RepairScript = "C:\EvolutionaryTradingAlgo\eta_engine\deploy\scripts\repair_ibgateway_vps.ps1",
    [switch]$ForceDownload,
    [switch]$Install,
    [switch]$AllowUnsignedInstaller,
    [switch]$RepairAfterInstall
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

function Normalize-PathString {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\").ToLowerInvariant()
}

function Assert-Gateway1046 {
    param([string]$Path)
    $expected = "C:\Jts\ibgateway\1046"
    if ((Normalize-PathString -Path $Path) -ne (Normalize-PathString -Path $expected)) {
        throw "Refusing non-canonical IB Gateway path: $Path; expected $expected"
    }
}

function Write-State {
    param([object]$Payload)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatePath) | Out-Null
    ($Payload | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $StatePath -Encoding ASCII
}

Assert-CanonicalEtaPath -Path $DownloadDir
Assert-CanonicalEtaPath -Path $StatePath
Assert-CanonicalEtaPath -Path $RepairScript
Assert-Gateway1046 -Path $GatewayDir

$installerName = Split-Path -Leaf ([System.Uri]$InstallerUrl).AbsolutePath
if (-not $installerName) {
    $installerName = "ibgateway-latest-standalone-windows-x64.exe"
}
$installerPath = Join-Path $DownloadDir $installerName
$result = [ordered]@{
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    installer_url = $InstallerUrl
    installer_path = $installerPath
    gateway_dir = $GatewayDir
    state_path = $StatePath
    downloaded = $false
    installer_length = 0
    installer_sha256 = ""
    authenticode_status = ""
    authenticode_status_message = ""
    signer_subject = ""
    install_requested = [bool]$Install
    install_attempted = $false
    install_exit_code = $null
    installed = $false
    repair_after_install_requested = [bool]$RepairAfterInstall
    repair_attempted = $false
    repair_exit_code = $null
    operator_action_required = $false
    operator_action = ""
}

try {
    New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null

    if ($ForceDownload -or -not (Test-Path -LiteralPath $installerPath)) {
        Invoke-WebRequest -UseBasicParsing -Uri $InstallerUrl -OutFile $installerPath -TimeoutSec 600
    }

    $item = Get-Item -LiteralPath $installerPath
    $hash = Get-FileHash -LiteralPath $installerPath -Algorithm SHA256
    $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
    $signer = ""
    if ($signature.SignerCertificate) {
        $signer = [string]$signature.SignerCertificate.Subject
    }

    $result.downloaded = $true
    $result.installer_length = $item.Length
    $result.installer_sha256 = $hash.Hash
    $result.authenticode_status = [string]$signature.Status
    $result.authenticode_status_message = [string]$signature.StatusMessage
    $result.signer_subject = $signer
    $result.installed = Test-Path -LiteralPath (Join-Path $GatewayDir "ibgateway.exe")

    if (-not $Install -and -not $result.installed) {
        $result.operator_action_required = $true
        $result.operator_action = (
            "IB Gateway 10.46 is not installed at C:\Jts\ibgateway\1046. " +
            "Rerun with -Install -RepairAfterInstall; only add " +
            "-AllowUnsignedInstaller after confirming the official IBKR download source."
        )
    }

    if ($Install) {
        if ($signature.Status -ne "Valid" -and -not $AllowUnsignedInstaller) {
            $result.operator_action_required = $true
            $result.operator_action = (
                "Installer Authenticode status is " + $signature.Status +
                "; rerun with -AllowUnsignedInstaller only after confirming the official IBKR download source."
            )
            Write-State -Payload $result
            $result | ConvertTo-Json -Depth 6
            exit 3
        }

        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $GatewayDir) | Out-Null
        $args = @(
            "-q",
            "-dir", $GatewayDir,
            "-DexecuteLauncherAction=false",
            "-DjtsConfigDir=$GatewayDir"
        )
        $process = Start-Process -FilePath $installerPath -ArgumentList $args -Wait -PassThru
        $result.install_attempted = $true
        $result.install_exit_code = $process.ExitCode
        $result.installed = Test-Path -LiteralPath (Join-Path $GatewayDir "ibgateway.exe")
        if (-not $result.installed) {
            $result.operator_action_required = $true
            $result.operator_action = "IB Gateway installer finished but C:\Jts\ibgateway\1046\ibgateway.exe is missing."
        }
    }

    if ($RepairAfterInstall -and $result.installed) {
        $repairArgs = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $RepairScript,
            "-ApplyJtsIni",
            "-ApplyVmOptions",
            "-RepairTasks",
            "-EnforceSingleSource"
        )
        $repair = Start-Process -FilePath "powershell.exe" -ArgumentList $repairArgs -Wait -PassThru
        $result.repair_attempted = $true
        $result.repair_exit_code = $repair.ExitCode
        if ($repair.ExitCode -ne 0) {
            $result.operator_action_required = $true
            $result.operator_action = "IB Gateway installed, but repair_ibgateway_vps.ps1 returned a non-zero exit code."
        }
    }
} catch {
    $result.operator_action_required = $true
    $result.operator_action = $_.Exception.Message
    Write-State -Payload $result
    $result | ConvertTo-Json -Depth 6
    throw
}

Write-State -Payload $result
$result | ConvertTo-Json -Depth 6
