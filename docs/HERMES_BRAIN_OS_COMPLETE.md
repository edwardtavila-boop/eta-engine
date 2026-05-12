# HERMES BRAIN-OS — Complete (Zeus Supercharge)

The full 17-track + Zeus Supercharge capstone reference. After this
build, the operator's Hermes-JARVIS bridge is a complete brain/OS for
the trading framework:

* **17 specialized tracks** (lenses), each polished, tested, deployed.
* **30 MCP tools** spanning read + write + analytics + coordination.
* **12 skills** giving Hermes a domain-fluent vocabulary.
* **Zeus** — one tool, one snapshot, the operator's command center.

## The 17 tracks

| # | Track | What it gives the operator | Live? |
|---|---|---|---|
| **T1** | Real-time event stream | Cursor-poll any of 7 JSONL streams via Hermes ("subscribe me to consults > 3R") | ✅ |
| **T2** | Write-back overrides | Pin a size_modifier on a bot or pin a school_weight overlay, with TTL auto-expire | ✅ |
| **T3** | Persistent operator memory | Holographic SQLite FTS5 fact store on the VPS — Hermes remembers preferences across sessions | ✅ |
| **T4** | Custom skills | 4 workflow skills (daily-review, drawdown-response, anomaly-investigator, pre-event-prep) | ✅ |
| **T5** | Multi-channel | Templates + setup docs for Discord / Slack / iMessage / generic webhook | ✅ template |
| **T6** | Causal layer | Marginal-effect attribution — "which schools mattered for THIS verdict" | ✅ |
| **T7** | Replay & counterfactual | Re-execute past consults with hypothetical overrides — "what if I had pinned X" | ✅ |
| **T8** | Regime classifier | Rule-based regime detection + 6 pre-defined override packs | ✅ |
| **T9** | Multi-agent council | 3-pass advocate/skeptic/judge for high-stakes decisions (kill, retire, deep trim) | ✅ skill |
| **T10** | Trade narrator + journal | Deterministic 1-line-per-consult journal + weekly LLM synthesis | ✅ |
| **T11** | Adversarial inspector | Devil's-advocate that argues the OPPOSITE verdict using the same evidence | ✅ skill |
| **T12** | Attribution cube | Multi-dim slice-and-aggregate (school × asset × hour × verdict × bot) | ✅ |
| **T13** | Kelly optimizer | Per-bot fractional-Kelly sizing recommendations with drawdown penalty | ✅ |
| **T14** | Inter-agent bus | Multi-Claude-Code-session coordination with resource locks + heartbeats | ✅ |
| **T15** | Voice + wake word | Desktop "Hey JARVIS" → whisper STT → Hermes → SAPI/Piper TTS | ✅ script |
| **T16** | Sentiment overlay | LunarCrush + macro sentiment → JARVIS-readable feature cache | ✅ |
| **T17** | Live risk topology | Force-directed node-link graph of the fleet for Claw3D | ✅ |
| **⚡ Zeus** | **Unified command center** | ONE tool that snapshots every track in one call | ✅ |

## MCP tool surface (30 tools)

```
─── Read ────────────────────────────────
jarvis_fleet_status         (existing)
jarvis_trace_tail           (existing)
jarvis_wiring_audit         (existing)
jarvis_hot_weights          (existing)
jarvis_upcoming_events      (existing)
jarvis_explain_verdict      (existing)
jarvis_portfolio_assess     (existing)
jarvis_subscribe_events     (T1)
jarvis_active_overrides     (T2)
jarvis_topology             (T17)
jarvis_list_agents          (T14)
jarvis_explain_consult_causal  (T6)
jarvis_attribution_cube     (T12)
jarvis_current_regime       (T8)
jarvis_list_regime_packs    (T8)
jarvis_kelly_recommend      (T13)
jarvis_zeus                 (⚡ Zeus)

─── Write (TTL-bounded, no enforcement) ──
jarvis_set_size_modifier    (T2, TTL 240m, [0,1] de-risk only)
jarvis_pin_school_weight    (T2, TTL 240m, [0,2])
jarvis_clear_override       (T2, manual escape hatch)
jarvis_replay_consult       (T7, pure compute — no live writes)
jarvis_counterfactual       (T7, pure compute)
jarvis_apply_regime_pack    (T8, fans out via T2 surfaces)
jarvis_register_agent       (T14, presence declaration)
jarvis_acquire_lock         (T14, coordination)
jarvis_release_lock         (T14, coordination)

─── Destructive (token + 2-run gate / confirm-phrase) ──
jarvis_kaizen_run           (existing, read-only flag hardcoded)
jarvis_deploy_strategy      (existing, 2-run gate)
jarvis_retire_strategy      (existing, 2-run gate)
jarvis_kill_switch          (existing, requires "kill all" phrase)
```

