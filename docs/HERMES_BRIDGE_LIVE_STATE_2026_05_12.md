# Hermes-JARVIS Bridge — Live State Reference (2026-05-12)

Operator-facing single-page reference for what's running RIGHT NOW
across the desktop and VPS after the Track 1–5 cutover.

## What changed today

1. **Hermes update error fixed** — VPS Hermes was in a 401 crash loop because:
   * The DeepSeek-side model name `deepseek-chat` was retired upstream
     (only `deepseek-v4-pro` and `deepseek-v4-flash` are available now).
   * The credential pool was using `env:DEEPSEEK_API_KEY` template-source
     instead of the literal key value, so it was sending the literal
     template string `${env:DEEPSEEK_API_KEY}` as the bearer token.
   * Config had `provider: custom`, which made Hermes fall through to
     `OPENAI_API_KEY` env var and send an OpenAI bearer to DeepSeek.

   All three are fixed. The cure recipe is now baked into the template
   at `deploy/hermes_vps_config.yaml` and the postinstall script.

2. **5 brain/OS tracks live** — see "What's running" below.

3. **12 future tracks documented** — see
   `eta_engine/docs/HERMES_BRAIN_FUTURE_TRACKS.md` for the menu with
   effort / payoff / recommended order.

## What's running

### VPS (forex-vps, `C:\EvolutionaryTradingAlgo\`)

| Component | Location / Process | Owns |
|---|---|---|
| Hermes gateway | scheduled task `ETA-Hermes-Agent` (auto-restart on failure, LogonTrigger) | API at `127.0.0.1:8642`, MCP server `jarvis`, memory store, scheduled cron tasks |
| Hermes config | `C:\Users\Administrator\.hermes\config.yaml` | Model = deepseek-v4-pro, provider = deepseek, memory.provider = holographic |
| Hermes credential pool | `C:\Users\Administrator\.hermes\state.db` | One entry: `deepseek #1 (literal)` |
| Memory store | `C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_memory_store.db` | Operator-saved facts (SQLite FTS5) |
| Trace stream | `var/eta_engine/state/jarvis_trace.jsonl` (when supervisor runs) | Live consult records, polled by `jarvis_subscribe_events` |
| Override sidecar | `var/eta_engine/state/hermes_overrides.json` | Operator-pinned size_modifiers + school_weights (TTL-bounded) |
| Audit log | `var/eta_engine/state/hermes_actions.jsonl` (gzip-rotated at 10MB) | Every Hermes-side tool call |
| Skills directory | `C:\Users\Administrator\.hermes\skills\jarvis-*` | 5 skills: trading, daily-review, drawdown-response, anomaly-investigator, pre-event-prep |
| Scheduled cron tasks (Hermes-side) | (Hermes-internal scheduler — NOT Windows Task Scheduler) | 3 entries: `morning_briefing` 06:30 UTC, `daily_review` 19:00 UTC weekdays, `pre_event_scanner` every 15 min |

### Desktop (`C:\Users\edwar\.hermes\`)

| Component | Location / Process | Owns |
|---|---|---|
| Hermes-desktop GUI | Electron process group `hermes-agent` (4 PIDs) | User chat UI |
| SSH tunnel watcher | Startup folder `ETA-Hermes-Tunnel.lnk` → `hermes_tunnel.ps1` (PID 37168 currently, 2+ hours stable) | `ssh -L 8642:127.0.0.1:8642 forex-vps` |
| Desktop config | `~/.hermes/config.yaml` + `~/.hermes/desktop.json` (connectionMode=remote) + `~/.hermes/auth.json` | Local fallback config (in case operator ever clicks "Local mode") |
| Models picker | `~/.hermes/models.json` | 7 entries (Claude × 2, GPT-4.1, DeepSeek V4 Pro, DeepSeek V4 Flash, GPT-5 Codex, Hermes 4 405B Nous) |
| Claw3D / Hermes Office | Electron app, running | Visualization overlay on top of Hermes |

