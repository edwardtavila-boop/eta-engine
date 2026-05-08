# BluSky Launch 50K — Onboarding Runbook

**Status:** Active onboarding 2026-05-08. Operator purchased BluSky Launch
50K Tradovate eval ($59). This runbook covers every step from BluSky
welcome-email arrival to first live order in BluSky's funded account.

> **DORMANCY NOTE:** Tradovate remains in `DORMANT_BROKERS` per the
> 2026-04-24 broker dormancy mandate. This runbook describes the
> un-dormancy procedure for the prop-funded futures lane. Activation
> requires the operator-authorized credential commit + routing change
> in Step 5 below; until then, Tradovate stays dormant.

---

## Pre-flight (DONE today, before BluSky email arrives)

- [x] BluSky Launch 50K Tradovate eval purchased ($59)
- [x] 399-line Tradovate adapter at `eta_engine/venues/tradovate.py`
      verified intact (40/40 unit tests pass)
- [x] `setup_tradovate_secrets.py` ready (5-credential interactive
      keyring loader)
- [x] `authorize_tradovate.py` ready (OAuth2 verifier)
- [x] `bot_broker_routing.yaml` reviewed; staged Tradovate route
      below (commented; activated in Step 5)

---

## Step 1: Wait for BluSky welcome email (today–tomorrow)

BluSky typically provisions a Tradovate sub-account within minutes
to a business day after purchase. The welcome email contains:

- **Tradovate username** (usually your email or a BluSky-assigned ID)
- **Tradovate password** (initial password — change on first login)
- **Tradovate account ID** (numeric, like `9999999`)
- **Demo / live indicator** (eval phase = Tradovate **demo** environment;
  funded phase = Tradovate **live** environment)
- **BluSky dashboard URL** for tracking eval progress

**Action:** When email arrives, save credentials in a password manager
(NOT in plain-text files). Note whether the sub-account is `demo` or
`live` — it changes the API base URL.

---

## Step 2: Register Tradovate developer app (5 min)

The Tradovate API needs an "app" registration to issue OAuth2 tokens.
This is FREE and takes 2 minutes. Independent of BluSky — your dev app
works for all your Tradovate sub-accounts.

1. Log into Tradovate web UI: https://trader.tradovate.com (use the
   credentials BluSky provided).
2. Top-right menu → **Apps** (or **Settings** → **API Access**)
3. Click **New App**:
   - **App Name:** `EtaEngine` (or whatever you like — must match
     `TRADOVATE_APP_ID` in our env)
   - **App Version:** `1.0`
   - **Description:** "ETA automated trading"
4. Click **Create**. You will see two values:
   - **CID** (Client ID — numeric, e.g. `8`)
   - **App Secret** (a long alphanumeric string)
5. Copy both into your password manager. **You can never view the
   App Secret again** after closing the page (Tradovate hashes it
   server-side).

---

## Step 3: Wire credentials into the OS keyring (1 min)

The `setup_tradovate_secrets.py` helper stores the 5 values in your
OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret
Service). Plaintext never touches disk or env vars.

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m eta_engine.scripts.setup_tradovate_secrets
```

The script prompts for 5 fields. Type each:

| Prompt | Source |
|---|---|
| `TRADOVATE_USERNAME` | BluSky email — Tradovate login |
| `TRADOVATE_PASSWORD` | BluSky email — Tradovate password |
| `TRADOVATE_APP_ID` | The app name from Step 2 (default `EtaEngine`) |
| `TRADOVATE_APP_SECRET` | App Secret from Step 2 (one-time-visible) |
| `TRADOVATE_CID` | Client ID from Step 2 (numeric) |

Verify:

```powershell
python -m eta_engine.scripts.setup_tradovate_secrets --check
```

Expected output: `5 / 5 secrets present` (or similar success line).

---

## Step 4: Smoke-test in Tradovate DEMO mode (5 min)

Before flipping any live routing, verify the adapter can authenticate
and place a test order in Tradovate's free demo environment. **This
does NOT use BluSky's eval account yet** — it uses Tradovate's public
demo API to verify the wiring works end-to-end.

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m eta_engine.scripts.authorize_tradovate --demo
```

Expected output:
- `Authorization: OK`
- `Token: ****XXXX` (last-4 of access token)
- `Endpoint: https://demo.tradovateapi.com/v1`

If this works, the adapter + credentials + dev app are all wired. If
it errors, the most common causes:
- App Secret typo (Step 2 — re-register the dev app if needed)
- CID typo (must be the numeric client ID, not the app name)
- Tradovate web UI session expired (relog and re-register)

---

## Step 5: Stage the BluSky routing config (un-dormancy commit)

**This is the un-dormancy moment.** Tradovate moves from
`DORMANT_BROKERS` to active for `volume_profile_mnq` only.

Add to `eta_engine/configs/bot_broker_routing.yaml`:

