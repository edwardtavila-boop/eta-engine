"""One-shot: bump roadmap_state.json to v0.1.62.

BROKER-EQUITY-ADAPTER PROTOCOL SCAFFOLDING -- formalises the contract
that any broker venue adapter must satisfy to drive the v0.1.59 R1
:class:`BrokerEquityReconciler` / :class:`BrokerEquityPoller` pair.

Why this bundle exists
----------------------
v0.1.59 shipped the reconciler and the async->sync poller bridge. The
two production venues that already implement the right shape
(``IbkrClientPortalVenue.get_net_liquidation`` and
``TastytradeVenue.get_net_liquidation``) satisfy the contract
*implicitly*. Implicit contracts hold up exactly as long as nobody
tries to add a third broker -- the moment Tradovate (when funding
clears) or a paper-mode stub gets wired in, there is no compile-time
or runtime gate that catches a shape mismatch.

v0.1.62 lifts that contract into a typed, runtime-checkable protocol
without changing any runtime behaviour.

What ships
----------
core/broker_equity_adapter.py (NEW)
  * ``BrokerEquityAdapter`` -- ``@runtime_checkable`` ``typing.Protocol``
    requiring ``name: str`` and ``async def get_net_liquidation()
    -> float | None``. Both production venues already match
    structurally; no inheritance change required.
  * ``NullBrokerEquityAdapter`` -- canonical no-op for paper / dry-run
    / dormant-broker. Always returns ``None``. Wiring this through the
    reconciler is equivalent to disabling drift detection.
  * ``make_poller_for(adapter, *, refresh_s, stale_after_s)`` --
    factory that runtime-checks the adapter, raises ``TypeError`` with
    a class-named message on miswiring, and returns a constructed-but-
    unstarted :class:`BrokerEquityPoller`.

tests/test_broker_equity_adapter.py (NEW, 24 tests)
  * ``TestProtocolStructuralFit`` (7 tests):
    - fixed adapter satisfies protocol
    - null adapter satisfies protocol
    - missing-name attr fails check
    - missing-method attr fails check
    - random object fails check
    - production IBKR adapter (IbkrClientPortalVenue) satisfies
    - production Tastytrade venue satisfies
  * ``TestNullBrokerEquityAdapter`` (4 tests):
    - default name == ``"null"``
    - custom name preserved
    - get_net_liquidation returns None
    - returns None repeatably
  * ``TestMakePollerFor`` (9 tests):
    - returns BrokerEquityPoller for valid adapter
    - returned poller is not running yet
    - forwards adapter.name to poller
    - forwards refresh/stale params
    - default refresh=5.0 / stale=30.0
    - TypeError on missing-name input
    - TypeError on missing-method input
    - TypeError message names offending class
    - null adapter round-trips through factory
  * ``TestEndToEndAdapterPollerReconciler`` (4 tests):
    - fixed adapter -> poller -> reconciler within tolerance
    - null adapter -> poller -> reconciler no_broker_data
    - drift above tolerance flips reconciler
    - polling loop repeatedly calls adapter (sanity)

Design choices
--------------
  * **Structural typing (Protocol), not nominal (ABC).** Forcing
    venues to inherit from a new base class would touch every
    production adapter and create a churn risk. Structural fit means
    IBKR and Tastytrade are already compliant without touching either
    file -- the contract is a *check* over their existing surface,
    not a new requirement.
  * **``@runtime_checkable``.** Lets ``isinstance(x, ...)`` work at
    runtime so the factory can fail loudly on miswiring (e.g. someone
    passing a router or a config dict). PEP 544 disclaims that the
    runtime check only verifies attribute presence, not signatures
    -- that's fine for catching obvious mistakes; signature
    enforcement stays a static-analysis / test concern.
  * **Two attrs, not more.** The protocol asks only for ``name`` and
    ``get_net_liquidation``. The reconciler does not need
    ``place_order``, ``cancel_order``, etc. Keeping the surface
    narrow makes paper / test stubs cheap to write -- a stub that
    only knows how to return a fake net-liq does not have to
    implement the entire venue surface.
  * **Null adapter, not None-handling everywhere.** Every consumer
    that wants "no broker source" can either pass
    ``NullBrokerEquityAdapter()`` or skip the wiring entirely. This
    keeps the supervisor's wiring path uniform: every code branch
    hands the reconciler a real source object; the no-data semantics
    are encoded in the source itself.
  * **Factory raises TypeError, not silently returns None.** A
    miswiring is an operator-blocking bug, not a runtime degradation.
    Failing fast at boot is better than silently producing a poller
    bound to a non-functional source.
  * **Factory does NOT auto-start the poller.** Lifecycle ownership
    stays with the caller (the supervisor); the factory is just
    construction.

Scope discipline
----------------
  * No changes to the supervisor wiring code. Whichever code path
    today picks IBKR vs Tastytrade vs nothing keeps doing so. v0.2.x
    will refactor that path to use ``make_poller_for`` directly.
  * No new behaviour in :class:`BrokerEquityReconciler` or
    :class:`BrokerEquityPoller`. They already accept the right
    callables.
  * No multi-broker fan-out. We single-account on Apex evals; multi-
    broker drift cross-checking is a future-state concern.
  * No KillVerdict synthesis on out-of-tolerance. Reconciler stays
    observation-only per its v0.1.59 docstring.

Regression
----------
  * Full pytest (excluding the pre-existing flaky
    ``test_dashboard_api.py::test_btc_lanes_empty_when_fleet_dir_absent``):
    3920 passed, 10 skipped.
  * In-scope delta vs v0.1.61 (3889 absolute): +24 new tests for the
    adapter protocol. Remaining +7 come from out-of-scope test
    additions in adjacent modules (broker_equity_poller etc.) that
    landed alongside this work.
  * Ruff: clean on both files touched.

Expected state changes
----------------------
  * ``roadmap_state.json`` version bumped to v0.1.62.
  * ``eta_engine_tests_passing`` updated to 3920.
  * New key ``eta_engine_v0_1_62_broker_equity_adapter_protocol``
    with the full ledger.
  * New milestone appended.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.62"
NEW_TESTS_ABS = 3920
PROTOCOL_TESTS_ADDED = 24


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_62_broker_equity_adapter_protocol"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "BROKER-EQUITY-ADAPTER PROTOCOL SCAFFOLDING -- formalise "
            "the v0.1.59 R1 contract via a runtime-checkable Protocol "
            "+ NullBrokerEquityAdapter stub + make_poller_for factory."
        ),
        "theme": (
            "v0.1.59 shipped the reconciler and async->sync poller, "
            "but the adapter contract was implicit. v0.1.62 lifts it "
            "to a typed Protocol so that wiring a third broker "
            "(Tradovate when funding clears, paper-mode stubs, or "
            "test fakes) becomes a structural-fit check rather than a "
            "code-archaeology exercise. No runtime behaviour changes."
        ),
        "operator_directive_quote": "continue all.",
        "modules_added": [
            "eta_engine/core/broker_equity_adapter.py "
            "(BrokerEquityAdapter Protocol + NullBrokerEquityAdapter "
            "stub + make_poller_for factory).",
        ],
        "tests_added": [
            "eta_engine/tests/test_broker_equity_adapter.py (NEW, "
            "24 tests across 4 sections: protocol structural fit, "
            "null-adapter behaviour, factory contract, end-to-end "
            "adapter->poller->reconciler smoke).",
        ],
        "design_choices": {
            "structural_protocol_not_abc": (
                "Inheritance would force every venue to subclass a "
                "new base, churn-risking the production adapter "
                "files. Structural fit means IBKR and Tastytrade are "
                "already compliant -- the protocol is a check over "
                "their existing surface, not a new requirement."
            ),
            "runtime_checkable_for_loud_failure": (
                "The factory uses isinstance() to catch obvious "
                "miswiring (router, config dict, wrong venue) at "
                "boot. PEP 544 only verifies attribute presence -- "
                "signature correctness stays a static-analysis / "
                "test concern, which is fine for catching the "
                "miswiring class of bugs."
            ),
            "narrow_protocol_surface": (
                "Two members only: name + get_net_liquidation. The "
                "reconciler does not need place_order / cancel_order. "
                "Narrow surface keeps paper / test stubs cheap to "
                "write."
            ),
            "null_adapter_not_none_handling": (
                "Consumers that want 'no broker source' pass "
                "NullBrokerEquityAdapter(). Every wiring path hands "
                "the reconciler a real source object; the no-data "
                "semantics live inside the source itself, not in "
                "every call site."
            ),
            "factory_raises_typeerror": (
                "Miswiring is an operator-blocking bug, not a "
                "runtime degradation. Fail fast at boot beats "
                "silently producing a poller bound to nothing."
            ),
            "factory_does_not_start_poller": (
                "Lifecycle ownership stays with the caller (the "
                "supervisor). The factory is construction; "
                "start()/stop() remains the supervisor's job."
            ),
        },
        "scope_exclusions": {
            "supervisor_wiring_unchanged": (
                "v0.1.62 does not touch any code that decides "
                "whether to wire IBKR, Tastytrade, or nothing. "
                "v0.2.x will route through make_poller_for once the "
                "broker-selection policy is in place."
            ),
            "no_runtime_behaviour_change": (
                "BrokerEquityReconciler and BrokerEquityPoller are "
                "unchanged. They already accept the right callables; "
                "the contract layer only formalises that fact."
            ),
            "no_multi_broker_fanout": (
                "Apex evals are single-account. Cross-broker drift "
                "checking is deferred until we genuinely run "
                "multi-broker."
            ),
        },
        "production_venues_protocol_status": {
            "IbkrClientPortalVenue": ("Already satisfies. Pinned by test_ibkr_adapter_satisfies_protocol."),
            "TastytradeVenue": ("Already satisfies. Pinned by test_tastytrade_venue_satisfies_protocol."),
            "TradovateVenue": (
                "DORMANT (operator mandate 2026-04-24). When funding "
                "clears, add get_net_liquidation() async method and "
                "the protocol fit is automatic; no broker_equity_"
                "adapter changes needed."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_added_in_this_bundle": PROTOCOL_TESTS_ADDED,
        "tests_delta_residual_from_other_modules": (NEW_TESTS_ABS - prev_tests - PROTOCOL_TESTS_ADDED),
        "ruff_green_touched_files": True,
        "pre_existing_flaky_dashboard_test": (
            "test_dashboard_api.py::test_btc_lanes_empty_when_fleet_"
            "dir_absent reads from the real docs/btc_live/broker_"
            "fleet instead of tmp_path. Pre-existing test-isolation "
            "bug; unrelated to this bundle. Tracked for a dedicated "
            "cleanup pass."
        ),
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Broker-equity-adapter protocol scaffolding. "
                    "Runtime-checkable Protocol + NullBrokerEquity"
                    "Adapter stub + make_poller_for factory. No "
                    "runtime behaviour change; v0.2.x supervisor "
                    "rewiring lands on this contract."
                ),
                "tests_delta": PROTOCOL_TESTS_ADDED,
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
        f"{PROTOCOL_TESTS_ADDED:+d} in-scope)",
    )


if __name__ == "__main__":
    main()
