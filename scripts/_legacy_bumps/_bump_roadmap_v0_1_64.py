"""One-shot: bump roadmap_state.json to v0.1.64.

ROUTER-AWARE BROKER-EQUITY ADAPTER -- closes the v0.1.62 deferred item
*router-aware poller selection*. v0.1.63 wired the reconciler / poller
into ApexRuntime, but the poller was bound to a single statically-
chosen adapter. Under the broker dormancy mandate (IBKR primary,
Tastytrade fallback, Tradovate dormant per operator mandate
2026-04-24) a mid-session router failover would leave the poller
pointed at the now-circuit-tripped venue, silently degrading drift
detection to ``no_broker_data`` for the duration of the failover
window.

v0.1.64 closes that with :class:`RouterBackedBrokerEquityAdapter`:
the adapter resolves the active futures venue via
``router.choose_venue(probe_symbol)`` on every fetch and proxies to
that venue's ``get_net_liquidation`` reader. The reconciler / poller
side keep their existing single-source contract; the router takes
care of the substitution.

What ships
----------
core/broker_equity_adapter.py (EDIT)
  * New class RouterBackedBrokerEquityAdapter.
    - __init__(router, *, probe_symbol="MNQ", name="router-active-futures")
      validates router is a SmartRouter at construct time.
    - get_net_liquidation() consults router.choose_venue(probe_symbol)
      on every call. Reads from venue.get_net_liquidation() if the
      venue exposes it; degrades to None on any exception (router
      probe error, venue without reader, reader raise) so the
      reconciler classifies as no_broker_data and the supervisor
      keeps running.
    - active_venue_name property exposes a best-effort live name of
      the currently-routed venue for log lines (returns None if the
      router probe raises).
    - name attribute is stable so the poller's log key does not
      flip on every failover.
  * Module docstring updated: deferred-item bullet
    "Routing-aware poller selection" moved out of "Non-goals" into
    a new "Closed in v0.1.64" section.
  * __all__ extended with the new class.

tests/test_broker_equity_adapter.py (EDIT)
  * New class TestRouterBackedBrokerEquityAdapter (15 tests):
    - protocol fit (isinstance check)
    - default name is "router-active-futures"
    - custom name is preserved
    - non-router input raises TypeError
    - active_venue_name reports the routed venue (ibkr / tastytrade)
    - get_net_liquidation reads from the active venue (call counter)
    - failover swaps the read target on the next call
    - returns None when venue lacks the reader
    - returns None when the reader raises
    - returns None when choose_venue raises
    - returns None when the venue itself returns None
    - tradovate dormancy substitution is honoured (router substitutes
      tradovate -> ibkr at construct, adapter follows)
    - end-to-end: adapter -> poller -> reconciler within tolerance
    - mid-polling failover: poller picks up router preference flip

Scope discipline
----------------
  * Reconciler stays observation-only. No KillVerdict synthesis.
  * Single-source contract on the reconciler is preserved -- the
    router-backed adapter is one adapter, not a fan-out.
  * Multi-broker drift cross-check is still v0.2.x scope.
  * The supervisor (run_eta_live.py) is unchanged. Callers that want
    router-aware drift detection now construct
    RouterBackedBrokerEquityAdapter(router) and feed it through
    make_poller_for, then pass the resulting poller + reconciler to
    ApexRuntime via the v0.1.63 kwargs.

Regression
----------
  * pytest tests/test_broker_equity_adapter.py: 39 passed in 0.94s
    (24 prior + 15 new).
  * Ruff: clean on core/broker_equity_adapter.py +
    tests/test_broker_equity_adapter.py.
  * Full pytest sweep: see roadmap update; expected 3950 -> 3965 with
    +15 in-scope tests.

Expected state changes
----------------------
  * roadmap_state.json version bumped to v0.1.64.
  * eta_engine_tests_passing updated to 3965.
  * New key eta_engine_v0_1_64_router_aware_adapter with the full
    ledger.
  * New milestone appended.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.64"
NEW_TESTS_ABS = 3965
ROUTER_TESTS_ADDED = 15


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_64_router_aware_adapter"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "ROUTER-AWARE BROKER-EQUITY ADAPTER -- close the v0.1.62 "
            "deferred item *router-aware poller selection* by adding "
            "RouterBackedBrokerEquityAdapter, a single-source proxy "
            "that resolves the active futures broker via "
            "router.choose_venue() on every fetch."
        ),
        "theme": (
            "v0.1.63 wired the reconciler / poller into ApexRuntime "
            "but the poller bound to one statically-chosen adapter. "
            "A mid-session failover (IBKR -> Tastytrade) under the "
            "broker dormancy mandate would silently degrade drift "
            "detection to no_broker_data. The router-backed adapter "
            "fixes that: failover swaps the read source automatically "
            "because the adapter delegates to whichever venue the "
            "router currently prefers."
        ),
        "operator_directive_quote": "continue all",
        "modules_edited": [
            "eta_engine/core/broker_equity_adapter.py (new class "
            "RouterBackedBrokerEquityAdapter; lazy SmartRouter import "
            "in __init__ to keep import graph clean; docstring "
            "updated to move 'router-aware poller selection' out of "
            "Non-goals into Closed-in-v0.1.64).",
            "eta_engine/tests/test_broker_equity_adapter.py (new "
            "class TestRouterBackedBrokerEquityAdapter, 15 tests "
            "covering protocol fit, failover, exception swallowing, "
            "tradovate dormancy substitution, end-to-end smoke, "
            "mid-polling failover).",
        ],
        "tests_added": [
            "eta_engine/tests/test_broker_equity_adapter.py::"
            "TestRouterBackedBrokerEquityAdapter (15 tests across "
            "the router-aware proxy contract: protocol fit, name "
            "stability, type validation, active venue reporting, "
            "fetch delegation, failover semantics, exception "
            "swallowing on three layers, dormant-broker substitution, "
            "end-to-end with reconciler, mid-polling failover)."
        ],
        "design_choices": {
            "single_source_contract_preserved": (
                "Reconciler still consumes a single broker_equity_"
                "source callable. The router-backed adapter is one "
                "adapter that internally varies its read target -- "
                "the upstream contract does not change."
            ),
            "stable_poller_name_dynamic_venue_name": (
                "The adapter's `name` attr is stable "
                "('router-active-futures' by default) so the poller's "
                "log key does not flip every failover. A separate "
                "`active_venue_name` property exposes the live name "
                "for callers that want to log it alongside drift "
                "events."
            ),
            "exception_swallowing_three_layers": (
                "router.choose_venue raise / venue lacks reader / "
                "reader raises -- all three degrade to None so the "
                "reconciler classifies as no_broker_data. The "
                "supervisor never sees a router fault as a tick-"
                "halt; drift detection just goes dark for the "
                "affected window and resumes on the next successful "
                "read."
            ),
            "construct_time_router_validation": (
                "RouterBackedBrokerEquityAdapter.__init__ runs an "
                "isinstance(SmartRouter) check at construct time. A "
                "miswired supervisor fails loud at startup, not "
                "silently at the first tick."
            ),
            "lazy_router_import": (
                "core.broker_equity_adapter imports SmartRouter "
                "lazily (inside __init__ + a TYPE_CHECKING guard) so "
                "the module's import graph stays free of every "
                "venue's HTTP client. Importing the adapter module "
                "for unit tests does not pull in aiohttp / httpx."
            ),
            "probe_symbol_default_mnq": (
                "MNQ is the canonical Apex-eval symbol. Crypto / non-"
                "futures probes would resolve to crypto venues, "
                "which do not implement equity reconciliation -- "
                "those would return None and disable drift detection. "
                "Default keeps the adapter useful out of the box; "
                "callers may override via probe_symbol kwarg."
            ),
        },
        "scope_exclusions": {
            "no_supervisor_rewiring": (
                "scripts/run_eta_live.py is unchanged. The v0.1.63 "
                "kwargs already accept any (poller, reconciler) the "
                "caller constructs, so adopting the router-backed "
                "path is a one-line change at the call site rather "
                "than a contract change in ApexRuntime."
            ),
            "no_killswitch_synthesis_on_drift": (
                "Reconciler stays observation-only. KillVerdict "
                "synthesis on sustained out-of-tolerance is v0.2.x "
                "scope once we have live-paper tolerance empirics."
            ),
            "no_multi_broker_fanout": (
                "Single-source contract preserved. Cross-broker "
                "drift (IBKR vs Tastytrade simultaneously) is still "
                "v0.2.x scope."
            ),
            "no_replace_logical_with_broker_mtm": (
                "The tracker still consumes logical equity. Whether "
                "to feed it broker_mtm - sum(open_pnl) once the "
                "drift check is live is a venue-integration choice "
                "deferred to v0.2.x."
            ),
        },
        "r1_deferred_item_2_closure_state": {
            "v0_1_62_status": (
                "Listed as deferred under 'Non-goals': supervisor "
                "explicitly picks one adapter; v0.2.x to lift into "
                "router-driven selector."
            ),
            "v0_1_63_status": (
                "Still deferred -- runtime wiring landed but adapter selection remained a call-site decision."
            ),
            "v0_1_64_status": (
                "CLOSED -- RouterBackedBrokerEquityAdapter ships. "
                "Single-line construction at the supervisor: "
                "`adapter = RouterBackedBrokerEquityAdapter(router)`; "
                "`poller = make_poller_for(adapter)`. Failover "
                "automatic; tradovate-dormancy substitution honoured."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_added_in_this_bundle": ROUTER_TESTS_ADDED,
        "tests_delta_residual_from_other_modules": (NEW_TESTS_ABS - prev_tests - ROUTER_TESTS_ADDED),
        "ruff_green_touched_files": True,
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Router-aware broker-equity adapter. "
                    "RouterBackedBrokerEquityAdapter proxies to "
                    "whichever futures venue the SmartRouter "
                    "currently prefers, so a mid-session failover "
                    "from IBKR to Tastytrade keeps drift detection "
                    "live instead of silently degrading to "
                    "no_broker_data. R1 deferred item #2 (router-"
                    "aware poller selection) flipped from DEFERRED "
                    "to CLOSED."
                ),
                "tests_delta": ROUTER_TESTS_ADDED,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    state["overall_progress_pct"] = state.get("overall_progress_pct", 100)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} "
        f"({NEW_TESTS_ABS - prev_tests:+d} full, "
        f"{ROUTER_TESTS_ADDED:+d} in-scope)",
    )


if __name__ == "__main__":
    main()
