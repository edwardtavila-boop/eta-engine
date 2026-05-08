# Prop-firm + small-capital live cutover plan

**Status:** 2026-05-08. Operator's Day-7 target: take 1-2 perfected strategies live with $1-2k personal OR $50-150k prop-funded eval.

---

## DORMANCY REACTIVATION CONTEXT

> **Important:** Tradovate remains DORMANT under the standing 2026-04-24
> broker dormancy mandate (`memory/broker_dormancy_mandate.md`). This
> document is the un-dormancy procedure for ONE specific path: routing
> the prop-firm-funded futures lane through Tradovate when the operator
> explicitly authorizes the reactivation.
>
> **All Tradovate references below describe what un-dormancy looks like.**
> They do NOT activate Tradovate by their presence. Activation requires
> the explicit operator-authorized steps in section (c): credentials
> wired, `bot_broker_routing.yaml` updated, `TRADOVATE_ENV=live` set.
> Until those steps land in a paired code+docs commit, Tradovate stays
> in `DORMANT_BROKERS`.
>
> See `dormancy_mandate.md` Appendix A for the canonical un-dormancy
> procedure that this plan refines for the prop-fund use case.

---

## Recommendation: Apex Trader Funding via Tradovate API (un-dormancy candidate)

| Criterion | Apex (via Tradovate) | Topstep (via Tradovate) | Personal $1-2k |
|---|---|---|---|
| Capital base | $25-300k accounts | $50-150k accounts | $1-2k cash |
| API path | Tradovate REST + WS | Tradovate REST + WS | Brokers we already have |
| Eta integration | `eta_engine/venues/tradovate.py` (399 lines, ALREADY EXISTS) | Same | IBKR live + Alpaca live |
| Profit split | 30→90% scaling | 80% from start | 100% (your money) |
| Eval cost | $50-200/mo while testing | $50-200/mo | $0 |
| Daily loss rule | 3% of account | 3% of account | None (your discretion) |
| Trailing DD rule | 5% trailing | 4% trailing | None |
| Best for | Maximum capital leverage | Familiar UX | Full control, slow compound |

**Pick: Apex Trader Funding, $50k account, Tradovate connection.** Why:
- Lowest-risk path to ~25× capital leverage on the SAME audit-confirmed edge
- The existing `eta_engine/venues/tradovate.py` adapter is already implemented; wiring it is hours of work, not days
- Apex eval rules (3% daily, 5% trailing) are well within `volume_profile_mnq`'s audit drawdown profile
- MNQ is Apex's most-traded product — perfect alignment with our top strategy

### Second prop firm: Topstep (don't put all eggs in one basket)

Operator directive 2026-05-08: add a SECOND prop firm so an Apex
account closure / rule change / eval failure doesn't kill the
live-money path. Tradovate stays under the dormancy_mandate Appendix A
gate; the second prop reactivation lands in the same operator-
authorized commit when credentials are wired.

**Pick: Topstep $50k Trading Combine (also via Tradovate API).** Why:
- **Zero new code** — same 399-line `eta_engine/venues/tradovate.py`
  adapter reused with a different `accountId` + OAuth credential set
- **80% profit split from start** vs Apex's 30→90% scaling — Topstep
  delivers larger early payouts on the same edge
- **TIGHTER rules:** $1k daily loss (vs Apex $2,500), 4% trailing DD
  (vs Apex 5%) — passing Topstep proves the strategy at higher rigor
- **Operationally independent:** if Apex changes rules / freezes /
  fails eval, Topstep account is intact (separate creds, separate
  rules, separate UX); this is the diversification the operator asked
  for

### Dual-prop architecture summary

| | Apex 50k Static | Topstep 50k Trading Combine |
|---|---|---|
| Adapter | `tradovate.py` (un-dormancy) | Same adapter, separate instance |
| Account ID | `apex_50k_static_001` | `topstep_50k_combine_001` |
| Daily loss | $2,500 (5%) | $1,000 (2%) — TIGHTER |
| Trailing DD | $2,500 (5%) | $2,000 (4%) — TIGHTER |
| Profit split (live) | 30→90% scaling | 80% from start |
| Routing mode | broker_router replicates | (parallel order on each) |

