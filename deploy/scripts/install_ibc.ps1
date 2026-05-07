[CmdletBinding()]
param(
    [string]$ReleaseApiUrl = "https://api.github.com/repos/IbcAlpha/IBC/releases/latest",
    [string]$ReleaseTag = "",
    [string]$AssetName = "",
    [string]$DownloadDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\downloads\ibc",
    [string]$InstallRoot = "C:\EvolutionaryTradingAlgo\var\eta_engine\tools\ibc",
    [string]$StatePath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibc_install.json",
    [switch]$ForceDownload,
    [switch]$Install
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

function Write-State {
    param([object]$Payload)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatePath) | Out-Null
    ($Payload | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $StatePath -Encoding ASCII
}

function Resolve-ReleaseApiTarget {
    if (-not [string]::IsNullOrWhiteSpace($ReleaseTag)) {
        $escapedTag = [System.Uri]::EscapeDataString($ReleaseTag.Trim())
        return "https://api.github.com/repos/IbcAlpha/IBC/releases/tags/$escapedTag"
    }
    return $ReleaseApiUrl
}

function Resolve-ReleaseAsset {
    param([object]$Release)

    if ($null -eq $Release) {
        throw "IBC release metadata is missing."
    }

    $assets = @($Release.assets)
    if ($assets.Count -eq 0) {
        throw "IBC release metadata did not include any assets."
    }

    if (-not [string]::IsNullOrWhiteSpace($AssetName)) {
        $exact = $assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
        if ($null -eq $exact) {
            throw "Requested IBC asset not found in release: $AssetName"
        }
        return $exact
    }

    $windowsAsset = $assets |
        Where-Object { $_.name -like "IBCWin-*.zip" } |
        Select-Object -First 1
    if ($null -eq $windowsAsset) {
        throw "Official IBC Windows ZIP asset (IBCWin-*.zip) was not found in the release metadata."
    }

    return $windowsAsset
}

function Find-IbcPayloadDir {
    param([string]$ExpandedRoot)

    $rootStartIbc = Join-Path $ExpandedRoot "scripts\StartIBC.bat"
    if (Test-Path -LiteralPath $rootStartIbc) {
        return $ExpandedRoot
    }

    $candidate = Get-ChildItem -Path $ExpandedRoot -Directory -Recurse -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "scripts\StartIBC.bat") } |
        Sort-Object FullName |
        Select-Object -First 1
    if ($null -eq $candidate) {
        throw "Expanded IBC payload does not contain scripts\StartIBC.bat."
    }

    return $candidate.FullName
}

function Remove-InstallDirIfPresent {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $normalizedInstallRoot = Normalize-PathString -Path $InstallRoot
    $normalizedTarget = Normalize-PathString -Path $Path
    if (-not $normalizedTarget.StartsWith($normalizedInstallRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove non-canonical IBC install directory: $Path"
    }

    Remove-Item -LiteralPath $Path -Recurse -Force
}

Assert-CanonicalEtaPath -Path $DownloadDir
Assert-CanonicalEtaPath -Path $InstallRoot
Assert-CanonicalEtaPath -Path $StatePath

$result = [ordered]@{
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    release_api_url = Resolve-ReleaseApiTarget
    requested_release_tag = $ReleaseTag
    requested_asset_name = $AssetName
    download_dir = $DownloadDir
    install_root = $InstallRoot
    state_path = $StatePath
    release_tag = ""
    release_name = ""
    published_at = ""
    asset_name = ""
    asset_url = ""
    asset_size = 0
    download_path = ""
    download_sha256 = ""
    install_requested = [bool]$Install
    install_dir = ""
    current_install_dir = ""
    installed = $false
    start_ibc_path = ""
    start_gateway_path = ""
    config_template_path = ""
    operator_action_required = $false
    operator_action = ""
}

try {
    $release = Invoke-RestMethod -Uri $result.release_api_url -Headers @{ "User-Agent" = "EvolutionaryTradingAlgo-IBCInstaller" }
    $asset = Resolve-ReleaseAsset -Release $release

    $result.release_tag = [string]$release.tag_name
    $result.release_name = [string]$release.name
    $result.published_at = [string]$release.published_at
    $result.asset_name = [string]$asset.name
    $result.asset_url = [string]$asset.browser_download_url
    $result.asset_size = [int64]$asset.size

    New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null

    $downloadPath = Join-Path $DownloadDir $result.asset_name
    $result.download_path = $downloadPath

    if ($ForceDownload -or -not (Test-Path -LiteralPath $downloadPath)) {
        Invoke-WebRequest `
            -UseBasicParsing `
            -Uri $result.asset_url `
            -OutFile $downloadPath `
            -Headers @{ "User-Agent" = "EvolutionaryTradingAlgo-IBCInstaller" } `
            -TimeoutSec 600
    }

    $hash = Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256
    $result.download_sha256 = $hash.Hash

    $sanitizedTag = if ([string]::IsNullOrWhiteSpace($result.release_tag)) {
        "unknown"
    } else {
        $result.release_tag.Trim()
    }
    $installDir = Join-Path $InstallRoot $sanitizedTag
    $result.install_dir = $installDir

    if ($Install) {
        $extractDir = Join-Path $InstallRoot ("_extract_" + $sanitizedTag + "_" + $PID)
        Remove-InstallDirIfPresent -Path $extractDir
        New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
        Expand-Archive -LiteralPath $downloadPath -DestinationPath $extractDir -Force
        $payloadDir = Find-IbcPayloadDir -ExpandedRoot $extractDir

        Remove-InstallDirIfPresent -Path $installDir
        New-Item -ItemType Directory -Force -Path $installDir | Out-Null
        Copy-Item -Path (Join-Path $payloadDir "*") -Destination $installDir -Recurse -Force
        Remove-InstallDirIfPresent -Path $extractDir

        $startIbcPath = Join-Path $installDir "scripts\StartIBC.bat"
        if (-not (Test-Path -LiteralPath $startIbcPath)) {
            throw "IBC install completed but scripts\StartIBC.bat is missing from $installDir"
        }

        $result.installed = $true
        $result.current_install_dir = $installDir
        $result.start_ibc_path = $startIbcPath
        $result.start_gateway_path = Join-Path $installDir "StartGateway.bat"
        $result.config_template_path = Join-Path $installDir "config.ini"
    } else {
        $existingStartIbc = Join-Path $installDir "scripts\StartIBC.bat"
        if (Test-Path -LiteralPath $existingStartIbc) {
            $result.installed = $true
            $result.current_install_dir = $installDir
            $result.start_ibc_path = $existingStartIbc
            $result.start_gateway_path = Join-Path $installDir "StartGateway.bat"
            $result.config_template_path = Join-Path $installDir "config.ini"
        } else {
            $result.operator_action_required = $true
            $result.operator_action = "IBC release metadata was fetched, but no installed runtime is present. Rerun with -Install."
        }
    }
} catch {
    $result.operator_action_required = $true
    $result.operator_action = $_.Exception.Message
    Write-State -Payload $result
    $result | ConvertTo-Json -Depth 8
    throw
}

Write-State -Payload $result
$result | ConvertTo-Json -Depth 8
