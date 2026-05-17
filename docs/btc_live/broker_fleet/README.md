# BTC Broker Fleet Snapshots

This folder contains checked-in historical broker-fleet snapshots and lane
state examples.

These files are not the authoritative active fleet surface.

The canonical live runtime fleet state lives under:

- `var/eta_engine/state/broker_fleet/`

Ledger and per-worker state used by the current runtime should be read from the
canonical runtime path above, not from these checked-in snapshots.
