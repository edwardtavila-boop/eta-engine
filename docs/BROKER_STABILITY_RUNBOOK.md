# Broker Stability Runbook

ETA broker stability is built around a single-owner rule: one machine owns the
IBKR username, and all automated recovery pauses when IBKR reports a competing
TWS/Gateway session. This prevents the VPS, home desktop, and manual logins from
knocking each other offline.

## Active Broker Policy

- IBKR is the primary futures broker and owns the Gateway API on `127.0.0.1:4002`.
- Tastytrade is the secondary futures broker and remains credential-gated.
- Tradovate stays dormant unless the operator reactivates it in code and docs together.
- No broker recovery task places, cancels, or flattens orders. Recovery is session-only.

## IBKR Gateway Stability

The canonical health files are:

- `C:\EvolutionaryTradingAlgo\var\eta_engine\state\tws_watchdog.json`
- `C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibgateway_reauth.json`

The scheduled task lane is:

- `ETA-TWS-Watchdog`: checks the socket and API handshake.
- `ETA-IBGateway-Reauth`: starts the canonical Gateway task when safe.
- `ETA-IBGateway-RunNow`: starts Gateway through IBC.
- `ETA-IBGateway-DailyRestart`: force-restart lane for the daily maintenance window.

IBC is configured with `ExistingSessionDetectedAction=primary` by default. This
makes the ETA broker host the stable primary owner once it is logged in, instead
of repeatedly overriding other sessions.

## Competing Session Response

If `ibgateway_reauth.json` reports `status=competing_session_detected`, do not
keep restarting Gateway. IBKR has seen another active TWS/Gateway login for the
same username.

Operator recovery:

1. Log out of every other IBKR TWS, Gateway, mobile, or browser trading session
   using the same username.
2. Wait one watchdog cycle.
3. Start `ETA-IBGateway-RunNow` once.
4. Confirm `tws_watchdog.json` shows `healthy=true`.

If another machine must stay logged in for observation, use a separate IBKR user
or a non-trading/secondary workflow. Do not let two automated Gateway lanes use
the same username.

## Data Capture Load Shedding

`mnq_backtest\scripts\ibkr_bbo1m_capture.py` now requires a fresh healthy
`tws_watchdog.json` before it connects. This keeps data collection from adding
extra IBKR API pressure while Gateway is down or flapping.

Manual override for diagnostics only:

```powershell
$env:IBKR_BBO_REQUIRE_HEALTHY_WATCHDOG = "0"
python C:\EvolutionaryTradingAlgo\mnq_backtest\scripts\ibkr_bbo1m_capture.py --dry-run
```

Unset the override after diagnostics.
