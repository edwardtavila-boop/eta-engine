# Install Hermes-agent on VPS — manual flow because install.ps1 over SSH stdin is flaky.
# Run on VPS via: pwsh hermes_vps_install.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$HermesHome = Join-Path $env:USERPROFILE '.hermes'
$HermesAgent = Join-Path $HermesHome 'hermes-agent'
$Uv = 'C:\Users\Administrator\.local\bin\uv.exe'

Write-Output ("Target: $HermesAgent")
New-Item -ItemType Directory -Force -Path $HermesHome | Out-Null

if (-not (Test-Path (Join-Path $HermesAgent 'pyproject.toml'))) {
    Write-Output 'Cloning hermes-agent (depth 1)...'
    git clone --depth 1 https://github.com/NousResearch/hermes-agent.git $HermesAgent 2>&1 | Select-Object -Last 5
}

Write-Output 'Building venv (Python 3.11 via uv)...'
& $Uv venv --python 3.11 (Join-Path $HermesAgent '.venv') 2>&1 | Select-Object -Last 5

Write-Output 'Installing deps (~2 min)...'
& $Uv pip install --python (Join-Path $HermesAgent '.venv\Scripts\python.exe') -e $HermesAgent 2>&1 | Select-Object -Last 10

Write-Output ''
Write-Output 'Smoke checks:'
Write-Output ('  python.exe: ' + (Test-Path (Join-Path $HermesAgent '.venv\Scripts\python.exe')))
Write-Output ('  cli.py:     ' + (Test-Path (Join-Path $HermesAgent 'cli.py')))
Write-Output ('  hermes:     ' + (Test-Path (Join-Path $HermesAgent 'hermes')))

# Linux-style 'venv/bin/python' via junctions so Hermes-desktop's hardcoded path resolves
$Junction = Join-Path $HermesAgent 'venv'
$BinJunction = Join-Path $Junction 'bin'
if (-not (Test-Path $Junction)) {
    New-Item -ItemType Junction -Path $Junction -Target (Join-Path $HermesAgent '.venv') | Out-Null
}
if (-not (Test-Path $BinJunction)) {
    New-Item -ItemType Junction -Path $BinJunction -Target (Join-Path $Junction 'Scripts') | Out-Null
}
Write-Output ('  venv junction: ' + (Test-Path $Junction))
Write-Output ('  venv/bin/python.exe: ' + (Test-Path (Join-Path $BinJunction 'python.exe')))
