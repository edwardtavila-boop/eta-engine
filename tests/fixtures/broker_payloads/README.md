# Broker payload fixtures

M4 closure (Red Team v0.1.64 review).

These JSON files are sanitized, **synthetic** broker API responses used by
`tests/test_broker_payload_fixtures.py` to pin the parsing layer of the
two production venue adapters:

* `eta_engine.venues.ibkr.IbkrClientPortalVenue.get_balance` /
  `get_net_liquidation` — parses IBKR Client Portal
  `/portfolio/{account_id}/summary` responses.
* `eta_engine.venues.tastytrade.TastytradeVenue.get_balance` /
  `get_net_liquidation` — parses Tastytrade
  `/accounts/{account_number}/balances` responses.

## Why these exist

Per the Red Team review:

> The tests pin the protocol shape but not the transport correctness.
> IBKR ships a Client Portal update. `summary` returns
> `{"netLiquidation": {...}}` (camelCase) instead of
> `{"netliquidation": {...}}` (lowercase). `get_balance` line 217 looks
> for `"netliquidation"`, gets `None`, returns `{}`,
> `get_net_liquidation` returns `None` for every fetch forever. Poller's
> `fetch_none` counter climbs but `current()` keeps returning the last
> good cached value until staleness kicks in 30s later. Then
> `no_broker_data` for the rest of the session, no alerts (by design --
> `no_broker_data` does not alert), drift goes uncaught.

These fixtures + their parser tests are the regression net. A future
broker API change that drifts the field name / nesting / type will
fail the test before the operator pulls the change to live.

## What's NOT in here

* No real account IDs, real balances, or real authentication tokens.
  Every value is synthetic.
* No PII.
* No actual broker connection traces -- the fixtures are constructed
  by reading the broker's published API docs, not captured from a live
  account.

## Naming convention

`<broker>_<endpoint>_<scenario>.json`

* `<broker>`: `ibkr` or `tastytrade`
* `<endpoint>`: short name (`summary`, `balances`)
* `<scenario>`:
  - `happy` — full healthy response
  - `missing` — required field absent
  - `wrong_type` — required field present but wrong type
  - `zero` — zero balance edge case
  - `camelcase` — alternate casing the broker has used historically
  - `flat_amount` — value is a flat float instead of `{"amount": float}`

## Updating fixtures

When IBKR / Tastytrade ship a real API change:

1. Capture a sanitized response (replace account IDs, mask balances).
2. Drop it under `tests/fixtures/broker_payloads/<broker>_<endpoint>_<scenario>.json`.
3. Add a parametrize entry to `tests/test_broker_payload_fixtures.py`.
4. Update the venue parser if needed.
5. Re-run `python -m pytest tests/test_broker_payload_fixtures.py -v`.