## MCP tool surface (16 tools — bumped from 11)

| Tool | Read/Write | New today? |
|---|---|---|
| `jarvis_fleet_status` | R | — |
| `jarvis_trace_tail` | R | — |
| `jarvis_wiring_audit` | R | — |
| `jarvis_portfolio_assess` | R | — |
| `jarvis_hot_weights` | R | — |
| `jarvis_upcoming_events` | R | — |
| `jarvis_kaizen_run` | R | — |
| `jarvis_deploy_strategy` | W (2-run gated) | — |
| `jarvis_retire_strategy` | W (2-run gated) | — |
| `jarvis_kill_switch` | W (confirm-phrase gated) | — |
| `jarvis_explain_verdict` | R | — |
| **`jarvis_subscribe_events`** | R (cursor-poll) | **Track 1** |
| **`jarvis_set_size_modifier`** | W (TTL-bounded, de-risk clamp) | **Track 2** |
| **`jarvis_pin_school_weight`** | W (TTL-bounded, [0–2x] clamp) | **Track 2** |
| **`jarvis_active_overrides`** | R | **Track 2** |
| **`jarvis_clear_override`** | W (manual TTL escape) | **Track 2** |

All gated on `JARVIS_MCP_TOKEN` env var (set in `hermes_secrets.bat` →
sourced by `hermes_run.bat` → inherited by python subprocess →
inherited by the MCP server stdio subprocess Hermes spawns).

## Scheduled tasks now in cron

Edit at `~/.hermes/config.yaml` on VPS → `scheduled_tasks:` block.

| Task | Cron | Delivers via | Skill called |
|---|---|---|---|
| `morning_briefing` | `30 6 * * *` (06:30 UTC daily) | telegram | (inline prompt, no skill) |
| `daily_review` | `0 19 * * 1-5` (19:00 UTC weekdays = 3pm ET) | telegram | `jarvis-daily-review` |
| `pre_event_scanner` | `*/15 * * * *` (every 15 min) | telegram (silent if no event) | `jarvis-pre-event-prep` (conditional) |

`pre_event_scanner` runs every 15 minutes but only DELIVERS when an
event with severity ≥ 2 is within 30 minutes. The other ~95 polls per
day return the single word "quiet" and Hermes suppresses delivery.
DeepSeek-V4-Pro cost for those silent polls: ~$0.50/month.

## Operator playbook

### Most common operations

| Want to ... | Open Hermes-desktop and say |
|---|---|
| Check fleet | "how's the fleet?" |
| Recent verdicts | "show me recent JARVIS verdicts" |
| Explain a loss | "why did vwap_mr_mnq lose today?" |
| Trim a bot for the session | "trim atr_breakout_mnq to 0.5x for 4 hours, reason: drawdown caution" |
| Boost/cut a school overlay | "pin MNQ momentum to 1.2x for 4h, reason: trend day" |
| See active overrides | "what overrides are pinned right now?" |
| Manual daily review | "run the daily review now" |
| Investigate anomaly | "what's wrong with eth_sage_daily? it's lost 5 in a row" |
| Pre-event prep | "FOMC prep now" |
| Save a durable fact | "remember: I prefer IBKR over Tastytrade for futures" |
| Recall something | "what do you know about my broker preferences?" |
| Emergency stop | "kill all" — must type EXACTLY |

### Operator preferences saved to memory today

Run `fact_store action=list` in Hermes to see all. As of this writing:

* `subject=operator preference predicate=prefers object="IBKR Pro for futures over Tastytrade for now" trust=0.9`

### What's NOT live (opt-in switches)