**Implementation cost:** if Apex un-dormancy is 2-3 days, Topstep is
+1 day on top (just credential + routing config). Both prop accounts
get the SAME `volume_profile_mnq` signal; each account's daily-loss
and trailing-DD circuit breakers are enforced INDEPENDENTLY by the
supervisor's existing per-account guardrails.

---

## Strategy ranking for the live slot

Pick 1-2 strategies. Top candidates ranked by audit confidence + capital fit:

| Bot | sh_def | n | Why it's the pick |
|---|---|---|---|
| **`volume_profile_mnq`** | **+2.91** | 2916 | ONLY strict-gate pass in entire fleet. MNQ is prop-firm-native. THIS is the one. |
| `volume_profile_nq` | +2.12 | 3073 | NQ too big for personal $2k; great for prop $50k+ |
| `sol_optimized` | +0.13 | 18 | Spot crypto, fractional sizing, fits any capital. Good complement. |
| `mym_sweep_reclaim` | -0.12 | 11 | Per-trade #1 (+0.672) but fractional-contract bug at live (see below) |
| `mcl_sweep_reclaim` | -1.39 | 16 | Split-stable, +0.111. Backup option. |

**Top-2 pick: `volume_profile_mnq` (prop) + `sol_optimized` (Alpaca crypto, optional diversifier).**

---

## Apex eval rule mapping (vs strategy audit profile)

| Apex Rule (50k Static account) | volume_profile_mnq audit | Status |
|---|---|---|
| Max contracts (50k): 10 | Strategy sizes 1-2 typically | ✓ Well under |
| Daily loss limit: $2,500 (5% of 50k) | Per-trade R ≈ $50-100 (1 contract MNQ) | ✓ 25-50 trades headroom |
| Trailing threshold: 50k → 52,500 max DD | Audit max DD is fraction of equity | ✓ Strategy doesn't approach |
| Min trading days: 7 | We'll have 7 days of paper-soak | ✓ Aligns |
| End-of-day flat (no overnight): variable | Strategy is intraday | ✓ Compatible |
| Rules account: scale to 90% split after 30 days profitable | — | Goal state |

**Risk: trailing drawdown is the operative cap.** It moves up as you profit but never down. If the strategy has a bad week early, the trailing threshold gets crossed and the eval is lost. Sizing should target ≤1 contract until the buffer accumulates.

---

## (a) Live cutover prep checklist — vol_prof_mnq

Use this 7-day prep for either prop-eval or personal-money path:

### Days 1-5 (paper soak with current 12-bot pin)
- [ ] **D1-D7:** Verify supervisor stays running 24×7 on VPS (already healthy)
- [ ] **D1-D7:** Daily kaizen reports written to `var/eta_engine/state/kaizen_reports`
- [ ] **D1-D3:** Accumulate ≥30 real fills on `volume_profile_mnq` (typical 5m signal cadence ~5 fills/day)
- [ ] **D3-D5:** Compare live PnL vs audit baseline — if live > 50% of audit-projected, GREEN
- [ ] **D5-D7:** Lock entry, run final stress (overnight gap, broker reconnect, kill-switch drill)

### Days 6-7 (cutover prep)
- [ ] **Choose lane:**
  - **Prop:** Sign up Apex $50k Static, pass eval rules sim. Connect Tradovate API key.
  - **Personal:** Open IBKR live account, fund $2k, get IBKR Pro market data subscription if not already.
- [ ] **Configure live env:**
  ```
  ETA_LIVE_MONEY=1
  ETA_LIVE_ENABLED_BOTS=volume_profile_mnq
  ETA_LIVE_BUDGET_PER_BOT_USD=500       # personal: $500; prop: $5000
  ETA_LIVE_DAILY_LOSS_LIMIT_USD=200     # personal: $200; prop: $2,500
  ETA_LIVE_KILL_SWITCH=1                # auto-flatten on circuit breaker
  ```
- [ ] **Smaller bracket size:** override registry per-bot extras temporarily to half-size while live
- [ ] **Pre-cutover dry run:** 1 hour live mode with $0 to verify order routing path (should reject as expected)

