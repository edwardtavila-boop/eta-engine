# MBT / MET Historical Bar Fetch — Operator Runbook

Snapshot: 2026-05-07. Owner: Edward.

## Why

The walk-forward lab harness (`feeds.strategy_lab.engine`) needs ~18
months of 5-minute OHLCV bars for **MBT** (Micro Bitcoin) and **MET**
(Micro Ether) to validate three new CME-crypto-micro strategies.
Today the canonical history root has zero coverage for these symbols.
Until this fetcher is run once, those strategies fail at the bar-load
gate.

## Pre-requisites

1. **IB Gateway / Client Portal Gateway running** at
   `https://127.0.0.1:5000/v1/api`. Start it from the local
   `clientportal.gw` install: `./bin/run.sh root/conf.yaml`.
2. **Authenticated session.** Visit `https://127.0.0.1:5000` in a
   browser and log in with your IBKR credentials. Confirm via
   `https://127.0.0.1:5000/v1/api/iserver/auth/status` →
   `{"authenticated": true}`.
3. **CME Crypto market-data subscription active** on the account
   (~$10/mo). Without it, `/marketdata/history` silently returns empty
   payloads with no error code.
4. **Paper-account login is fine** — historical-bar entitlements are
   the same as live for CME crypto micros.

## Command

From the workspace root (`C:\EvolutionaryTradingAlgo`):

```powershell
# Default: 18 months × 5m × MBT + MET → mnq_data/history/{MBT,MET}1_5m.csv
python -m eta_engine.scripts.fetch_mbt_met_bars `
    --symbols MBT MET --days 540

# Dry run first to confirm the chunk plan
python -m eta_engine.scripts.fetch_mbt_met_bars `
    --symbols MBT MET --days 540 --dry-run
```

The script is idempotent: re-running merges new bars into the
existing CSV (deduped by timestamp). Run it again whenever you need
to top up coverage to the present moment.

## Expected wall-time

5-minute bars over 540 days ≈ **155k bars per symbol**, or **~173
chunked requests per symbol** at the IBKR Client Portal ~900-bar/chunk
ceiling. With the script's 0.2s polite-sleep between chunks plus
typical request latency (~0.3-1.0s):

| Phase           | Per-chunk time | Per-symbol time |
| --------------- | -------------- | --------------- |
| Best case       | ~0.5s          | ~1.5 min        |
| Typical         | ~1.2s          | ~3.5 min        |
| Pessimistic     | ~2.5s          | ~7 min          |

**Realistic estimate: ~10-15 minutes total for both symbols.** If
the gateway throttles or the network is slow, allow up to ~30
minutes. If you exceed an hour, kill it and check the gateway log —
that's a sign of an auth/sub problem, not data volume.

## Output verification

After the run completes, confirm:

```powershell
# Files exist at the canonical lab-harness path
ls C:\EvolutionaryTradingAlgo\mnq_data\history\MBT1_5m.csv
ls C:\EvolutionaryTradingAlgo\mnq_data\history\MET1_5m.csv

# Header is correct
Get-Content C:\EvolutionaryTradingAlgo\mnq_data\history\MBT1_5m.csv -TotalCount 1
# Expected: time,open,high,low,close,volume

# Bar count is sane (155k ± session-mask losses; expect ~120k-155k)
(Get-Content C:\EvolutionaryTradingAlgo\mnq_data\history\MBT1_5m.csv | Measure-Object).Count

# Lab harness can load it
python -c "from eta_engine.feeds.strategy_lab.engine import _resolve_bar_path, _load_ohlcv; p = _resolve_bar_path('MBT', '5m'); print(p, _load_ohlcv(p)['close'].shape)"
```

The script's stdout also reports first/last coverage dates and any
intra-window gaps >2× the bar size.

## Gotchas

- **Quarterly contract rollover.** CME crypto futures expire on the
  last Friday of Mar/Jun/Sep/Dec. The script asks `/trsrv/futures`
  for the front-month conid and IBKR stitches across rolls
  internally for historical bars. Sample the merged CSV around roll
  dates if you suspect splice artifacts.
- **Conid stability.** Per-contract conids change every quarter; do
  not cache them across runs.
- **Empty payload, no error.** The Client Portal gateway returns an
  empty `data` array if the session is unauthenticated or the CME
  Crypto subscription is missing. The script reports zero rows with
  the most likely causes — check `/iserver/auth/status` and the
  market-data subscriptions screen first.
- **US-legal venues only.** This fetcher uses IBKR exclusively. Do
  not add Binance/Bybit/etc. spot-crypto fallbacks for these
  symbols — they would violate the workspace data-source policy and
  poison the lab harness with non-CME-aligned tape.
