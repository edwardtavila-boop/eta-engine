# Continuous front-month back-fetch via TWS API

> Operator runbook addendum for `eta_engine/scripts/fetch_tws_historical_bars.py`
> -- specifically the `--back-fetch` mode that stitches multiple expired
> contracts into a single continuous front-month bar series.

## Why this exists (2026-05-07 VPS evidence)

The 2026-05-07 fleet fetcher run on the VPS exposed a hard ceiling on
how far back the legacy front-month-only fetch can reach for monthly-roll
futures:

```
HMDS query returned no data: METK6@CME (chunks 12-18 returned 0)
MET: 11923 unique bars across 18 chunks
MET: coverage 2026-03-02 -> 2026-05-07 (~66 days)
```

The legacy fetcher resolves the current front-month contract (e.g.
`METK6` = May 2026) and walks `reqHistoricalData` backward in 30-day
chunks. **TWS HMDS only has data for the SPECIFIC contract** -- chunks
older than that contract's listing date return zero bars. That caps
history at ~30-70 days for monthly-roll futures (MBT, MET, CL, MCL, NG).

`--back-fetch` solves this by **enumerating each contract that was
front-month during the requested window** and fetching its
front-month-window of bars individually, then stitching the per-contract
bar lists into a single continuous series.

## The stitch design

For each `(year, month)` in the back-window:

1. Build a `Future(symbol=root, lastTradeDateOrContractMonth="YYYYMM",
   includeExpired=True)` -- pinned to a specific contract month, not
   the ambiguous front-month query.
2. `qualifyContracts` resolves the pinned contract.
3. Plan the chunks for that contract's front-month-window (~30 days
   monthly, ~90 days quarterly).
4. `reqHistoricalData` walks the window in 30-day chunks at 5m.
5. Per-contract dedupe + sort.

After all contracts are fetched, `_stitch_continuous` concatenates
oldest -> newest, deduping any overlapping timestamps (first-wins).

### Optional back-adjustment (`--adjust`)

Each contract roll introduces a price discontinuity (the new front-month
trades at a different basis than the prior). `--adjust` walks the
stitched contracts newest -> oldest, computing the delta at each roll
boundary as `first(new).close - last(old).close`, and shifts the older
contract's full OHLC by that delta. This produces a **price-continuous**
back-adjusted series suitable for moving-average / volatility / cross-roll
backtests.

`--adjust` is implemented (not just scaffolded). The default is
**unadjusted** (raw OHLC from each contract), which is what most
backtest harnesses prefer.

## CLI

```powershell
# Legacy: front-month only (capped at ~70 days for monthly-roll futures).
python -m eta_engine.scripts.fetch_tws_historical_bars `
    --symbols MBT --days 540

# New: stitches multiple expired contracts.
python -m eta_engine.scripts.fetch_tws_historical_bars `
    --symbols MBT --days 540 --back-fetch

# New: stitches AND back-adjusts so price is continuous at each roll.
python -m eta_engine.scripts.fetch_tws_historical_bars `
    --symbols MBT --days 540 --back-fetch --adjust

# Dry-run: print per-contract enumeration without connecting.
python -m eta_engine.scripts.fetch_tws_historical_bars `
    --symbols MBT MET CL NG --days 540 --back-fetch --dry-run
```

## Roll-cadence map

The fetcher classifies each symbol's contract listing cadence in
`_ROLL_CADENCE`:

| Cadence    | Symbols                          | Months listed       |
| ---------- | -------------------------------- | ------------------- |
| `monthly`  | MBT, MET, CL, MCL, NG            | All 12              |
| `quarterly`| MNQ, NQ, ES, MES, RTY, M2K, GC,  | Mar/Jun/Sep/Dec     |
|            | MGC, ZN, ZB, 6E, M6E             |                     |

For monthly contracts, 540 days back enumerates ~18-19 contracts. For
quarterly contracts, 540 days back enumerates ~6-7 contracts.

## Pacing budget