### Day 8+ (live go)
- [ ] First live entry on `volume_profile_mnq`
- [ ] Operator-watched first 10 fills
- [ ] If 10 fills land clean → relax to autonomous

---

## (b) Apex / Topstep eval-rule mapping → audit drawdown

Already covered in the table above. Bottom line: **`volume_profile_mnq` audit profile fits well within Apex's 50k Static and Topstep's 50k. The trailing-DD is the binding constraint.**

Mitigate by:
1. Start with 1-contract entries even if strategy wants more (override `extras["per_bot_budget_usd"]` temporarily)
2. Never carry overnight (matches Apex Static rules)
3. Pause for 1 day after any drawdown >50% of trailing buffer

---

## (c) JARVIS commanding prop firm via Tradovate (parallel to IBKR)

> **Reminder: Tradovate is DORMANT.** This section describes the
> un-dormancy steps from `dormancy_mandate.md` Appendix A applied to
> the prop-fund use case. Code paths exist; activation requires the
> explicit operator-authorized credential + routing changes below.

### Current state of `eta_engine/venues/tradovate.py`

Already implemented (399 lines):
- ✓ `authenticate()` — OAuth2 password+app+secret flow
- ✓ `place_order()` — Market/Limit, full payload builder
- ✓ `cancel_order()` — by symbol+order_id
- ✓ `get_positions()` — REST poll
- ✓ `get_balance()` — account equity
- ✓ `bracket_order()` — OSO (one-cancels-other) parent+stop+target
- ✓ `resolve_contract()` — quarterly month-code resolution
- ✓ Demo and Live URL switching via `TRADOVATE_DEMO` / `TRADOVATE_LIVE`

### What's missing to wire it live:

