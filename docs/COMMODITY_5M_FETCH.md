# Commodity 15m / 5m historical bar fetch via TWS API

> Operator runbook addendum for `eta_engine/scripts/fetch_tws_historical_bars.py`
> -- specifically for the 5 commodity contracts surfaced by the
> 2026-05-07 fleet audit: **GC, CL, NG, ZN, 6E**.

## Why this exists (2026-05-07 audit context)

The 2026-05-07 fleet audit found the **1h timeframe is too coarse** for
these 5 commodity contracts -- most commodity edges live at **15m or 5m
intraday**. Equity-index micros (MNQ/MES) and crypto micros (MBT/MET)
already have working 5m fetches via the same script; this runbook
extends the same plumbing to the commodity fleet.

The fetcher is **multi-strategy / multi-asset** by design. Adding
commodities does not make it commodity-only -- the same script still
serves MBT/MET, MNQ/MES, and any other futures the `_FUTURES_MAP`
knows about. See the parent runbook
[`MBT_MET_TWS_FETCH.md`](MBT_MET_TWS_FETCH.md) for the full
end-to-end story; this addendum just covers commodity-specific
gotchas.

## Pre-requisites

Same as the parent MBT/MET runbook plus **per-exchange data
subscription cost**:

1. **TWS or IB Gateway running on port 4002** (paper Gateway, the
   script default). Fallback ports 7497 / 4001 are tried on connect
   failure.
2. **Paper account logged in** at the same gateway the live execution
   venue uses. Client ID 11 is the script default; pass `--client-id`
   if 11 is in use by another process.
3. **Per-exchange market-data subscriptions active**:

   | Symbol | Exchange | IBKR data subscription |
   | --- | --- | --- |
   | GC, MGC | COMEX | "COMEX (NYMEX) Level 1" |
   | CL, MCL, NG | NYMEX | "COMEX (NYMEX) Level 1" |
   | ZN, ZB | CBOT | "CBOT Real-Time" (separate from CME) |
   | 6E, M6E | CME | "CME Real-Time" (already on if MNQ/MES work) |

   **CBOT is a separate per-exchange charge** -- if `ZN` returns 0 bars
   with no pacing-violation message, the operator likely needs to
   enable CBOT in IBKR Account Management. CME and COMEX (NYMEX) are
   typically already on for paper accounts.

   Cost surface: IBKR charges per-exchange data subscriptions per
   month. Verify the active subscription list at
   <https://www.interactivebrokers.com/en/index.php?f=14193>
   before launching a multi-exchange fleet fetch.

## Default command (the 540-day 5m commodity fleet)

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.fetch_tws_historical_bars `
    --symbols GC CL NG ZN 6E `
    --days 540 `
    --timeframe 5m
```

Recommended: dry-run first to verify the chunk plan and pacing
estimate without opening a TWS connection.

```powershell
python -m eta_engine.scripts.fetch_tws_historical_bars `
    --symbols GC CL NG ZN 6E --days 540 --timeframe 5m --dry-run
```

The dry-run prints `total chunks across symbols: 90` for this
configuration -- 5 symbols x 18 chunks at 30D each.

## Alternate timeframes

15m bars are an alternative if 5m is denser than the strategy needs:

```powershell
python -m eta_engine.scripts.fetch_tws_historical_bars `
    --symbols GC CL NG ZN 6E --days 540 --timeframe 15m
```

The same chunking math applies (TWS caps `15m` durationStr at 30D, so
540d / 30d = **18 chunks/symbol** = 90 total).

## Chunking math + expected runtime

