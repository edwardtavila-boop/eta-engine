# MBT/MET (and friends) historical bar fetch via TWS API

> Operator runbook for `eta_engine/scripts/fetch_tws_historical_bars.py`.
> Pulls 540 days of 5-min OHLCV history for futures contracts through
> the same IB Gateway / TWS instance the live execution venue uses.

## Why this exists

The lab harness needs a working canonical CSV at
`mnq_data/history/{SYMBOL}1_{TF}.csv` for any contract a strategy
references. The original `fetch_mbt_met_bars.py` fetcher uses the
**Client Portal Gateway (HTTPS REST)** -- a separate process from the
TWS API gateway used at runtime. On the VPS today the Client Portal
Gateway is **not** running; the TWS API gateway on port 4002 (paper)
**is**.

This script reuses the running TWS gateway via `ib_insync` and
`ib.reqHistoricalData`, the same call pattern proven in
`feeds/bar_accumulator.py`.

Coexistence:

* `feeds/bar_accumulator.py` -- left untouched; real-time refresh path.
* `scripts/fetch_mbt_met_bars.py` -- left untouched; Client Portal path.
* `scripts/fetch_tws_historical_bars.py` -- this script; TWS API path.

## Pre-requisites

1. **TWS or IB Gateway running** on the VPS (or wherever you invoke the
   script) and **logged into the paper account**. Default port:
   - `4002` -- paper IB Gateway (script default)
   - `7497` -- paper TWS (first fallback)
   - `4001` -- live IB Gateway (second fallback)
   The script tries the primary port, then walks the fallback list.
2. **Client ID free.** The supervisor uses `ETA_IBKR_CLIENT_ID`,
   `bar_accumulator.py` uses 50 / 51, the live venue defaults to 99.
   The fetcher defaults to **clientId=11** to stay clear of all of
   them. If you see `Error 326: clientId already in use`, pass
   `--client-id` with a different positive integer.
3. **Standard CME Level 1 market-data subscription** on the account.
   Paper accounts ship with this; if you see empty payloads with no
   pacing-violation messages, double-check the subscription page in
   Account Management.
4. The fetcher writes to the canonical workspace path
   `mnq_data/history/{SYMBOL}1_{TF}.csv`. The directory will be
   created if absent.

## Default command (the 540-day MBT + MET fetch)

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.fetch_tws_historical_bars --symbols MBT MET --days 540
```

Adding `--dry-run` first is recommended -- it prints the chunk plan and
expected wall-time without opening a TWS connection:

```powershell
python -m eta_engine.scripts.fetch_tws_historical_bars --symbols MBT MET --days 540 --dry-run
```

## Reusable across the futures fleet

The fetcher is **not MBT-only**. Any symbol in the futures map
(`MNQ NQ ES MES RTY M2K MBT MET CL MCL NG GC MGC ZN ZB 6E M6E`) works:

```powershell
# Equity-index micros for the lab harness
python -m eta_engine.scripts.fetch_tws_historical_bars --symbols MNQ MES --days 540

# Phase-2 commodities expansion
python -m eta_engine.scripts.fetch_tws_historical_bars --symbols GC CL 6E --days 540 --timeframe 1h
```

Supported timeframes: `1m`, `5m`, `15m`, `1h`.

## Chunking math (for 540d x 5m)

* TWS caps `durationStr` at ~30 days for 5m bars in practice. Each
  chunk requests `"30 D"`.
* 540 days / 30 days-per-chunk = **18 chunks per symbol**.
* MBT + MET = **36 total `reqHistoricalData` calls**.
* Each chunk returns ~8,640 bars at 5m (288 bars/day x 30 days).
* Final per-symbol output: ~155,000 bars after dedupe and session
  trimming.

## Pacing safety

TWS caps historical-bar requests at **60 per 10 minutes**. The script
enforces:

* **10-second sleep between successful chunks** -> 6 req/min, well
  under the 60/10min ceiling.
* **60-second back-off** when a chunk raises a `Pacing violation`
  error before retry. The script keeps the chunk-end cursor and just
  resumes; it does NOT retry the failed chunk (logs + skips). If you
  notice gaps, re-run the script -- the merge step is idempotent.

For a 540d x 5m x 2-symbol fetch (36 chunks), pacing sleeps total
~ 36 x 10s = 6 minutes. Add ~1-3 seconds per chunk for the actual
TWS round-trip -> expected wall-time **8-12 minutes** end-to-end on
the VPS.

For a 540d x 5m x 6-symbol fleet fetch (108 chunks), pacing sleeps
total ~ 18 minutes; total wall-time **20-30 minutes**.

## Output format

CSV at `mnq_data/history/{SYMBOL}1_{TF}.csv`:

```
time,open,high,low,close,volume
1700000000,50000.0,50100.0,49900.0,50050.0,12.5
...
```

`time` is **epoch seconds (UTC)**. Schema matches
`feeds/strategy_lab/engine._load_ohlcv` so the lab harness loads the
output directly with no further conversion.

## Idempotency

Re-runs are safe. The script reads any existing CSV at the target
path, dedupes by `time` (existing rows win on duplicates), and writes
the merged superset. Pass `--no-merge` to overwrite instead.

## Common errors

| Symptom | Cause | Fix |
| --- | --- | --- |
| `could not connect to TWS API on any of [4002, 7497, 4001]` | Gateway not running, or wrong port. | Start IB Gateway (paper) and verify port 4002 is open. |
| `Error 326: clientId already in use` | Another process is using clientId 11. | Pass `--client-id 12` or whatever's free; check supervisor + bar_accumulator. |
| `qualifyContracts returned nothing for SYM` | Symbol not indexed at IB, or contract month unavailable. | Verify symbol is in `_FUTURES_MAP`. For 6E, IB indexes the standard contract under `EUR` (the script handles this). |
| `Pacing violation` warnings | More than 60 reqs in 10min -- usually because another process is hammering the same gateway. | Wait 10 minutes; the script's 60s back-off should catch up. Reduce concurrency. |
| Empty CSV / 0 rows fetched | Market-data subscription missing, or contract has no bars in the requested window. | Check the CME Level 1 subscription. For very old contracts, lower `--days`. |

## Validation after a fetch

```powershell
# Row count + first/last timestamp sanity check
python -c "import csv,datetime as dt; rows=list(csv.DictReader(open('mnq_data/history/MBT1_5m.csv'))); print(f'{len(rows)} bars, {dt.datetime.fromtimestamp(int(rows[0][chr(34)+chr(116)+chr(105)+chr(109)+chr(101)+chr(34)]))} -> {dt.datetime.fromtimestamp(int(rows[-1][chr(34)+chr(116)+chr(105)+chr(109)+chr(101)+chr(34)]))}')"
```

For 540d of 5m MBT bars, expect **~120,000 to 155,000 rows** depending
on session masking and weekend coverage.

## Tests

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m pytest tests/test_fetch_tws_historical_bars.py -q
```

The test suite mocks `ib_insync.IB()` -- no live TWS needed. CI can run
this on any machine.