1. **Credentials:** `setup_tradovate_secrets.py` exists; needs operator to enter:
   - `TRADOVATE_USER_NAME`
   - `TRADOVATE_PASSWORD`
   - `TRADOVATE_APP_ID` (from Apex's API portal)
   - `TRADOVATE_APP_VERSION`
   - `TRADOVATE_CLIENT_SECRET` (from Apex's API portal)

2. **broker_router routing entry** in `configs/bot_broker_routing.yaml`:
   ```yaml
   bots:
     volume_profile_mnq:
       venue: tradovate          # was: ibkr (paper)
       account_alias: apex_50k    # human-readable alias
   defaults:
     futures_live: tradovate      # all futures-class bots route here when live
   ```

3. **Supervisor env switches:**
   ```
   ETA_VENUE_OVERRIDE_FUTURES=tradovate   # tells broker_router to use Tradovate
   TRADOVATE_ENV=live                      # vs demo
   ```

### Dual-prop multi-account routing (Apex + Topstep replication)

> **Reminder: Tradovate stays DORMANT.** The dual-prop replication
> below is governed by the same dormancy_mandate Appendix A gate;
> activation requires the operator-authorized credential commit for
> BOTH props.

The 399-line tradovate.py adapter accepts `account_id` in
`_build_place_payload(request, account_id=N)` (line 242). Multi-account
routing is a config + adapter-instance pattern, not a code rewrite:

```yaml
# configs/bot_broker_routing.yaml — dual-prop replication block
prop_accounts:
  apex_50k_static:
    venue: tradovate
    env: live
    account_id_env: APEX_TRADOVATE_ACCOUNT_ID
    creds_env_prefix: APEX_         # APEX_TRADOVATE_USER_NAME, etc.
    daily_loss_usd: 2500
    trailing_dd_usd: 2500
  topstep_50k_combine:
    venue: tradovate
    env: live
    account_id_env: TOPSTEP_TRADOVATE_ACCOUNT_ID
    creds_env_prefix: TOPSTEP_
    daily_loss_usd: 1000
    trailing_dd_usd: 2000

bots:
  volume_profile_mnq:
    routing: replicate
    accounts: [apex_50k_static, topstep_50k_combine]
```

**`routing: replicate`** — the new broker_router mode. Per signal:
1. Compute per-account qty independently (each account has its own
   per-bot budget cap)
2. Submit order to BOTH accounts in parallel via asyncio.gather
3. If either account's daily-loss or trailing-DD circuit breaker is
   tripped, that account skips this signal — the other proceeds
4. Reconcile each account independently; broker_only positions on
   one account don't affect the other

**Failure modes handled per-account by supervisor:**
- Apex API down → Topstep keeps trading
- Apex daily-loss tripped → Apex pauses 24h; Topstep continues
- Apex eval failed (account closed) → Topstep continues; only one
  capital base lost
- Tradovate platform-wide outage → BOTH affected (this is the
  shared-API risk; mitigated only by adding a Rithmic-based prop
  later as a third venue, see below)

**Future API-diversification path** (NOT in 7-day scope): add a
Rithmic-based prop (e.g. TradeDay, Take Profit Trader) using a new
`eta_engine/venues/rithmic_live.py` adapter. Rithmic uses a different
binary protocol, so a Tradovate platform outage wouldn't take down
all three. Estimated effort: 1-2 weeks for adapter + tests + un-
dormancy review. Add to roadmap, not the Day-7 plan.

4. **TWS-equivalent watchdog** (optional but good): the `eta_engine/safety` module has health-check patterns for IBKR; mirror one for Tradovate session-token expiry (it already auto-refreshes 5min before expiry per `_token_expiring()`).

### JARVIS command surface (already in place)

The supervisor has:
- `_handle_jarvis_kill_signal()` — flatten all positions on emergency
- Per-bot circuit breaker on consecutive broker rejects
- Daily-loss circuit breaker per registry config

These work the SAME for Tradovate as they do for IBKR — the abstraction is at the `VenueBase` level. JARVIS's commands flow through the supervisor → broker_router → venue (whichever is configured). No JARVIS-side changes needed.

### Effort estimate to first Apex live trade

- Operator action (1 hour): sign up Apex, get API creds
- Code change (~2 hours): `bot_broker_routing.yaml` entry + secret wiring + smoke test in demo mode
- Test (1 day): demo-mode end-to-end verification
- Live cutover (1 hour): flip env to live, paper trail audit

Total: **2-3 days from "operator gets API keys" to "first Tradovate live fill"**, assuming 12-bot paper soak runs in parallel.

---

## Decision matrix

> **Reminder: Tradovate stays DORMANT** until the explicit code+docs
> reactivation per `dormancy_mandate.md` Appendix A. The matrix below
> compares paths; the Tradovate path requires un-dormancy authorization.

| If you want | Do this |
|---|---|
| Maximum capital leverage on one prop | Apex Static $50k + vol_prof_mnq |
| **Don't put all eggs in one basket** | **Apex 50k + Topstep 50k (dual-prop replication)** |
| Faster to "first live trade" | Personal $2k + IBKR live + vol_prof_mnq |
| Diversification with crypto | Add sol_optimized on Alpaca live |
| All four | Apex + Topstep for futures (dual replication), IBKR personal for backup, Alpaca for crypto |

**Recommended starting move (operator's "don't put all eggs in one basket"):**
Sign up BOTH Apex $50k Static AND Topstep $50k Trading Combine today.
Cost: ~$167 + $165 = **~$332/month** while in eval. Use the 7-day
paper-soak window to pass both evals (10 min trading days each, MNQ
on both). Day 8 = first real-money trades simultaneously on TWO
independent prop-funded accounts running the same audit-confirmed
edge.

The dual-prop replication delivers:
- **~2× capital exposure** on the same edge ($50k + $50k = $100k base)
- **~2× expected daily PnL** per the strategy's audit profile
- **Independent rule-violation surfaces** — losing one eval does NOT
  close the other; one prop firm freezing accounts does NOT pause the
  other; one account's daily-loss trip does NOT pause the other
- **Same Tradovate adapter codepath** (399 lines, already implemented
  under un-dormancy gate), different OAuth + accountId per prop firm

---

## Continuing strategy optimization (do this in parallel)

The current 12-bot pin keeps optimizing in paper-soak. Daily kaizen reports will:
- Track each bot's live expR_net vs audit baseline
- Flag bots where live edge ≠ audit edge (drift detection)
- Auto-retire any bot whose live performance drops below threshold for 2 consecutive days

No additional bot adds for the next 7 days. Let the existing 12 collect data.

The strategies that earn live capital after Day 7 will be the ones with both audit-derived edge AND live-fill confirmation. Anything else stays paper.