| Channel | Status | To enable |
|---|---|---|
| Telegram | ✅ live | already wired |
| Discord | ⏸ off | get bot token → add to `hermes_secrets.bat` → flip `enabled: true` in config. See `deploy/MULTICHANNEL_SETUP.md` §2. |
| Slack | ⏸ off | get bot + app-level tokens → add to `hermes_secrets.bat` → flip `enabled: true`. See §3. |
| iMessage (BlueBubbles) | ⏸ off | needs always-on Mac as bridge host. See §4. |
| Generic webhook (Claw3D push) | ⏸ off | optional — Claw3D already polls the api_server directly. |
| Inter-agent council (T9) | ⏸ off | future track |
| Voice STT/TTS (T15) | ⏸ off | future track |

## Health check

Quick verification anytime the operator wants to confirm everything is alive:

```powershell
# 1. Desktop tunnel
Test-NetConnection 127.0.0.1 -Port 8642 -InformationLevel Quiet

# 2. Hermes API health
curl http://127.0.0.1:8642/health -H "Authorization: Bearer $env:API_SERVER_KEY"

# 3. VPS gateway log tail (via SSH)
ssh forex-vps "powershell -Command Get-Content C:\EvolutionaryTradingAlgo\var\hermes_gateway.log -Tail 5"
```

Expected:
1. `True`
2. `{"status": "ok", "platform": "hermes-agent"}`
3. Latest line should be `Hermes gateway starting` (after most recent restart), no `exited` line after it.

## If something breaks

| Symptom | Likely cause | Fix |
|---|---|---|
| Hermes-desktop shows "Cannot reach remote Hermes" | Either tunnel died OR VPS gateway crashed | `Get-Process ssh` → if missing, run `~/.hermes/hermes_tunnel.ps1`. Then `ssh forex-vps "schtasks /Run /TN ETA-Hermes-Agent"`. |
| HTTP 401 from DeepSeek | Credential pool reverted to env-source | `ssh forex-vps`, then in hermes-agent venv: `python hermes auth list`. If `manual` source is missing, re-add with `hermes auth add deepseek --type api-key --api-key <key>`. |
| `jarvis_*` tools missing from Hermes | New tool added to source but gateway didn't restart | `schtasks /End` + `schtasks /Run` on ETA-Hermes-Agent. |
| Memory facts lost on restart | DB path misconfigured | Check `~/.hermes/config.yaml plugins.hermes-memory-store.db_path`. SQLite file should persist across restarts. |

## Auto-restart safety net

* VPS: `ETA-Hermes-Agent` scheduled task — `RestartOnFailure Interval=1m Count=999`. If gateway crashes, Windows auto-restarts within 1 minute.
* Desktop: `ETA-Hermes-Tunnel` Startup-folder shortcut runs `hermes_tunnel.ps1` which is a `while $true { ssh; sleep 30 }` loop. Tunnel survives ssh disconnect.
* Cross-reboot: both auto-start on user login (`LogonTrigger` + Startup folder).

## What's tracked in git vs not

| Asset | Tracked in git? | Why / Why not |
|---|---|---|
| `eta_engine/brain/jarvis_v3/*.py` | ✅ | Source code |
| `eta_engine/mcp_servers/*.py` | ✅ | Source code |
| `eta_engine/hermes_skills/jarvis-*/` | ✅ | Operator-readable skill definitions |
| `eta_engine/deploy/hermes_vps_config.yaml` | ✅ | TEMPLATE — used by postinstall.ps1 on fresh installs |
| `eta_engine/deploy/hermes_run.bat` | ✅ | Production runner |
| `eta_engine/deploy/hermes_secrets.bat` | ❌ (gitignored) | Real credentials |
| `eta_engine/deploy/hermes_secrets.example.bat` | ✅ | Template for ↑ |
| VPS `~/.hermes/config.yaml` | ❌ | Rendered from template + scheduled-task additions |
| VPS `~/.hermes/state.db` | ❌ | Credentials & cron state |
| VPS `var/eta_engine/state/hermes_memory_store.db` | ❌ | Operator memory, may contain personal context |
| Desktop `~/.hermes/*` | ❌ | Per-user state |
