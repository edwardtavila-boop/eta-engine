# Phase 1 Paper-Mode Scoping

> Status: SCOPING (no code changes proposed in this doc)
> Owner: Edward Avila / Claude Code session
> Last updated: 2026-04-26

## Why this exists

PR #2..#6 closed every `v0.1.x` WIP loop and brought CI to first-time
green. The next major arc is **flipping a bot from "scaffolded but
paused" to "trading paper money against a real broker session"**. This
doc is a stake-in-the-ground checklist of what's between us and that
flip, gathered into one file so the operator can make decisions
without re-deriving the same wire-up each session.

It is intentionally narrow: paper mode for ONE bot family at a time,
not a phase-wide rollout.

## TL;DR

To flip the **MNQ engine** to paper mode against IBKR or Tastytrade you
need, in order:

1. **`.env` populated** with the broker's paper credentials.
2. **`make preflight`** passing locally (every required key present,
   venue probe green, kill_switch latch ARMED).
3. **The Firm board** running its 6-agent gate against the bot's
   spec and returning a non-KILL/non-NO_GO verdict.
4. **A bounded paper run** -- `--mode paper --bot mnq --max-bars 200`
   -- exits with rc=0, the runtime log shows ticks, no
   `kill_switch_latched` fires, and the broker_equity reconciler
   logs `within_tolerance` or `no_broker_data`.
5. **Operator sign-off**, captured as a journal line, before adding
   `--unpause`.

Everything else (NQ, crypto seed, ETH/SOL/XRP perp, staking) follows
the same recipe one bot at a time.

## What's already in place

- **Boot gate**: `core.kill_switch_latch.KillSwitchLatch` boots
  ARMED on a clean disk, fail-closes on corrupt JSON, refuses boot
  with an operator-readable reason on a TRIPPED latch.
- **Preflight checks**: `scripts/preflight.py` covers `secrets`,
  `venues`, `blackout_window`, `firm_verdict`, `tick_cadence`. The
  test in `test_preflight.py` already asserts every check returns the
  right shape.
- **Venue routing**: `venues/router.py` carries the operator-mandated
  `DORMANT_BROKERS = frozenset({"tradovate"})`. IBKR (primary) and
  Tastytrade (fallback) are the active futures lane. Bybit/OKX cover
  crypto.
- **Paused-by-default**: `main.py` and `scripts/run_apex_live.py`
  both default to `--mode paper` and require `--unpause` to remove
  the pause flag. CLI banners log `>>> BOTS STARTED IN PAUSED
  STATE.` when the flag isn't passed.
- **Alert dispatcher**: `obs.alert_dispatcher.AlertDispatcher` is
  wired into `ApexRuntime`; events route through the YAML-pinned
  registry tested by `test_alert_event_registry`.
- **Runtime log**: `scripts/run_apex_live.py` writes one JSONL line
  per tick with `kind="tick"`, including a `broker_equity` block
  via the R1 reconciler.
