# Tradovate API Deposit Prep

**DORMANT / un-dormancy prep only. Status:** 2026-05-09. Use this while waiting to fund the personal
Tradovate account that unlocks API access for BluSky prop testing.

This runbook is intentionally conservative. It prepares the API path but
does not route `volume_profile_mnq` or place orders until every cutover
check is green.

## Current State

- BluSky platform login is stored in the ETA keyring under the `BLUSKY_`
  prop-account prefix.
- The API fields are still missing until the Tradovate account is funded
  and API access is enabled:
  - `BLUSKY_TRADOVATE_ACCOUNT_ID`
  - `BLUSKY_TRADOVATE_APP_ID`
  - `BLUSKY_TRADOVATE_APP_SECRET`
  - `BLUSKY_TRADOVATE_CID`
- Tradovate remains safe-held unless the process explicitly sets
  `ETA_TRADOVATE_ENABLED=1`.
- `volume_profile_mnq` is not routed to Tradovate yet.

## Before Deposit

Run the no-order readiness check:

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.tradovate_prop_readiness --phase predeposit
python -m eta_engine.scripts.futures_prop_ladder
python -m eta_engine.scripts.closed_trade_ledger
python -m eta_engine.scripts.broker_bracket_audit
python -m eta_engine.scripts.prop_live_readiness_gate
python -m eta_engine.scripts.prop_operator_checklist
```

Expected pre-deposit summary:

```text
summary: READY_FOR_DEPOSIT
prop_login_credentials: PASS
prop_api_credentials: WAIT
tradovate_activation_flag: SAFE_HELD
winning_bot_route: SAFE_HELD
```

If this command is `BLOCKED`, fix the blocked item before funding.

The consolidated `prop_live_readiness_gate` is expected to stay `BLOCKED`
before funding. It is the hard go/no-go latch for the full automated prop
lane and should block until all of these are true:

- `volume_profile_mnq` is the only primary futures prop candidate cleared by
  the ladder.
- Tradovate/BluSky cutover readiness is `READY_FOR_DRY_RUN`.
- IBKR, broker-router, and paper-live surfaces are green.
- Historical failed/quarantined/rejected router residue has been resolved or
  archived.
- Broker-native bracket/OCO proof exists for open exposure.
- A schema-backed closed-trade ledger exists so win rate, PnL, and R are not
  stale.

Use `prop_operator_checklist` when you want the short operator version of
what remains. It writes
`C:\EvolutionaryTradingAlgo\var\eta_engine\state\prop_operator_checklist_latest.json`
and prints the exact commands for the remaining manual steps.

If `broker_bracket_audit` reports an open IBKR futures position that requires
manual broker-OCO verification, clear it only after checking TWS/IB Gateway
and confirming the position has a broker-native TP/SL OCO attached outside
ETA:

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.broker_bracket_audit --ack-manual-oco --symbol MNQM6 --venue ibkr --operator edward --expires-hours 24 --confirm
python -m eta_engine.scripts.broker_bracket_audit
python -m eta_engine.scripts.prop_live_readiness_gate
```

Do not use this acknowledgment as a substitute for actual broker-side
protection. If the broker OCO is not visible, flatten manually or let the gate
remain blocked.

## Deposit Day

In Tradovate:

1. Fund the personal live Tradovate account above the current API-access
   threshold.
2. Complete the CME Information License Agreement.
3. Purchase or enable the API Access add-on.
4. Register or generate the API application credentials.
5. Capture the values below without pasting them into chat:
   - numeric account ID
   - app ID/name
   - CID/client ID
   - app secret/SEC

Then store the fields locally:

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.setup_tradovate_secrets --prop-account blusky_50k
python -m eta_engine.scripts.setup_tradovate_secrets --prop-account blusky_50k --check
```

## OAuth Smoke

Run demo first if the credentials are valid for demo/sim:

```powershell
cd C:\EvolutionaryTradingAlgo
$env:ETA_TRADOVATE_ENABLED = "1"
python -m eta_engine.scripts.authorize_tradovate --prop-account blusky_50k --json
```

If Tradovate only authorizes the funded/live API environment, run:

```powershell
cd C:\EvolutionaryTradingAlgo
$env:ETA_TRADOVATE_ENABLED = "1"
python -m eta_engine.scripts.authorize_tradovate --prop-account blusky_50k --live --json
```

The required result is:

```json
{
  "credential_scope": "blusky_50k",
  "result": "AUTHORIZED"
}
```

## Cutover Readiness

After the OAuth smoke succeeds:

```powershell
cd C:\EvolutionaryTradingAlgo
$env:ETA_TRADOVATE_ENABLED = "1"
python -m eta_engine.scripts.tradovate_prop_readiness --phase cutover
python -m eta_engine.scripts.futures_prop_ladder
python -m eta_engine.scripts.closed_trade_ledger
python -m eta_engine.scripts.broker_bracket_audit
python -m eta_engine.scripts.prop_live_readiness_gate
python -m eta_engine.scripts.prop_operator_checklist
```

Required cutover summary:

```text
summary: READY_FOR_DRY_RUN
prop_api_credentials: PASS
oauth_authorization: PASS
tradovate_activation_flag: PASS
winning_bot_route: SAFE_HELD
```

`SAFE_HELD` on `winning_bot_route` is correct at this stage. It means the
system is ready for a dry-run, but the winning bot has not been connected
to Tradovate yet.

The required consolidated gate result before the first prop dry run is:

```text
summary: READY_FOR_CONTROLLED_PROP_DRY_RUN
primary_ladder: PASS
prop_readiness: PASS
broker_surfaces: PASS
router_cleanliness: PASS
broker_native_brackets: PASS
closed_trade_ledger: PASS
live_bot_gate: PASS
```

## First Dry Run

Only after `READY_FOR_DRY_RUN`:

1. Keep `volume_profile_mnq` unrouted.
2. Run broker-router dry-run against a synthetic MNQ pending order.
3. Confirm the payload uses the numeric `accountId`.
4. Confirm the route stays behind `ETA_TRADOVATE_ENABLED=1`.
5. Confirm no non-winning strategy can route to Tradovate.

## First Prop Test

Only after dry-run evidence is clean:

1. Route only `volume_profile_mnq` to `blusky_50k`.
2. Start with the smallest allowed MNQ exposure.
3. Require bracket/exit protection before every exposure.
4. Operator watches the first fills.
5. Pause immediately on auth errors, account mismatch, missing stop/target,
   daily-loss warning, or broker reconciliation mismatch.

## Official Requirements To Recheck

- Tradovate API access requirements:
  https://tradovate.zendesk.com/hc/en-us/articles/4403105829523-How-Do-I-Get-Access-to-the-Tradovate-API
- Tradovate OAuth app registration:
  https://tradovate.zendesk.com/hc/en-us/articles/4403100442515-How-Do-I-Register-an-OAuth-App
- BluSky automation language:
  https://blog.blusky.pro/blusky-blog/attention-prop-firm-traders