```yaml
# ── Prop-firm funded lanes (BluSky / Elite via Tradovate) ─────────
# Un-dormancy 2026-05-XX: operator-purchased BluSky Launch 50K eval.
# volume_profile_mnq (strict-gate pass) routes to Tradovate
# specifically for the BluSky funded lane. All other futures bots
# stay on IBKR paper.
bots:
  volume_profile_mnq:
    venue: tradovate
    account_alias: blusky_launch_50k
```

Update `eta_engine/venues/router.py` (or equivalent) to remove
`tradovate` from `DORMANT_BROKERS` for the `volume_profile_mnq`
specifically. Pair the code change with a docs commit per the
dormancy mandate.

Set the supervisor environment for live mode:

```
ETA_TRADOVATE_ENABLED=1
TRADOVATE_ENV=demo            # for eval phase; switch to "live" at funded
ETA_VENUE_OVERRIDE_FUTURES=ibkr  # default stays IBKR; routing yaml pins specific bot
```

Restart the supervisor on VPS.

---

## Step 6: First live order (BluSky eval phase)

Once the supervisor is running with `volume_profile_mnq → tradovate`,
the next time the strategy generates a signal:

1. Supervisor calls `tradovate.place_order(request)`
2. Adapter authenticates → places order on BluSky Tradovate sub-account
3. BluSky's Tradovate counts it toward Eval profit target
4. Strategy continues firing per its schedule (5m bars)

**Operator action for first 10 fills:**
- Watch BluSky dashboard for fills appearing
- Cross-check fill prices against supervisor log
- Verify position direction matches strategy intent
- If anything looks wrong, flip `ETA_TRADOVATE_ENABLED=0` to halt

After 10 clean fills → relax to autonomous monitoring. Daily kaizen
reports will track eval progress.

---

## Step 7: Eval pass → Buffer phase

When BluSky email confirms Eval passed:
1. New Tradovate sub-account credentials likely issued (Buffer phase)
2. Repeat Step 3 with new credentials (run `setup_tradovate_secrets`
   to overwrite)
3. Strategy continues; aim for the second 6% profit ($3,000 more)

---

## Step 8: Buffer pass → Funded

When BluSky email confirms Funded status:
1. Switch `TRADOVATE_ENV=live` (Funded uses Tradovate live API)
2. Re-run `setup_tradovate_secrets` with funded-account credentials
3. Restart supervisor
4. **First payout review:** confirm 90/10 split, BluSky pays via
   their normal payout schedule (Mon-Fri daily payouts)

---

## Step 9 (later): Add Elite 25K Static as second prop

After BluSky proves out (~Day 14-21), repeat Steps 1-5 for Elite
Trader Funding. Use a SECOND set of Tradovate credentials (Elite's
sub-account is separate from BluSky's). Update `bot_broker_routing.yaml`
to use `routing: replicate` mode for `volume_profile_mnq`, listing
both `blusky_launch_50k` and `elite_25k_static` as accounts.

---

## Failure-mode quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `Auth failed` in Step 4 | App Secret / CID typo | Re-register dev app, re-run setup_secrets |
| `Symbol not tradeable` on first order | Wrong contract month | Check `tradovate.resolve_contract()` quarterly cadence |
| `Position cap exceeded` | Strategy req > BluSky max contracts | Lower per-bot budget or qty cap |
| `Eval failed` | DD breached | Pay $47-85 reset fee, reduce sizing, retry |
| `Tradovate API error 429` | Rate limit | Adapter has built-in retry; increase delay if persistent |
| BluSky account closed | TOS violation suspected | Contact BluSky support immediately; review TOS bot policy |

---

## Cost tracking

> **DORMANCY:** purchases below activate the un-dormancy procedure
> per `dormancy_mandate.md` Appendix A; entries below describe the
> sub-account fees, not new code paths.

| Date | Item | Cost |
|---|---|---|
| 2026-05-08 | BluSky Launch 50K Tradovate eval (purchase) | $59 |
| TBD | BluSky setup fee (charged at funding) | $85 |
| Monthly | BluSky eval renewal (until passed) | $59/mo |
| TBD | Elite 25K Static (Phase 2, ~Day 14-21) | $277 + $177 |

Operator's $1-2k starting capital comfortably covers Phase 1 (BluSky
only). Add Elite when BluSky shows positive trajectory.

---

## Operator next-action

> **DORMANCY:** operator-side prep below is part of the un-dormancy
> procedure per `dormancy_mandate.md` Appendix A; nothing here
> activates Tradovate by itself — Step 5 (paired code+docs commit)
> is the activation gate.

**Right now, while waiting for BluSky email:**
- [ ] Read this runbook end-to-end
- [ ] Pull up Tradovate web UI in a browser tab (https://trader.tradovate.com)
      — bookmark it for Step 2

**When BluSky email arrives:**
- [ ] Step 1: save credentials to password manager
- [ ] Step 2: register Tradovate dev app (5 min)
- [ ] Step 3: run `setup_tradovate_secrets.py` (1 min)
- [ ] Step 4: verify with `authorize_tradovate.py --demo` (5 min)
- [ ] Step 5: staged commit (engineer pairs code+docs)
- [ ] Step 6: monitor first 10 live fills

Total time once email arrives: **~30 minutes to first live order.**
