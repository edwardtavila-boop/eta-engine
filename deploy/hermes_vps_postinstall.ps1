# Post-install steps on VPS: mirror skill, install mcp, drop pth.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Continue'

$HermesHome = Join-Path $env:USERPROFILE '.hermes'
$HermesAgent = Join-Path $HermesHome 'hermes-agent'
$Venv = Join-Path $HermesAgent '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$Site = Join-Path $Venv 'Lib\site-packages'
$Skills = Join-Path $HermesHome 'skills\jarvis-trading'
$Uv = 'C:\Users\Administrator\.local\bin\uv.exe'

Write-Output '=== Step 1: Mirror jarvis-trading skill into ~/.hermes/skills/ ==='
$SrcSkill = 'C:\EvolutionaryTradingAlgo\eta_engine\hermes_skills\jarvis-trading'
if (Test-Path $SrcSkill) {
    New-Item -ItemType Directory -Force -Path $Skills | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $SrcSkill '*') -Destination $Skills
    Write-Output ("  copied " + (Get-ChildItem $Skills).Count + " items")
} else {
    Write-Output "  SOURCE MISSING: $SrcSkill"
}

Write-Output '=== Step 2: Install mcp package into VPS venv ==='
& $Uv pip install --python $VenvPy mcp 2>&1 | Select-Object -Last 5
Write-Output ("  mcp pkg installed: " + (Test-Path (Join-Path $Site 'mcp')))

Write-Output '=== Step 3: Drop eta_engine.pth so eta_engine imports without PYTHONPATH ==='
$PthPath = Join-Path $Site 'eta_engine.pth'
Set-Content -Encoding ASCII -NoNewline -Path $PthPath -Value 'C:\EvolutionaryTradingAlgo'
Write-Output ("  pth file: $PthPath")
Write-Output ("  contents: " + (Get-Content $PthPath))

Write-Output '=== Step 4: Verify eta_engine importable from VPS venv python ==='
& $VenvPy -c "import eta_engine.mcp_servers.jarvis_mcp_server; print('IMPORT_OK from VPS venv')" 2>&1

Write-Output '=== Step 5: Register jarvis-trading skill via hermes CLI ==='
& $VenvPy -m hermes_cli.skills_hub install $Skills 2>&1 | Select-Object -Last 5

Write-Output '=== Step 6: Final smoke ==='
Write-Output ("  config.yaml:        " + (Test-Path (Join-Path $HermesHome 'config.yaml')))
Write-Output ("  skill manifest:     " + (Test-Path (Join-Path $Skills 'manifest.yaml')))
Write-Output ("  mcp package:        " + (Test-Path (Join-Path $Site 'mcp')))
Write-Output ("  eta_engine.pth:     " + (Test-Path $PthPath))
Write-Output ("  venv python:        " + (Test-Path $VenvPy))
Write-Output ("  cli.py:             " + (Test-Path (Join-Path $HermesAgent 'cli.py')))
