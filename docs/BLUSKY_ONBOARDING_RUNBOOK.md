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

## Step 2: Obtain CID + APP_SECRET (BluSky-aware path)

> **DORMANCY:** Step 2 enumerates ways to acquire Tradovate API
> credentials. None of the sub-paths below activate Tradovate by
> itself — `DORMANT_BROKERS` still lists Tradovate. Activation
> happens only at Step 5 (paired code+docs commit).

> **2026-05-08 discovery — the prior version of this step assumed
> self-service app registration was visible inside the BluSky-issued
> sub-account UI. It is not.** A walk-through of trader.tradovate.com
> while logged in as `BSKELAUNCHEDWARD15586` (BluSky Launch 50K eval,
> $50,000 demo equity) confirmed the following:
>
> - Settings → **Accounts** — eval-account properties, risk settings only.
> - Settings → **Subscriptions** — Market Data products only (CME
>   Bundle Top of Book $12/mo, Depth of Market $48/mo, etc.). A search
>   for `API` returns only the `API3/USD` crypto symbol, not API Access.
> - Settings → **Your Profile** — `Customer Applications` panel shows
>   only **Demo** (with `Open Live Account` upsell). This is the gate.
> - Settings → **Security & Privacy** — 2FA, Privacy Mode, Trusted
>   Devices. No OAuth/authorized-apps section.
> - Settings → **Add-Ons** — TradingView, Order Flow+, Market Replay,
>   Extended Tick Data History, Point and Figure, Relative Volume,
>   Tick Stream. **No API Access add-on.**
>
> Tradovate's public docs state API registration requires a **LIVE
> account with ≥ $1,000 equity** plus an **API Access subscription**.
> The BluSky-issued sub-account runs in Tradovate **demo** during the
> eval phase, which is why none of the self-service registration UI
> is exposed today.

The Tradovate API still needs an "app" registration to issue OAuth2
tokens. Below are the three known paths to obtain the **CID + APP_SECRET**
that Steps 3-4 require. Run them in the order listed.

### Path A — Ask BluSky support (start here, ~1 business day)

Most prop firms hold a B2B arrangement with Tradovate that exposes a
documented API-access procedure for funded traders. Send the BluSky
support address (in the welcome email) the following:

> **Subject:** API access for BluSky Launch 50K eval — automated
> trading via Tradovate
>
> Hi BluSky team,
>
> I'm Edward Avila, just purchased the **Launch 50K Tradovate eval**
> on 2026-05-08 (sub-account **BSKELAUNCHEDWARD15586**). I plan to
> run a fully automated strategy via the Tradovate REST/WebSocket
> API — which your TOS confirms is allowed on Launch plans.
>
> The Tradovate trader UI for my Demo eval sub-account doesn't
> expose the standard "Apps" / API registration page (since
> Tradovate's docs require a Live retail account with $1k equity
> for self-service registration).
>
> Two questions:
> 1. Do you provide CID + APP_SECRET credentials directly to
>    funded traders, or
> 2. Should I register the dev app on a separate personal
>    Tradovate Live account and use those credentials to
>    authenticate against my BluSky sub-account?
>
> Either path is fine — just want to know your standard procedure
> so I follow your TOS correctly.
>
> Thanks,
> Edward Avila

While waiting on the reply the supervisor keeps running on IBKR/Tasty
futures paper exactly as today; Alpaca/spot lanes stay cellared while
we prepare the prop lane. Nothing changes on the runtime side.

### Path B — Personal Tradovate Live account ($1k of your own capital)

> **DORMANCY:** acquiring CID + APP_SECRET via a personal Live
> account does not move Tradovate out of `DORMANT_BROKERS` either.
> Per `dormancy_mandate.md` Appendix A, only the Step 5 paired
> code+docs commit does that. Capturing credentials here is purely
> preparation.

Tradovate dev apps are registered to **users**, not to specific
trading accounts, and OAuth2's CID + APP_SECRET identify the **app**
while the username + password identify the **user being authenticated**.
A single registered app can OAuth-authenticate any Tradovate user,
including BluSky-issued sub-accounts.

So once BluSky confirms third-party apps are TOS-compliant for
funded traders (Path A response), Path B becomes the fastest road:

1. Open a personal Tradovate Live retail account at
   https://trader.tradovate.com (separate from the BluSky-issued
   sub-account login).