For the realistic Bitcoin Micro back-window (540d, MBT, monthly, 5m):

* 540d / monthly listings = ~18 contracts (Nov 2024 through May 2026)
* Each monthly contract's front-month-window = 30 days = 1 chunk @ 5m
* Total chunks: ~18 contracts * 1 chunk = ~18 chunks
* Pacing: 10s sleep between chunks = ~3 minutes pacing
* Per-chunk fetch + qualifyContracts: ~3-5s = additional ~1-2 minutes
* **Estimated runtime: ~5-7 minutes** for MBT 540d
* IBKR ceiling: 60 requests / 10 minutes -- well under at <2 req/min

For the 4-symbol monthly fleet (MBT, MET, CL, NG):

* 4 symbols * ~18 chunks each = ~72 chunks
* ~12 minutes pacing + ~5 minutes fetch = **~17-20 minutes**

For quarterly (MNQ, ES, GC, etc.):

* ~6 contracts * ~3 chunks each (90d window / 30d chunks) = ~18 chunks
* ~5-7 minutes runtime per quarterly symbol

The script enforces `_PACING_SLEEP_S = 10.0` between chunks across the
entire back-fetch run (not per-contract), so the IBKR cap of
`60 req / 10 min` is structurally respected even on the worst-case
multi-symbol fleet fetch.

## CSV format (no change)

The output CSV is the same canonical format the lab harness's
`signals_*` adapters expect:

```
time,open,high,low,close,volume
1740000000,50000.0,50100.0,49900.0,50050.0,12.5
...
```

Where `time` is epoch seconds UTC. This is unchanged whether
`--back-fetch` is passed or not, and whether `--adjust` is passed or
not.

## Gotchas

1. **Contract availability**: very old contracts (5+ years) may have been
   delisted from IBKR's HMDS even with `includeExpired=True`. The
   fetcher logs `"not listed -- skipping"` and continues. For MBT/MET
   the listing history goes back to 2022; CL/NG go back further.

2. **Weekend-bordering rolls**: when a contract's expiration falls on a
   weekend, the `_last_business_day_of_month` proxy steps back to the
   prior Friday. The stitch boundary may have a 1-2 bar overlap with
   the next contract; this is handled by the first-wins dedupe in
   `_stitch_continuous`.

3. **Expiry-day partial bars**: the last day of an expiring contract
   often has thin volume / settlement-only bars. These are included
   in the stitched output; the back-adjust delta uses the actual
   `last(old).close` so the adjustment compensates if the close is
   anomalous. For backtests where this matters, consider trimming the
   last 1-2 bars of each contract before stitching (not implemented
   here -- yagni until needed).

4. **Subscription required per exchange**: the same per-exchange data
   subscriptions that the legacy fetch needs apply here. CBOT (ZN/ZB)
   is a separate IBKR subscription from CME/COMEX/NYMEX. See
   [`COMMODITY_5M_FETCH.md`](COMMODITY_5M_FETCH.md) for the per-symbol
   subscription map.

5. **`includeExpired` is mandatory**: the back-fetcher's contract objects
   set `includeExpired=True` before calling `qualifyContracts`. This is
   different from the legacy front-month-only fetcher which uses
   `includeExpired=False`. Without this flag IBKR drops expired
   contracts from the qualification response.

6. **Stitch dedupe is first-wins (older contract wins)**: when two
   contracts both report a bar at the exact same timestamp during the
   roll-window overlap, the older contract's value is kept. Most
   backtests prefer this because the older contract was the one
   actually trading at that timestamp.

## Truth surfaces

* Implementation: `scripts/fetch_tws_historical_bars.py`
* Tests: `tests/test_fetch_tws_historical_bars.py`
* Legacy front-month runbook:
  [`MBT_MET_TWS_FETCH.md`](MBT_MET_TWS_FETCH.md)
* Per-exchange subscription matrix:
  [`COMMODITY_5M_FETCH.md`](COMMODITY_5M_FETCH.md)