## 12 skills installed on VPS

```
jarvis-trading             Bridge / core context
jarvis-daily-review        End-of-session synthesis (T4)
jarvis-drawdown-response   -3R auto-response (T4)
jarvis-anomaly-investigator  5-loss cluster diagnosis (T4)
jarvis-pre-event-prep      FOMC/CPI 30-min brief (T4)
jarvis-trade-narrator      Weekly narrative synthesis (T10)
jarvis-adversarial-inspector  Devil's advocate (T11)
jarvis-council             3-voice high-stakes decision (T9)
jarvis-sentiment-overlay   LunarCrush poll + render (T16)
jarvis-topology            Fleet-graph view (T17)
jarvis-bus                 Multi-agent coordination (T14)
jarvis-zeus                ⚡ UNIFIED COMMAND CENTER (Zeus)
```

## Scheduled tasks (Hermes cron)

```
morning_briefing        06:30 UTC daily          → telegram
daily_review            19:00 UTC weekdays       → telegram
pre_event_scanner       */15 min                 → telegram (silent if no event)
zeus_briefing           (suggested 09:30 ET)     → telegram (wire when desired)
```

## Operator's morning workflow (post-Zeus)

```
06:30 UTC — morning_briefing cron fires (auto)
            "{n} bots active, {tier_counts}, no dark modules, top
            elite atr_breakout_mnq +X.X today"

08:30 ET — operator opens Hermes-desktop
            Says: "zeus"
              Hermes → calls jarvis_zeus
              Returns: full snapshot
              Renders: 9-section operator-friendly brief
              Reading time: ~15 seconds

08:35 ET — operator decides what to act on
            * If regime detected → "apply the calm_trend pack" → council → confirm
            * If anomaly visible in attribution → "anomaly investigator on bot_X"
            * If kelly recommends a change → review + manual override
            * Otherwise → no action, fleet runs itself

15:00 ET — daily_review cron fires (auto)
            "{N} consults today, top contributor +X.XR, anomalies: ..."

20:00 UTC Sunday — weekly_review (if wired)
            7-day narrative synthesis from the trade journal
```

## Live state (as of cutover)

```
✓ VPS Hermes Agent running on forex-vps, 127.0.0.1:8642
✓ Model: deepseek-v4-pro
✓ Memory: holographic SQLite at var/eta_engine/state/hermes_memory_store.db
✓ Audit log: var/eta_engine/state/hermes_actions.jsonl (gzip-rotated at 10MB)
✓ Override sidecar: var/eta_engine/state/hermes_overrides.json (TTL-expiring)
✓ Agent registry: var/eta_engine/state/agent_registry.json (lock TTL 10m)
✓ Sentiment cache: var/eta_engine/state/sentiment/*.json (60-min staleness gate)
✓ Trade journal: var/eta_engine/state/trade_journal/YYYY-MM-DD.md (T10)
✓ Memory backup: ETA-Hermes-Memory-Backup Windows task (nightly 04:00 UTC, 14-day retention)
✓ SSH tunnel watcher: desktop Startup-folder shortcut → hermes_tunnel.ps1
✓ Auto-restart: scheduled tasks with RestartOnFailure 1m × 999
```

## Test footprint