2. Deposit **$1,000** of your own capital. This is a brokerage
   deposit, not a fee — it stays in your personal account and is
   withdrawable.
3. Subscribe to API Access on the personal account (the docs imply
   it's a paid subscription line item that's only visible once the
   equity threshold is met).
4. Register the dev app inside the personal account UI:
   - **App Name:** `EtaEngine` (must match `TRADOVATE_APP_ID` in env)
   - **App Version:** `1.0`
   - **Description:** "ETA automated trading"
5. Capture **CID** (numeric) and **App Secret** (one-time-visible
   long alphanumeric string) into the password manager.
6. In Step 3 below, pair these credentials with the **BluSky-issued
   username + password** — the resulting token is scoped to the
   BluSky sub-account.

**Risk:** Tradovate's `$1k LIVE account` wording is ambiguous about
whether the requirement applies to the **app registrant** (one-time
barrier, fine) or to the **user being authenticated** (would block
the BluSky demo sub-account). Standard OAuth2 reading is the former,
but Tradovate is both broker and API platform and may enforce the
latter. **Step 4 (`authorize_tradovate.py --demo`) is the moment of
truth** — if it returns `Authorization: OK`, Path B works; if it
returns "API access not enabled for this account," Path B is dead
and the operator escalates back to Path A or falls back to Path C.

### Path C — Wait until eval pass → funded (5-7 weeks per phase)

The funded phase runs on Tradovate **live**, and live accounts with
the funded balance should auto-expose the API registration UI.
**Catch-22 warning:** reaching funded requires passing the eval and
the buffer, both of which require placing trades. Without API
access during the eval phase, the only way to pass is to mirror
supervisor signals into the trader UI by hand — which defeats the
automation thesis and exposes the eval DD to manual-execution
errors.

Path C is only acceptable as a fallback if Paths A and B both fail.

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
    account_alias: blusky_launch_50k_phase1
```

The `blusky_launch_50k_phase1` profile is intentionally tighter than
the firm limit while we gather soak data:

- Account label: `BSKELAUNCHEDWARD15586`
- Starting balance: `$50,000`
- Drawdown floor: `$48,000` (`$2,000` total room)
- Target balance: `$53,000` (`$3,000` profit target)
- Internal daily loss cap: `$500`
- Internal liquidation buffer: `$300`
- Max new-order bracket risk: `$100`
- Consistency guard: stop new entries when realized day profit is within
  `$150` of `55%` of the `$3,000` target-profit day cap.

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
both `blusky_launch_50k_phase1` and `elite_25k_static` as accounts.

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

**Done as of 2026-05-08:**
- [x] BluSky welcome email received; credentials saved
- [x] Logged into trader.tradovate.com as `BSKELAUNCHEDWARD15586`
      (eval sub-account, $50,000 demo equity confirmed)
- [x] Portal rules encoded for Phase 1: `$48,000` max-drawdown floor,
      `$53,000` target balance, `55%` consistency guard, and conservative
      ETA paper-soak caps before any Tradovate route is activated
- [x] Walked every Settings tab to confirm API registration UI is
      gated behind a `LIVE` account upgrade

**Next operator action — send the Path A email to BluSky support:**
- [ ] Step 2 Path A: paste the support-email draft above into a
      reply to the BluSky welcome thread (or their listed support
      address) and wait ~1 business day for the API-access answer

**On BluSky reply confirming third-party apps are TOS-OK:**
- [ ] Step 2 Path B: open a personal Tradovate Live account, fund
      with $1,000, subscribe to API Access, register the `EtaEngine`
      app, capture **CID + APP_SECRET** into password manager
- [ ] Step 3: `setup_tradovate_secrets.py` — pair personal-account
      CID + APP_SECRET with the BluSky-issued username + password
- [ ] Step 4: `authorize_tradovate.py --demo` — verify OAuth flow
      against the BluSky sub-account; success here proves Path B
      works
- [ ] Step 5: paired code+docs un-dormancy commit
- [ ] Step 6: monitor first 10 live fills

Total time once Path A reply arrives: **~30 minutes plus the time
to open + fund the personal Tradovate Live account.** The supervisor
keeps running on IBKR/Tasty futures paper throughout, with Alpaca/spot
lanes hidden in the cellar; nothing changes on the runtime side until
Step 5 lands.
