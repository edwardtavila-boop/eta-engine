@echo off
REM ETA Hermes secrets template — COPY this file to hermes_secrets.bat
REM and fill in real values. hermes_secrets.bat is gitignored.
REM hermes_run.bat calls this file to populate process env before
REM spawning the Hermes gateway.
REM
REM Why a sidecar (vs setx / User-scope env vars):
REM  Windows scheduled tasks on the VPS run as user `trader` but load
REM  Administrator's profile. Reg-query env propagation silently fails
REM  across that split — explicit `set` in a sourced bat is the only
REM  path that works reliably.
REM
REM Rotation: edit hermes_secrets.bat then `schtasks /Run /TN ETA-Hermes-Agent`.

set DEEPSEEK_API_KEY=REPLACE_WITH_YOUR_DEEPSEEK_KEY
set JARVIS_MCP_TOKEN=REPLACE_WITH_YOUR_MCP_TOKEN
set API_SERVER_KEY=REPLACE_WITH_YOUR_API_SERVER_KEY
