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

Write-Output '=== Step 1: Mirror all jarvis-* skills into ~/.hermes/skills/ ==='
# As of 2026-05-12 there are 5 skills: jarvis-trading (the core MCP bridge)
# plus 4 workflow skills from Track 4 (daily-review, drawdown-response,
# anomaly-investigator, pre-event-prep). Mirror everything under
# hermes_skills/jarvis-* automatically so adding new skills later is just
# "drop a folder and re-run postinstall".
$SkillsRoot = Join-Path $HermesHome 'skills'
New-Item -ItemType Directory -Force -Path $SkillsRoot | Out-Null
$SrcRoot = 'C:\EvolutionaryTradingAlgo\eta_engine\hermes_skills'
$SkillDirs = Get-ChildItem $SrcRoot -Directory -Filter 'jarvis-*' -ErrorAction SilentlyContinue
foreach ($SrcSkill in $SkillDirs) {
    $Dest = Join-Path $SkillsRoot $SrcSkill.Name
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $SrcSkill.FullName '*') -Destination $Dest
    Write-Output ("  installed " + $SrcSkill.Name)
}
Write-Output ("  total skills mirrored: " + $SkillDirs.Count)

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

Write-Output '=== Step 6: Register DeepSeek credential as literal (avoids env-template trap) ==='
# Hermes 0.3.6 credential pool resolves `env:DEEPSEEK_API_KEY` at request
# time. If env resolution fails the literal template string is sent to
# DeepSeek and you get HTTP 401. Registering as `--api-key <literal>`
# stores the actual key in state.db and bypasses env resolution.
if ($env:DEEPSEEK_API_KEY) {
    Push-Location $HermesAgent
    & $VenvPy hermes auth add deepseek --type api-key --label 'DEEPSEEK_API_KEY (literal)' --api-key $env:DEEPSEEK_API_KEY 2>&1 | Select-Object -Last 3
    Pop-Location
} else {
    Write-Output "  SKIP: DEEPSEEK_API_KEY env var not set"
}

Write-Output '=== Step 7: Final smoke ==='
Write-Output ("  config.yaml:        " + (Test-Path (Join-Path $HermesHome 'config.yaml')))
Write-Output ("  jarvis-trading:     " + (Test-Path (Join-Path $SkillsRoot 'jarvis-trading\manifest.yaml')))
Write-Output ("  daily-review skill: " + (Test-Path (Join-Path $SkillsRoot 'jarvis-daily-review\SKILL.md')))
Write-Output ("  drawdown skill:     " + (Test-Path (Join-Path $SkillsRoot 'jarvis-drawdown-response\SKILL.md')))
Write-Output ("  anomaly skill:      " + (Test-Path (Join-Path $SkillsRoot 'jarvis-anomaly-investigator\SKILL.md')))
Write-Output ("  pre-event skill:    " + (Test-Path (Join-Path $SkillsRoot 'jarvis-pre-event-prep\SKILL.md')))
Write-Output ("  mcp package:        " + (Test-Path (Join-Path $Site 'mcp')))
Write-Output ("  eta_engine.pth:     " + (Test-Path $PthPath))
Write-Output ("  venv python:        " + (Test-Path $VenvPy))
Write-Output ("  cli.py:             " + (Test-Path (Join-Path $HermesAgent 'cli.py')))