- **Decision journal**: `obs.decision_journal` records every
  intent/outcome pair. The dashboard (`scripts/jarvis_dashboard.py`,
  added in PR #2) snapshots this state on every `DASHBOARD_ASSEMBLE`
  tick (PR #5 wired the local handler).

## What still needs operator action

### 1. `.env` (gitignored)
`.env.example` is the canonical template. For paper mode against
IBKR + Tastytrade you specifically need:
```
TASTY_VENUE_TYPE=paper
TASTY_API_BASE_URL=https://api.cert.tastyworks.com
TASTY_ACCOUNT_NUMBER=<paper account number>
TASTY_SESSION_TOKEN=<paper session token>

IBKR_VENUE_TYPE=paper
IBKR_CP_BASE_URL=https://127.0.0.1:5000/v1/api
IBKR_ACCOUNT_ID=<paper account id>
IBKR_SYMBOL_CONID_MAP=<minimal map for MNQ>

# Operator
OPERATOR_EMAIL=...
OPERATOR_TZ=America/New_York
```
Pushover/SMTP/Twilio keys are not strictly required for paper but
preflight will warn if missing. Tradovate values can stay blank
while `DORMANT_BROKERS` includes it.

### 2. The Firm verdict
`make firm-gate SPEC=docs/firm_spec_<bot>.json` runs the 6-agent
board against the named bot spec. Default `FIRM_ROUNDTABLE_DRY_RUN=1`
in `.env` keeps it offline; setting it to `0` plus a valid
`FIRM_ROUNDTABLE_MODEL` (default `claude-sonnet-4-5`) executes the
real round-table and writes `docs/last_firm_verdict.json`. The
preflight `firm_verdict` check refuses to pass on `KILL` or `NO_GO`.

### 3. Branch protection on `main` (DONE — PR #7)
4 required checks (`ruff (production code)`, `pytest (full sweep)`,
`py3.12`, `py3.13`) are required, strict mode on, force-push off.

### 4. PAT scope hygiene (operator todo)
The harness's PAT now has `workflow` scope so the workflow file
edits in this branch could land. Recommend rotating after this
session since the token has been in chat history.

## What still needs more code

### Items already shipped after this doc landed

* **`_amain` real-router conditional** -- the active-broker creds
  check used to look at Tradovate (DORMANT). Now checks IBKR
  (primary) + Tastytrade (fallback) per the 2026-04-24 mandate.
  Tradovate creds alone no longer flip ``--live`` into the
  real-router branch; both creds-absent and creds-present branches
  unit-tested.
* **Equity-source picker for `BrokerEquityPoller`** -- already wired
  in ``_amain`` via ``_build_broker_equity_adapter()`` →
  ``make_poller_for()`` → ``BrokerEquityReconciler``. The picker
  picks IBKR → Tastytrade → fail-closed (or
  ``APEX_ALLOW_LIVE_NO_DRIFT=1`` opt-in) automatically. The earlier
  scoping note ("operator picks the source explicitly") was wrong
  about the gap; the picker has been live since v0.1.65.

### Still open

### 1. `runtime_start` -> `runtime_log.jsonl` end-to-end pinning
PR #3 fixed the alert-log pollution; PR #5 added the local-handler
dashboard snapshot. We don't yet have a CI test that asserts a
**paper-mode end-to-end run** writes the expected sequence:
`runtime_start (live=False) -> N ticks (kind=tick, broker_equity
.reason in {within_tolerance, no_broker_data}) -> runtime_stop
(bars=N)`. Adding such a test would catch regressions where a future
refactor accidentally drops the broker_equity block from a tick or
silences `runtime_start`. Estimated: ~80 LOC, one new test file.

### 2. Real `_amain` paper-mode bot wiring
`scripts/run_apex_live.py:_amain` accepts `--bot` but the binding
table currently routes paper-mode MNQ through a `MockRouter`.
Wiring the **real** IBKR/Tastytrade router behind `--mode paper +
broker creds present` is what unblocks the paper-mode flip. The
contract surface is already there
(`venues.brokers.ibkr.IBKRBroker`, `venues.brokers.tastytrade.
TastytradeBroker`); the gap is the conditional in `_amain` that
chooses between `MockRouter` and `RealRouter`. ~40 LOC.

### 3. Equity-source picker for `BrokerEquityPoller`
PR landed in v0.1.63; today the operator picks the source explicitly.
Phase 1 wants a router-aware default: when `--bot mnq` is paper,
poll IBKR; when fall-through to Tastytrade, poll Tastytrade. ~30 LOC.

### 4. `--max-runtime-seconds` budget
The bounded test run pattern (`--max-bars 200`) is good for unit
smoke; for an overnight paper soak you'd want a wall-clock budget
so the daemon exits cleanly after N hours regardless of bar
ingestion rate. Affects `scripts/run_apex_live.py` arg parser +
`ApexRuntime.run` loop. ~20 LOC.

### 5. Operator sign-off journal hook
After `--unpause`, the first tick should write a `runtime_unpaused`
event with the operator's name + timestamp into both
`docs/alerts_log.jsonl` (tracked) and the runtime log. The event
isn't in `configs/alerts.yaml` yet -- the registry test would catch
the omission and refuse to dispatch silently. Add the event +
the dispatch site.

## Order of operations

1. **`.env`** populated for IBKR + Tastytrade paper credentials.
2. **One-shot preflight** -- run `make preflight`, fix every red.
3. **`make firm-gate SPEC=docs/firm_spec_mnq.json`** -- run the
   board, capture verdict.
4. **Code: real-router wiring + max-runtime budget**
   (items 2 + 4 above; ~60 LOC, ~25 LOC of tests).
5. **Add the end-to-end paper-mode test** (item 1 above; ~80 LOC).
6. **Bounded paper run** off the operator's box, eyeball the runtime
   log + dashboard snapshot.
7. **Operator sign-off + `runtime_unpaused` event** (item 5 above).
8. **First overnight paper soak** with `--max-runtime-seconds
   28800` (8h) and SMS alert routing live.
9. **Per-bot Firm gate before adding the next bot family.**

Estimated time: 4-6 focused hours of code + tests, then a
10-day-out paper soak before any live-tiny flip.

## Out of scope (Phase 1 should NOT touch)

- **Tradovate dormancy flip** (still funding-blocked per the
  2026-04-24 mandate).
- **Crypto seed grid live trading** (paper-side first, Bybit
  testnet then mainnet paper, then mainnet live -- own arc).
- **ETH/SOL/XRP perp turbo lane** (the casino tier; gated behind
  Phase 1 success on tighter assets first).
- **Cold-wallet sweep + staking allocator** (Phase 11 territory;
  needs paper PnL streaming first).
- **AI/ML RL agent + multi-agent supervisor** (Phase 10).

## Reference paths

- Roadmap: `ROADMAP.md`
- State: `roadmap_state.json` (see scripts/_new_roadmap_bump.py)
- Onboarding: `CLAUDE.md`
- Firm spec template: `docs/firm_spec_*.json`
- Last verdict: `docs/last_firm_verdict.json`
- Preflight checks: `scripts/preflight.py`
- Runtime entry: `scripts/run_apex_live.py`
- Bot bindings: `BOT_BINDINGS` in `scripts/run_apex_live.py`
- Venue router: `venues/router.py`
- Alert YAML: `configs/alerts.yaml`
