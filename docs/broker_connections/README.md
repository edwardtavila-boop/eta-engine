# Broker Connection Sweep

This folder holds the operator-facing broker/exchange connection artifacts.

## Purpose

- Probe configured brokers without placing trades.
- Report supported venues as `READY`, `STUBBED`, or `DEGRADED`.
- Report unsupported broker names explicitly as `UNAVAILABLE`.
- Keep a timestamped JSON artifact plus a `*_latest.json` snapshot for automation.

## Primary entry point

- Script: [`scripts/connect_brokers.py`](../../scripts/connect_brokers.py)

## Common usage

Run these from the repo root:

```powershell
python scripts/connect_brokers.py
python scripts/connect_brokers.py --brokers ibkr tastytrade bybit okx
python scripts/connect_brokers.py --config .\config.json --json
```

> **Dormancy note (2026-04-24):** Tradovate is currently DORMANT (funding-blocked).
> The active futures brokers are **IBKR (primary) + Tastytrade (fallback)**. The
> sweep will still probe `tradovate` if you pass it explicitly, but it is
> deliberately excluded from the default broker list. Single-point-of-truth for
> re-enablement: `apex_predator/venues/router.py` `DORMANT_BROKERS` frozenset.

## Output

- Default report directory: `docs/broker_connections/`
- Latest JSON snapshot: `broker_connections_latest.json`
- Timestamped artifact: `broker_connections_YYYYMMDDTHHMMSSZ.json`

## Config shapes

The sweep reads the configured broker list from these fields when present:

- top-level `brokers`
- top-level `venues`
- `execution.brokers`
- `execution.futures.broker_primary` / `broker_backup`
- `execution.futures.broker_backups`
- `execution.crypto.exchange_primary` / `exchange_backups`

## Notes

- Missing credentials do not fail the sweep; they are reported as `STUBBED`.
- A real connectivity failure on a supported venue is reported as `FAILED`.
- Tastytrade and IBKR are paper-gated adapters. Missing credentials report as
  `STUBBED`; ready paper configuration reports as `READY`.
- If the venue exists but the adapter has not been implemented yet, the sweep reports `UNAVAILABLE`.
