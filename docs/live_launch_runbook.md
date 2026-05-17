# EVOLUTIONARY TRADING ALGO — Live-Tiny Launch Runbook

> **DORMANCY BANNER — 2026-04-24 (operator mandate):**
> Live futures routing defaults to **IBKR (primary) + Tastytrade (fallback)**.
> **Tradovate is DORMANT** (funding-blocked, expected unlock weeks out). The
> body of this runbook is keyed to the IBKR-primary path; the small set of
> changes that flip back when Tradovate un-dormants live in [Appendix A:
> When Tradovate Un-Dormants](#appendix-a--when-tradovate-un-dormants).
> Single-point-of-truth for re-enablement:
> `eta_engine/venues/router.py` `DORMANT_BROKERS` frozenset.

**Audience:** operator on their own account.
**Scope:** MNQ + NQ Tier-A only. Tier-B crypto (Bybit) deferred pending bootstrap pass on redesign.
**Decision gate:** this runbook assumes `docs/canonical_v1_verdict_full.md` still says "cautious MNQ only". Re-read it before each run.

---

## T-minus: what must already be true

Do NOT start this runbook unless ALL of the following are true:

1. `var/eta_engine/state/kill_log.json` exists, valid JSON, has at least one review entry.
2. `var/eta_engine/state/paper_run/paper_run_report.json` shows Tier-A (MNQ+NQ) PASS.
3. `roadmap_state.json` → `current_phase` starts with `P9`.
4. `var/eta_engine/state/decisions_v1.json` exists with all 3 required tier sections.
5. `.env.example` present with all 9 required secret names.
6. `eta_engine/configs/` contains `bybit.yaml`, `alerts.yaml`, `kill_switch.yaml`, AND every yaml for the active futures brokers (i.e. for each broker in `ACTIVE_FUTURES_VENUES` from `venues/router.py`). With the current `DORMANT_BROKERS = {"tradovate"}` mandate this is `ibkr.yaml` + `tastytrade.yaml`. `tradovate.yaml` is NOT required while the broker is dormant.
7. `eta_engine/scripts/live_supervisor.py` importable cleanly (`from eta_engine.scripts.live_supervisor import JarvisAwareRouter`).
8. Extended preflight dryrun exits GO: `python -m eta_engine.scripts.live_tiny_preflight_dryrun`.
9. Funded **IBKR primary** account with ≥ $5,000 cleared balance in the tier-A bucket (Apex evaluation or funded). Tastytrade fallback may be funded later but is not required for first live tick. Tradovate is **DORMANT** — do NOT attempt to fund it; see Appendix A for the un-dormancy procedure.
10. **IBKR Client Portal** session credentials populated via `core.secrets.SecretsManager` (see Phase 1). Tastytrade fallback credentials are recommended but not blocking. Tradovate credentials are NOT required while dormant.

If any one of the above is RED, STOP here. Go fix before proceeding.

---

## Phase 1 — Secrets & env (≈10 min)

On the trading host. Populate **IBKR (primary)** first; **Tastytrade (fallback)**
is recommended but optional for first live tick. **Tradovate is DORMANT — skip
the Tradovate block** unless Appendix A says it has un-dormanted.

Start with the redacted scaffold helper. It creates `eta_engine/.env` only
when missing, never overwrites, and reports pending key names without printing
secret values:

```bash
python -m eta_engine.scripts.operator_env_bootstrap --create --json
```

```bash
# Populate secrets via keyring (preferred) or .env file.
python - <<'PY'
from eta_engine.core.secrets import SecretsManager
sm = SecretsManager()

# --- IBKR primary (REQUIRED) ----------------------------------------
# IBKR Client Portal Gateway is the local TLS endpoint that brokers
# the Web API. Spin it up first via the IBKR distribution; then fill
# the URL + account id below. The session is browser-OAuth + cookie
# based, so we do not stash a long-lived token here -- the Gateway
# holds the session.
sm.set("IBKR_CP_BASE_URL", "https://localhost:5000/v1/api")
sm.set("IBKR_ACCOUNT_ID", "DUxxxxxxx")  # paper or live account id

# --- Tastytrade fallback (RECOMMENDED, optional) --------------------
# Tastytrade uses a session-token model. Generate a token from
# https://my.tastytrade.com -> Manage -> Sessions, or via the API
# /sessions endpoint with username + password.
sm.set("TASTY_API_BASE_URL", "https://api.tastyworks.com")
sm.set("TASTY_ACCOUNT_NUMBER", "5WTxxxxx")
sm.set("TASTY_SESSION_TOKEN", "session_token_from_tastytrade")

# --- Operator notification channel (REQUIRED) -----------------------
sm.set("TELEGRAM_BOT_TOKEN", "bot_token")
sm.set("TELEGRAM_CHAT_ID", "chat_id")

# --- Tradovate (DORMANT -- do NOT populate while dormant) -----------
# See Appendix A for the un-dormancy procedure.
print("OK")
PY

# Verify the credential gate turns GREEN:
python -m eta_engine.scripts.operator_env_bootstrap --json
python -m eta_engine.scripts.operator_action_queue --json
python -m eta_engine.scripts.live_tiny_preflight_dryrun | grep credential_probe_full
# Expect: [opt] credential_probe_full   PASS   Tier-A present (... keys) ...
```

---

## Phase 1.5 — Refresh launch data (≈5–10 min)

Before venue smoke or live-tiny launch, refresh the launch-critical futures and
context datasets from the canonical operator entrypoint:

```bash
python -m eta_engine.scripts.refresh_launch_data --json
```

Expect `ok: true` and a full step list covering:

- `MNQ`, `NQ`, and `ES` futures bars
- `DXY` 5m and 1h context bars
- `VIX` 5m and 1m context bars
- advisory optional feed refreshes for Fear & Greed sentiment and SOL on-chain history
- daily NQ extension
- inventory republish + paper-live readiness verification

If any required step returns FAIL, stop here and fix the failing fetch or
readiness gate before proceeding to broker smoke tests. Optional advisory
feed failures are reported in the JSON but do not make `ok` false; use
`--skip-optional` for a critical-only launch refresh.

For automation, read `failed_required` and `failed_optional` from the JSON
summary. `failed_required` must be empty before broker smoke tests; non-empty
`failed_optional` is advisory and should be reviewed but does not block the
paper-live gate.

After the inventory republish, inspect
`bot_coverage.resolution_summary` in
`C:\EvolutionaryTradingAlgo\var\eta_engine\state\data_inventory_latest.json`.
Proxy rows are advisory quality caveats, synthetic rows are canonical support
feeds, and any non-zero `unknown` count should be resolved before broker smoke.

Then inspect the strategy/data readiness matrix:

```bash
python -m eta_engine.scripts.bot_strategy_readiness --json
```

This merges the per-bot strategy registry, frozen baselines, and data audit
into launch lanes such as `paper_soak`, `live_preflight`, `shadow_only`,
`research`, `non_edge`, and `blocked_data`. Treat `live_preflight` as permission
to run the separate per-bot promotion preflight and broker smoke checks, not as
permission to route live capital by itself.

For dashboards and wakeup automation, publish the same view as a canonical
runtime artifact:

```bash
python -m eta_engine.scripts.bot_strategy_readiness --snapshot
```

This writes
`C:\EvolutionaryTradingAlgo\var\eta_engine\state\bot_strategy_readiness_latest.json`.
The snapshot is read-only launch evidence; it must not be used as a broker
execution switch.

JARVIS and the dashboard API now read that same artifact through framework-native
surfaces:

```bash
python -m eta_engine.scripts.jarvis_status --json
curl http://127.0.0.1:8000/api/jarvis/bot_strategy_readiness
curl http://127.0.0.1:8000/api/jarvis/bot_strategy_readiness/nq_daily_drb
python -m eta_engine.scripts.strategy_supercharge_scorecard
curl http://127.0.0.1:8000/api/jarvis/strategy_supercharge_scorecard
python -m eta_engine.scripts.strategy_supercharge_manifest
curl http://127.0.0.1:8000/api/jarvis/strategy_supercharge_manifest
python -m eta_engine.scripts.strategy_supercharge_results
curl http://127.0.0.1:8000/api/jarvis/strategy_supercharge_results
```

The JARVIS readiness payload includes `row_count`, the full machine-readable
`rows` roster, and a `rows_by_bot` index for direct per-bot lookup in addition
to the compact, priority-sorted `top_actions` list. Framework clients can
enumerate or address every bot without re-running shell commands or scraping
the raw artifact. The per-bot endpoint returns `found`, `row`, `available_bots`,
`launch_lane`, paper/live readiness booleans, and the next readiness action in
one stable object. The dashboard rollup also embeds
`bot_strategy_readiness` in `/api/dashboard`, so UI clients can show launch
lanes and next actions. The V1 Command Center renders the same feed in the
JARVIS view and the top-bar `bots` chip, so readiness posture remains visible
even when the panel itself is off-screen.

The strategy supercharge scorecard is the retune queue for the approved
`A+C then B` sequence. It writes
`C:\EvolutionaryTradingAlgo\var\eta_engine\state\strategy_supercharge_scorecard_latest.json`
and ranks `paper_soak`, `research`, data/shadow repair, and only then
`live_preflight` bots. This is advisory launch evidence only: the scorecard
does not promote a bot, change broker permissions, or make `can_live_trade`
true.

The strategy supercharge manifest converts that queue into executable,
framework-native commands. It writes
`C:\EvolutionaryTradingAlgo\var\eta_engine\state\strategy_supercharge_manifest_latest.json`
and exposes `next_batch`, `commands`, `b_later`, and `hold` through
`/api/jarvis/strategy_supercharge_manifest` and `/api/dashboard`. A+C rows emit
runtime-only research-grid/data-repair commands plus timeframe-aware
`smoke_command` variants sized to produce real walk-forward windows. B rows
remain deferred until A+C is stable; every row keeps
`safe_to_mutate_live=false` and `writes_live_routing=false`.

This is a cross-asset, multi-style supercharge queue, not an MNQ-only lane. The
manifest `scope` and `groups` fields currently cover `BTC`, `ETH`, `SOL`,
`MNQ1`, and `NQ1` across ensemble voting, sage daily/gated, compression,
crypto ORB, regime/macro confluence, ORB, DRB, and legacy confluence rows. NQ
appears in the B-later live-preflight bucket, so it may be absent from current
A+C retest results while still being part of the full strategy queue.

The strategy supercharge results collector turns runtime research-grid markdown
reports back into JSON. It writes
`C:\EvolutionaryTradingAlgo\var\eta_engine\state\strategy_supercharge_results_latest.json`
and exposes `tested`, `passed`, `failed`, `pending`, stale report references,
per-bot retest metrics, per-row `retune_plan`, and ranked `retune_queue`
entries through `/api/jarvis/strategy_supercharge_results` and
`/api/dashboard`. It loads the canonical manifest snapshot first so older
research reports cannot be mistaken for current-batch A+C evidence. The
results payload also exposes `scope` and `groups.by_symbol` /
`groups.by_strategy_kind` so framework clients can see which tickers and
strategy styles have been retested and which bot/style should be optimized
next.

Wakeup automation gets the same posture through the operator queue snapshot
and heartbeat:

```bash
python -m eta_engine.scripts.operator_queue_snapshot --json
python -m eta_engine.scripts.operator_queue_heartbeat --json --changed-only
```

Those artifacts now include `bot_strategy_readiness_status`,
`bot_strategy_blocked_data`, and `bot_strategy_paper_ready` alongside the
operator blocker summary. `/api/dashboard/diagnostics` also includes a compact
`bot_strategy_readiness` block and contract check for self-diagnostics.

Scheduled JARVIS intelligence now consumes the same posture:

```bash
python -m eta_engine.scripts.daily_premarket
python -m eta_engine.scripts.jarvis_live --max-ticks 1 --interval 1
```

`var/eta_engine/state/premarket/premarket_latest.json`,
`var/eta_engine/state/premarket/premarket_latest.txt`, and
`var/eta_engine/state/jarvis_live_health.json` include bot strategy
readiness notes/payloads so premarket and live-supervisor
automation can see the launch posture without opening the dashboard.

The JARVIS strategy supervisor heartbeat also enriches each bot row with
`strategy_readiness`, and `/api/bot-fleet` preserves that per-bot
`launch_lane` / paper-live readiness detail for operator and framework clients.
Plain `state/bots` rows now inherit the same posture from the canonical
snapshot when the supervisor heartbeat is not the source. Snapshot-only bots
also appear as `readiness_only` roster/drill-down rows before their runtime
status files exist, keeping every strategy-matrix bot discoverable by the
framework. The V1 Fleet tab renders those same fields as per-bot readiness
chips plus the next readiness action, so operators can spot paper-ready,
live-preflight, and blocked bots directly from the roster and selected-bot
drill-down.

---

## Phase 2 — Venue smoke test (≈5 min, paper account preferred)

Never point at a funded LIVE account for the first smoke test. Use the IBKR
**paper** account (account id starts with `DU`) or the Tastytrade
**cert**/sandbox endpoint where available.

```bash
python - <<'PY'
import asyncio
from eta_engine.core.secrets import SecretsManager
from eta_engine.venues.ibkr import (
    IbkrClientPortalConfig,
    IbkrClientPortalVenue,
)

async def main() -> None:
    sm = SecretsManager()
    cfg = IbkrClientPortalConfig(
        base_url=sm.get("IBKR_CP_BASE_URL") or "",
        account_id=sm.get("IBKR_ACCOUNT_ID") or "",
    )
    v = IbkrClientPortalVenue(config=cfg)
    # Auth + connectivity smoke -- expect a real net-liq number,
    # not None. Returns None when creds are missing, the Client
    # Portal Gateway is offline, or the response is malformed.
    net_liq = await v.get_net_liquidation()
    if net_liq is None:
        msg = (
            "IBKR get_net_liquidation returned None. Check that the "
            "Client Portal Gateway is running on IBKR_CP_BASE_URL, that "
            "IBKR_ACCOUNT_ID matches a logged-in account, and that "
            "the session has not timed out."
        )
        raise RuntimeError(msg)
    print(f"IBKR auth + read OK, account net-liq=${net_liq:,.2f}")

asyncio.run(main())
PY
```

If Tastytrade fallback creds are populated, run an analogous smoke:

```bash
python - <<'PY'
import asyncio
from eta_engine.core.secrets import SecretsManager
from eta_engine.venues.tastytrade import (
    TastytradeConfig,
    TastytradeVenue,
)

async def main() -> None:
    sm = SecretsManager()
    cfg = TastytradeConfig(
        base_url=sm.get("TASTY_API_BASE_URL") or "",
        account_number=sm.get("TASTY_ACCOUNT_NUMBER") or "",
        session_token=sm.get("TASTY_SESSION_TOKEN") or "",
    )
    v = TastytradeVenue(config=cfg)
    net_liq = await v.get_net_liquidation()
    if net_liq is None:
        print("Tastytrade smoke: no creds or session expired (OK if fallback unused)")
    else:
        print(f"Tastytrade auth + read OK, account net-liq=${net_liq:,.2f}")

asyncio.run(main())
PY
```

Abort criteria for the **primary (IBKR)** path: any network error, any 4xx
from the Client Portal Gateway, any `get_net_liquidation()` returns None
when creds are populated. If abort: check that the Gateway is running, the
session is still authenticated, and `IBKR_ACCOUNT_ID` matches the logged-in
account. Do NOT proceed without the primary smoke green.

For `paper_live` direct order routing, the Client Portal smoke is not enough:
IB Gateway/TWS API port `4002` must also be freshly healthy. Keep the
order-entry hold engaged while completing the visible IBKR Gateway login or
two-factor prompt, then release through the guarded command below. The guard
refuses to clear the hold if `tws_watchdog.json` is missing, stale, unhealthy,
or if the active hold reason is an unrelated operator incident.

If `ibgateway_reauth_controller` reports `status=missing_recovery_task`, do
not promote to `paper_live`: the VPS cannot start the Gateway yet. First
install/configure the canonical IB Gateway 10.46 source at
`C:\Jts\ibgateway\1046`, then repair the ETA-owned tasks and low-memory
Gateway profile:

```powershell
cd C:\EvolutionaryTradingAlgo
.\eta_engine\deploy\scripts\install_ibgateway_1046.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\eta_engine\deploy\scripts\repair_ibgateway_vps.ps1

# The helper refuses non-valid Authenticode installers unless explicitly allowed.
# Only use -AllowUnsignedInstaller after confirming the official IBKR download source.
# Audit-only mode now marks operator_action_required=true when 10.46 is absent.
.\eta_engine\deploy\scripts\install_ibgateway_1046.ps1 -Install -RepairAfterInstall

# If the guarded install stops on a non-valid Authenticode result, confirm the
# installer came from the official IBKR download source before using this override:
.\eta_engine\deploy\scripts\install_ibgateway_1046.ps1 -Install -AllowUnsignedInstaller -RepairAfterInstall
python -m eta_engine.scripts.ibgateway_reauth_controller
```

```powershell
cd C:\EvolutionaryTradingAlgo
.\eta_engine\.venv\Scripts\python.exe -m eta_engine.scripts.runtime_order_hold status --json
.\eta_engine\.venv\Scripts\python.exe -m eta_engine.scripts.tws_watchdog --handshake-attempts 2 --handshake-timeout 20

# One read-only transition check. This never clears holds or starts tasks.
.\eta_engine\.venv\Scripts\python.exe -m eta_engine.scripts.paper_live_transition_check

# If the IBKR hold is active for login/2FA, the transition payload should point
# the operator to the visible Gateway prompt before the watchdog/release steps.

# VPS visibility loop. Keeps the Command Center's transition card/cache fresh
# while OP-19 is open; it also refreshes bot_strategy_readiness_latest.json
# first so the readiness endpoint and transition card agree. It never clears holds or submits orders.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\eta_engine\deploy\scripts\register_paper_live_transition_check_task.ps1 -Start

# Public first-paint speed. Keeps /api/dashboard/diagnostics warm from the VPS
# loopback so the ops website does not pay the cold diagnostics build on load.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\eta_engine\deploy\scripts\register_dashboard_diagnostics_cache_warm_task.ps1 -Start

# Daily-loss reset receipt. Keeps daily_stop_reset_audit_latest.json fresh so
# the VPS proves whether the midnight reset cleared and whether another gate
# still blocks paper_live. It never submits, cancels, flattens, or promotes.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\eta_engine\deploy\scripts\register_daily_stop_reset_audit_task.ps1 -Start

# Dry run first. Expect status=ready_to_release only after 4002 handshake is fresh.
.\eta_engine\.venv\Scripts\python.exe -m eta_engine.scripts.ibgateway_release_guard

# Execute only after the dry run is green. This clears the IBKR-specific hold,
# enables ETA-IBGateway-Reauth, and starts broker router + JARVIS supervisor.
.\eta_engine\.venv\Scripts\python.exe -m eta_engine.scripts.ibgateway_release_guard --execute
```

Tastytrade fallback failures are non-blocking for first live tick (the
runtime will degrade to `no_broker_data` for Tastytrade and IBKR will keep
serving). They are blocking once you intentionally exercise failover.

---

## Phase 2.5 — Clock sync (≈2 min, CRITICAL)

Order timestamps are stamped locally; a > 3-second drift vs wall time
will silently cause reconciliation mismatches and can invalidate fill audits.

```bash
# Verify drift:
python -m eta_engine.scripts.live_tiny_preflight_dryrun | grep clock_drift
# Expect: [opt] clock_drift   PASS   local vs server drift 0.XXs (< 3s)

# If FAIL: on Windows, force NTP resync:
w32tm /resync /force
# On Linux:
sudo ntpdate -u time.cloudflare.com

# Re-verify. Do NOT advance until clock_drift PASS.
```

---

## Phase 3 — Kill-switch drill (LIVE assertion, ≈2 min)

On the trading host, with all credentials present but BEFORE the live loop starts:

```bash
# Drill 1: portfolio-DD trip fires FLATTEN_ALL.
python -m eta_engine.scripts.live_tiny_preflight_dryrun | grep kill_switch_drill
# Expect: [REQ] kill_switch_drill       PASS   tripped FLATTEN_ALL/CRITICAL on 50% DD (...)

# Drill 2: idempotent order ID dedups.
python -m eta_engine.scripts.live_tiny_preflight_dryrun | grep idempotent_order_id
# Expect: [REQ] idempotent_order_id     PASS   identical reqs -> same coid=...
```

Both must be PASS. If either fails, STOP.

---

## Phase 4 — First live-tiny session (30–60 min watched)

**Position size: 1 MNQ contract maximum. NO NQ. NO Tier-B.** This session is a safety probe.

```bash
# Start the supervised live loop (foreground — DO NOT nohup):
python -m eta_engine.scripts.run_eta_live --live-tiny --tier-a-only --paper-assert-off
```

### Operator checklist during the session

- [ ] First 5 minutes: no trades placed, just bar ingestion + feature pipeline. If trades fire in <5 min, suspect misconfig — abort.
- [ ] Confirm first heartbeat Telegram (within 60s of start).
- [ ] Watch `logs/eta_engine/alerts_log.jsonl` — a `runtime_start` event must appear with `payload.live=True`.
- [ ] First order (if any) must appear in the **active broker's** account UI (IBKR Trader Workstation / Client Portal web for IBKR primary; Tastytrade web for the fallback) within 2s of local submission. If no UI ack, local clock drift or transport break — hit kill switch.
- [ ] After first filled trade: run `python eta_engine/scripts/_trade_journal_reconcile.py --hours 1` — expect GREEN.
- [ ] After 30 min: hit Ctrl-C (graceful stop). Confirm `runtime_stop` event appears in alerts_log.

### Abort triggers (stop immediately)

- Any 5xx from the active futures broker (IBKR Client Portal Gateway or Tastytrade API).
- Local account equity shows discrepancy vs internal equity tracker > $50.
- Kill-switch yaml shows any verdict other than CONTINUE in first 5 min.
- An order fills at a price more than 2 ticks worse than last-seen bid/ask (slippage sanity).
- Telegram alerts stop for >2 min while loop is running.

---

## Phase 5 — 48-hour soak (unsupervised)

Only proceed to Phase 5 if Phase 4 completed clean with at least one filled round-trip.

```bash
# Start under systemd or equivalent so it auto-restarts on crash:
# (assumes systemd unit apex-live.service exists pointing at run_eta_live.py)
sudo systemctl start apex-live
sudo systemctl status apex-live
```

### Daily checks (morning + evening)

1. `python eta_engine/scripts/_trade_journal_reconcile.py --hours 24` → must exit GREEN.
2. `python eta_engine/scripts/_kill_switch_drift.py --hours 24` → must exit GREEN.
3. Active broker account UI (IBKR Client Portal / Tastytrade web): compare realized PnL to internal journal. Discrepancy > $10 = stop. The R1 broker-equity drift detector should also catch this each tick (see `broker_equity` sub-key in `runtime_log.jsonl`).
4. `logs/eta_engine/alerts_log.jsonl` tail: no `kill_switch` events with severity CRITICAL.

### 48-hour exit criteria

- Session n_trades ≥ 5, n_sessions ≥ 6.
- No single day's realized PnL < -$200 (1 MNQ contract baseline).
- No reconcile RED exit in 48h.
- Internal-vs-venue equity discrepancy < 1.0% at all times.

If ALL met → qualifies for Phase 6. If ANY fails → stop, investigate, fix, restart Phase 5.

---

## Phase 6 — Scale to 2 contracts / add NQ (opt-in, ≥ 2 weeks soak)

Do NOT advance to Phase 6 before 2 calendar weeks of Phase-5 clean soak AND the adversarial validations re-run on the fresh live sample:

```bash
# Re-run walk-forward on live trade sample once it hits n_trades >= 120:
python eta_engine/scripts/walk_forward_real_bars.py \
  --overrides eta_engine/docs/overrides_p9_real_combined_v1.json \
  --weeks 4 --stride-weeks 1 --max-folds 6 --symbols mnq \
  --regime-overlay trending_only_plus_sol_ranging_flip --gate real \
  --label mnq_live_refresh

# Expect: mnq verdict STABLE (pass_rate >= 67%, mean > 0, CV < 1.0).
# If still MIXED or UNSTABLE -> HALT scaling. Stay at 1 contract.

# Re-run bootstrap on fresh live trades:
python eta_engine/scripts/bootstrap_ci_real_bars.py \
  --overrides eta_engine/docs/overrides_p9_real_combined_v1.json \
  --iterations 10000 --weeks 4 --seed 11 --symbols mnq \
  --regime-overlay trending_only_plus_sol_ranging_flip \
  --label mnq_live_refresh

# Expect: mnq CI95 lower bound > 0 (verdict CI_EXCLUDES_ZERO).
# If still MARGINAL -> HALT scaling.
```

Only after both above hit STABLE and CI-excludes-zero: advance to 2 contracts. NQ gets added only after another clean 2-week soak at that size.

---

## Emergency stop

```bash
# Gracefully stop the loop (preferred):
sudo systemctl stop apex-live

# Hard stop (if supervisor is hung):
pkill -f run_eta_live

# Manual flatten via the active broker's UI (IBKR primary):
#   IBKR Client Portal / Trader Workstation
#     -> Orders tab -> Cancel All
#     -> Positions tab -> Close All (market close)
# Tastytrade fallback (if router has failed over):
#     -> Trade tab -> Working orders -> Cancel All
#     -> Positions -> Close All (market)
# This is the last-resort button. Use if the supervisor crashed
# with open positions. Tradovate is DORMANT; ignore unless Appendix A
# says otherwise.
```

After ANY emergency stop:
1. Snapshot `logs/eta_engine/alerts_log.jsonl` to `docs/incidents/<date>.jsonl`.
2. Run `python eta_engine/scripts/_trade_journal_reconcile.py --hours 24 > docs/incidents/<date>_reconcile.txt`.
3. Append kill reason + root cause to `var/eta_engine/state/kill_log.json`.
4. Do NOT resume for at least 1 session (4h market hours). Use the pause to debug.

---

## Firm-board integration

After every live session (Phase 4 onward), run the Firm adversarial loop against the day's trades:

```bash
python eta_engine/scripts/firm_bridge.py --integrate --session today
```

Expected Firm verdict: `CONTINUE` or `ADJUST`. If `HALT` — stop live operations until the flagged finding is resolved (red team, risk, or macro veto).

---

## Post-launch review cadence

- **Daily:** reconcile + drift check (automated via cron).
- **Weekly:** run walk-forward + bootstrap refresh on accumulated live sample; compare to baseline.
- **Monthly:** strategy-generator agent review (Sonnet tier) — any drift in confluence axis effectiveness? Any need to re-tune thresholds?
- **Quarterly:** full adversarial cycle — risk-advocate, quant-researcher, devils-advocate all opus-tier. Budget the 5× cost window.

---

## Lineage

- Canonical override: `docs/overrides_p9_real_combined_v1.json`
- Canonical verdict: `docs/canonical_v1_verdict_full.md`
- Preflight entry: `eta_engine/scripts/live_tiny_preflight_dryrun.py` (14 required gates)
- Kill-switch policy: `eta_engine/core/kill_switch_runtime.py` + `configs/kill_switch.yaml`
- Order idempotency: `eta_engine/scripts/live_supervisor.py::JarvisAwareRouter._ensure_client_order_id`
- Journal reconcile: `eta_engine/scripts/_trade_journal_reconcile.py`
- Alerts: `eta_engine/obs/alert_dispatcher.py`
- Venues (active set): `eta_engine/venues/ibkr.py` (primary), `eta_engine/venues/tastytrade.py` (fallback). `eta_engine/venues/tradovate.py` ships but is DORMANT per `eta_engine/venues/router.py::DORMANT_BROKERS`.
- Router: `eta_engine/venues/router.py::SmartRouter` — substitutes any caller-supplied `preferred_futures_venue="tradovate"` with `DEFAULT_FUTURES_VENUE` (currently `"ibkr"`).
- Broker-equity drift detector: `eta_engine/core/broker_equity_reconciler.py` + `eta_engine/core/broker_equity_adapter.py::RouterBackedBrokerEquityAdapter`.

---

## Appendix A — When Tradovate Un-Dormants

Trigger: Tradovate funding clears AND the operator decides to bring the
adapter back into the active live-futures set. The dormancy mandate
(2026-04-24) is in `memory/broker_dormancy_mandate.md`; flipping out of
dormant requires the literal "unpark tradovate" / "re-enable tradovate"
operator language plus the steps below.

### A.1 Code-side flip (single source of truth)

`eta_engine/venues/router.py`:

```python
# From:
DORMANT_BROKERS: frozenset[str] = frozenset({"tradovate"})

# To:
DORMANT_BROKERS: frozenset[str] = frozenset()
```

`ACTIVE_FUTURES_VENUES` is computed from `DORMANT_BROKERS`, so flipping
the frozenset re-enables Tradovate everywhere it cares (router selection,
preflight gates, boot-banner advertised brokers).

### A.2 Re-add the Tradovate prereqs

In the **T-minus checklist** at the top of this runbook:

* Item #6 — `eta_engine/configs/` must now ALSO contain `tradovate.yaml`
  (it gets back into `ACTIVE_FUTURES_VENUES` automatically once `DORMANT_BROKERS`
  is empty).
* Item #9 — funding requirement extends to whichever futures broker is now
  the operator-selected primary. If staying IBKR-primary with Tradovate as
  a third fallback, no new funding gate; if flipping to Tradovate-primary,
  the original "≥ $5,000 cleared in the tier-A bucket" applies to Tradovate
  again.
* Item #10 — Tradovate **app credentials** (NOT user login: app_id +
  app_secret + cid issued from the Tradovate dev portal) must be
  populated in `core.secrets.SecretsManager` under the keys
  `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, `TRADOVATE_APP_ID`,
  `TRADOVATE_APP_SECRET`, `TRADOVATE_CID`.

### A.3 Tradovate secrets block (Phase 1 supplement)

```bash
python - <<'PY'
from eta_engine.core.secrets import SecretsManager
sm = SecretsManager()
sm.set("TRADOVATE_USERNAME", "your_username")
sm.set("TRADOVATE_PASSWORD", "your_password")
sm.set("TRADOVATE_APP_ID", "EtaEngine")
sm.set("TRADOVATE_APP_SECRET", "app_secret_from_tradovate_dev_portal")
sm.set("TRADOVATE_CID", "client_id_from_tradovate_dev_portal")
print("OK")
PY
```

Helper script: `python -m eta_engine.scripts.setup_tradovate_secrets`
(keyring-based; only runs on the trading host).

### A.4 Tradovate venue smoke test (DEMO endpoint only)

Never point at `TRADOVATE_LIVE` for the first smoke. Use `demo=True`.

```bash
python - <<'PY'
import asyncio
from eta_engine.core.secrets import SecretsManager
from eta_engine.venues.tradovate import TradovateVenue

