# jarvis-trading — Hermes skill package

A Hermes Agent skill that connects to **JARVIS**, the live policy authority for the Evolutionary Trading Algo. Install this skill into your local Hermes Agent and the trading-copilot persona is available to every conversation: fleet status, kaizen runs, strategy deploy/retire, kill switch, and live consult-trace explanations.

This is the **operator-facing** half of the JARVIS↔Hermes bridge. The MCP server it talks to lives in `eta_engine/mcp_servers/jarvis_mcp_server.py`.

## Prerequisites

1. **Hermes Agent installed.** Run `hermes-desktop` at least once so it bootstraps `~/.hermes/`. If the deploy script can't find `~/.hermes/skills/`, it exits with a diagnostic.
2. **`JARVIS_MCP_TOKEN` environment variable set.** Same value on both sides (this skill + the JARVIS MCP server). Generate one and persist it in your shell profile / Windows env vars:
   ```powershell
   $env:JARVIS_MCP_TOKEN = (python -c "import secrets; print(secrets.token_hex(32))")
   [Environment]::SetEnvironmentVariable('JARVIS_MCP_TOKEN', $env:JARVIS_MCP_TOKEN, 'User')
   ```
   ```bash
   export JARVIS_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
   # Persist to ~/.bashrc or equivalent.
   ```
3. **Python with `eta_engine` importable.** The manifest's `PYTHONPATH` defaults to `C:\EvolutionaryTradingAlgo`. Adjust if you've cloned ETA elsewhere — edit `manifest.yaml` before deploying.

## Install

### Windows

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine\hermes_skills\jarvis-trading
pwsh deploy.ps1
# Or, skip the overwrite prompt:
pwsh deploy.ps1 -Force
```

### Linux (e.g. the VPS, if Hermes ever runs there)

```bash
cd /opt/eta/eta_engine/hermes_skills/jarvis-trading
bash deploy.sh
# Or, skip the overwrite prompt:
bash deploy.sh --force
```

Both scripts copy the tree into `~/.hermes/skills/jarvis-trading/`, warn if `JARVIS_MCP_TOKEN` is unset, and verify `manifest.yaml` lands. Restart the Hermes Agent service after install so it picks up the new skill.

## Verification

1. Restart Hermes Agent.
2. Open `hermes-desktop` and ask: **"list available skills"**.
3. Confirm `jarvis-trading` appears.
4. Ask: **"what's the fleet doing?"** — Hermes should invoke `jarvis_fleet_status` and render a 5-line summary.
5. Confirm the 06:30 morning briefing arrives the next day via Telegram. (Manual trigger: ask Hermes "run the morning briefing now".)

The audit log lives at `var/eta_engine/state/hermes_actions.jsonl` on the JARVIS host — every Hermes-issued JARVIS tool call writes one line.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` from any tool | Token mismatch between Hermes and JARVIS | Confirm `JARVIS_MCP_TOKEN` is set in **both** environments to the same value. Restart both services after rotating. |
| `MCP server failed to start` | `eta_engine` not importable | Verify `PYTHONPATH` in `manifest.yaml` matches your ETA workspace root. Test with `python -m eta_engine.mcp_servers.jarvis_mcp_server` directly. |
| `jarvis_kill_switch` rejects with "phrase mismatch" | Operator (or you) typed something other than `kill all` | The phrase is enforced verbatim. Re-ask the operator for the exact phrase; do not paraphrase. |
| Tool returns `status: HELD` on deploy/retire | 2-run gate hasn't confirmed | Not an error — recommendation needs to appear in two consecutive kaizen runs before applying. Wait for the next 06:00 loop. |
| `jarvis_wiring_audit` reports dark modules | A JARVIS subsystem isn't reporting | Check the JARVIS supervisor log. Dark modules degrade decision quality but don't halt the system. |
| Morning briefing didn't arrive at 06:30 | Scheduler not running, or kaizen still in flight at 06:30 | Check Hermes's scheduled-tasks log. If kaizen routinely overruns 06:30, slide the cron later (edit `manifest.yaml`, redeploy, restart). |

## Uninstall

```powershell
Remove-Item -Recurse "$env:USERPROFILE\.hermes\skills\jarvis-trading"
```

```bash
rm -rf "${HOME}/.hermes/skills/jarvis-trading"
```

Restart Hermes Agent after removing.

## File layout

```
jarvis-trading/
  SKILL.md              # capability description (Hermes reads this)
  SOUL.md               # persona slice merged into operator's active SOUL
  manifest.yaml         # MCP server connection + toolsets + scheduled tasks
  examples/             # 4 example operator-Hermes-JARVIS conversations
    fleet_status.md
    explain_loss.md
    kill_switch.md
    morning_briefing.md
  deploy.ps1            # Windows install
  deploy.sh             # Linux install
  README.md             # this file
```
