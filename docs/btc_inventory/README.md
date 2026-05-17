# BTC Inventory Snapshots

This folder contains checked-in historical BTC artifact-inventory snapshots from
older audit and packaging runs.

These files are not authoritative live runtime state.

Some inventory entries still point at historical checked-in snapshot paths under
`docs/btc_live/`. Treat those references as archived evidence only, not as the
current BTC runtime contract.

Use the canonical workspace runtime surfaces instead:

- `var/eta_engine/state/btc_live/`
- `var/eta_engine/state/broker_fleet/`
- `var/eta_engine/state/btc_paper/`

If an inventory snapshot disagrees with current runtime state, trust the
canonical runtime state and treat this folder as stale historical evidence.