async def main() -> None:
    sm = SecretsManager()
    v = TradovateVenue(
        api_key="",
        api_secret=sm.get("TRADOVATE_PASSWORD") or "",
        demo=True,
        app_id="EtaEngine",
        app_version="1.0",
        cid=sm.get("TRADOVATE_CID") or "",
        app_secret=sm.get("TRADOVATE_APP_SECRET") or "",
    )
    await v.authenticate(username=sm.get("TRADOVATE_USERNAME") or "")
    print("demo auth OK, token obtained")

asyncio.run(main())
PY
```

Abort criteria: any network error, any 4xx from Tradovate, any
`access_token` is None. Demo creds and live creds can differ — verify
which set you populated.

### A.5 Operator UI references for Tradovate

Manual flatten, when Tradovate is the active routing venue:

* Tradovate web/desktop UI -> Orders tab -> Cancel All Working
* Positions tab -> Flatten All (market close)

### A.6 Re-enable checklist (advisory)

* [ ] `DORMANT_BROKERS` flipped in `router.py`.
* [ ] `tradovate.yaml` present in `eta_engine/configs/`.
* [ ] `setup_tradovate_secrets` populated 5 Tradovate keys.
* [ ] Phase 2 demo smoke green for Tradovate.
* [ ] Preflight `live_tiny_preflight_dryrun` still 14/14 PASS with
  Tradovate back in the active set.
* [ ] `memory/broker_dormancy_mandate.md` updated to record the
  un-dormancy date + operator authorisation.

After all six are checked, Tradovate is back in the active live-futures
set. The router-aware drift detector (`RouterBackedBrokerEquityAdapter`)
will follow whichever broker `router.choose_venue("MNQ")` picks; no
additional wiring needed for drift detection to track the new active
venue.