* TWS caps `durationStr` at ~30 days for 5m / 15m bars in practice.
* 540 days / 30 days-per-chunk = **18 chunks per symbol**.
* 5 symbols x 18 chunks = **90 total `reqHistoricalData` calls**.
* Per-chunk pacing sleep: 10s (under TWS's 60-req/10min cap).
* 90 chunks x 10s sleep = **15 min** of pacing sleeps minimum.
* Add 1-3s per chunk for the actual TWS round-trip ->
  expected wall-time **~17-22 min end-to-end**.

If you fetch all 5 commodities in one run (recommended) the pacing
budget governs total wall-time. Splitting into smaller batches does
not save wall-time because the 60-req / 10min ceiling is per gateway,
not per invocation.

## Expected output row counts

For 540 days of 5m bars on each commodity, expect roughly:

| Symbol | Session window (ET) | Approx 5m bars / 540d |
| --- | --- | --- |
| GC | 18:00 -> 17:00 (Globex 24x5) | ~155,000 |
| CL | 18:00 -> 17:00 (24x5) | ~155,000 |
| NG | 18:00 -> 17:00 (24x5) | ~155,000 |
| ZN | 19:00 -> 17:00 (24x5) | ~145,000 (lower 5m volume than equity micros; some sessions sparse) |
| 6E | 17:00 -> 16:00 (24x5) | ~155,000 |

If a symbol returns substantially fewer than ~120,000 bars, the
likely cause is either a missing per-exchange data subscription or a
gap in the IBKR cache around weekends / contract roll periods.
Re-running the fetcher is idempotent (the merge step dedupes by
timestamp).

## Common errors

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `ZN: 0 rows fetched` with no pacing-violation messages | CBOT data subscription not enabled. | Enable "CBOT Real-Time" in IBKR Account Management; CME / COMEX subs do not cover CBOT. |
| `qualifyContracts returned nothing for 6E` | IBKR indexes Euro FX as `EUR`, not `6E`. | Already handled by the script -- `_FUTURES_MAP["6E"] = ("EUR", "CME", "USD", "125000")`. If you see this, check you have not edited the map. |
| `qualifyContracts returned nothing for GC` | Wrong exchange string. | The script uses `COMEX` (CME Group's metals child exchange), not `CME`. Don't edit. |
| `Pacing violation` warnings | More than 60 reqs in 10 min. | Script's 60s back-off should catch up. If persistent, another process is hammering the same gateway -- pause the supervisor. |
| `clientId already in use` | clientId 11 collision. | Pass `--client-id 12` (or any free positive int). |

## Validation after a fetch

Row count + first/last timestamp sanity check:

```powershell
python -c "import csv,datetime as dt; `
  rows=list(csv.DictReader(open('mnq_data/history/GC1_5m.csv'))); `
  print(f'{len(rows)} bars, ' + `
        f'{dt.datetime.fromtimestamp(int(rows[0][\"time\"]))} -> ' + `
        f'{dt.datetime.fromtimestamp(int(rows[-1][\"time\"]))}')"
```

Repeat for `CL1_5m.csv`, `NG1_5m.csv`, `ZN1_5m.csv`, `6E1_5m.csv`.

For 540d of 5m commodity bars, expect **~120,000 to 155,000 rows**
per symbol depending on per-symbol session coverage. ZN trends
lower; the others should land in the 150-155K range.

## Front-month resolution

All 5 commodities resolve via the same
`qualifyContracts -> reqContractDetails` fallback pattern as
MBT/MET. CL and NG list 12+ active monthly expirations, so an
unqualified `Future(symbol="CL", exchange="NYMEX")` returns []
from `qualifyContracts` -- the same ambiguity case that triggers
the fallback for crypto micros. The script handles this
transparently; the operator does not need to specify an explicit
expiry. See `_resolve_front_month_via_details` in the script for
the soonest-non-expired pick logic.

## Tests

```powershell
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m pytest tests/test_fetch_tws_historical_bars.py -q
```

The commodity test additions (parametrized over GC / CL / NG / ZN
/ 6E) verify:

* All 5 symbols are present in `_FUTURES_MAP`.
* Each has the correct IBKR exchange string (COMEX / NYMEX / CBOT
  / CME) and currency / multiplier.
* Each runs end-to-end against a mocked `IB()` and writes its
  canonical CSV.
* The front-month fallback path resolves for each commodity.

No live TWS connection needed; the suite mocks `ib_insync.IB()`.