```
205 tests across 17 test files. Run subset for any track:

  test_trace_emitter.py        (T1 schema, read_since, rotation)
  test_jarvis_mcp_server.py    (30 tool registry + dispatch + audit)
  test_hermes_overrides.py     (T2 sidecar + portfolio_brain integration)
  test_hermes_memory_backup.py (memory store snapshot + integrity)
  test_hermes_bridge_health.py (9-layer health check)
  test_trade_narrator.py       (T10 deterministic narration)
  test_sentiment_overlay.py    (T16 cache + staleness gate)
  test_risk_topology.py        (T17 graph builder)
  test_agent_registry.py       (T14 lock state machine)
  test_causal_attribution.py   (T6 marginal-effect math)
  test_consult_replay.py       (T7 replay + counterfactual)
  test_attribution_cube.py     (T12 slice + aggregate)
  test_regime_classifier.py    (T8 decision ladder + pack apply)
  test_kelly_optimizer.py      (T13 Kelly math + drawdown penalty)
  test_zeus.py                 (Zeus unified snapshot + cache)

All 205 pass; pytest exit 0.
```

## How to operate when something breaks

```
1. Run health check:
   python -m eta_engine.scripts.hermes_bridge_health

2. If a layer FAILs:
   * tunnel/gateway → restart ETA-Hermes-Agent task on VPS
   * auth → check ~/.hermes/state.db credential pool (hermes auth list)
   * llm → check DeepSeek upstream + key validity
   * jarvis_mcp → restart Hermes (re-spawns MCP stdio subprocess)
   * memory → check holographic plugin status; restart Hermes
   * overrides/audit/memory_db → check file permissions + disk space

3. Memory store ate it:
   * Stop Hermes
   * Copy a recent backup from var/eta_engine/state/backups/hermes_memory/ to
     hermes_memory_store.db
   * Restart Hermes
```

## What's NOT in scope (intentional boundaries)

| Out of scope | Why |
|---|---|
| Live capital broker routing through Hermes | LLM latency (200-3000ms) is incompatible with fill-sensitive strategies. Hermes is the brain; the supervisor + bots are the hands. |
| Real-time consolidator re-execution in T6/T7 | v1 uses a surrogate cascade. Adequate for marginal-effect attribution; v2 swap is a separate track if needed. |
| Auto-apply override packs | Operator confirms every regime-pack apply. Auto-application requires more calibration data first. |
| Cross-machine multi-agent bus | Single-VPS only. Cross-machine coordination introduces clock-skew + network-partition complexity for marginal benefit. |
| RLHF / fine-tuning on operator decisions | Sample size too small (~hundreds of decisions); would fit noise. Memory + heuristics are the right primitives until 1000+ labeled decisions accumulate. |

## What to do next

The brain is built. The operator's path forward:

```
This week (May 12-15):
  * Run paper-soak review on the 12-bot soak (separate from Hermes work)
  * Pick the SINGLE best bot for live cutover ~May 15
  * Wire live broker routing (IBKR Pro per operator preference)
  * Use jarvis_kelly_recommend to set initial sizing on the chosen bot

Once live:
  * Daily: "zeus" first thing → 15-second situational awareness
  * Anything anomalous → drill into the specific skill
  * Weekly Sunday: jarvis-trade-narrator weekly_review

Month 2 candidates (only if friction surfaces):
  * Schema v2 emit-site population (currently empty until consults populate v2 fields)
  * Real-cascade T6/T7 (replaces surrogate)
  * Inter-agent bus enforcement (currently soft-coordination only)
  * Voice + Claw3D end-to-end (currently set up; needs operator-side install)
```

## Acknowledgments

Every track honors the operator's three immovable principles:

1. **JARVIS owns the policy.** Hermes is the interface, never the
   decision authority for live trading.
2. **Operator confirms every destructive action.** No tool fires
   write-back or destructive surfaces without explicit assent.
3. **Cost-aware.** Every LLM call counts. Bulk processing is
   deterministic; LLM is reserved for narration + reasoning.

Zeus is the capstone that makes all 17 tracks usable together without
the operator having to remember which lens answers which question.
