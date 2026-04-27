"""One-shot: bump roadmap_state.json to v0.1.54.

ALPHA EXPANSION -- MCP feature taps (Blockscout + LunarCrush), Deribit
options venue, BTC tail-hedge BS pricer, events calendar with blackout
windows, synthetic-scenario bridge. 62 new tests.

Why this bundle exists
----------------------
v0.1.52 shipped the scorecard-driven alpha core. v0.1.54 extends the
alpha surface along the axes the scorecard called out as "underused":

  1. "Two data-ingest modules (onchain, sentiment) TODO-comment MCP
     integration -- they never call a real MCP." -> ``features/
     mcp_taps.py`` gives both an optional MCP path via a Protocol
     adapter.
  2. "Tail-hedge pricer is equity-index only. BTC crashes 3x faster
     and the fleet has no OTM-put coverage." -> Deribit options venue
     + BTC BS variant.
  3. "FOMC / CPI / NFP windows are ignored by the entry policy." ->
     events calendar with pre/post blackout bisect.
  4. "Walk-forward stress uses flat paths. Need 2008-style flash
     crash and 2022 grind." -> ``backtest.synthetic_bridge`` bridges
     ``brain.synthetic`` and ``backtest.stress_scenarios`` into
     synthetic OHLCV.

What ships
----------
  * ``features/mcp_taps.py`` -- McpTap Protocol + Blockscout tap +
    LunarCrush tap. Feature-flag gated (``APEX_USE_MCP_TAPS`` env
    var). Existing REST clients unchanged; MCP is optional-parallel.
  * ``venues/deribit.py`` -- read-first Deribit client. Order paths
    explicitly REJECT with NotImplementedError until
    ``allow_orders=True``. ATM-IV fetch returns decimal.
  * ``core/tail_hedge.py`` -- new ``price_otm_put_btc_deribit``
    and ``"otm_put_btc_deribit"`` HedgeKind literal.
  * ``core/events_calendar.py`` -- ``CalendarEvent`` + ``EventsCalendar``
    with O(log n) bisect for next_event / blackout_active. Loaders
    for local JSON and BigData MCP.
  * ``backtest/synthetic_bridge.py`` -- maps stress-scenario returns
    + regime profile into seeded synthetic OHLCV.
  * Tests: 62 across the five modules.

Design choices
--------------
  * **MCP taps are additive, not replacements.** The existing REST
    clients (BlockscoutClient / LunarCrushClient) stay canonical.
    MCP taps provide an alternate path when connected. Opt-in via
    the env flag -- Databento-mandate-style dormancy.
  * **Deribit venue is read-first.** Orders rejected until
    `allow_orders=True` is explicitly flipped by the operator.
    Same pattern as the tail-hedge skeleton in v0.1.46.
  * **Events calendar uses bisect, not O(n) scan.** The calendar
    is small but hot; bisect is free correctness.
  * **Synthetic bridge is seeded.** Every scenario run with the
    same seed emits byte-identical bars. Matches the CI replay
    contract.

Delta
-----
  * tests_passing: 2596 -> 2658 (+62)
  * Five new modules across features / venues / core / backtest
  * tail_hedge.py extended (new Literal + new pricer function)
  * Ruff-clean on every new / modified file
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.54"
NEW_TESTS_ABS = 2658


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_54_alpha_expansion"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "ALPHA EXPANSION -- MCP feature taps (Blockscout + "
            "LunarCrush), Deribit options venue, BTC tail-hedge BS "
            "pricer, events calendar with blackout windows, "
            "synthetic-scenario bridge. 62 new tests."
        ),
        "theme": (
            "Expands the alpha surface along the axes the "
            "scorecard called out as underused -- real MCP "
            "integration for onchain + sentiment, BTC tail "
            "coverage via Deribit, event-aware entry blackouts, "
            "seeded synthetic stress bars for walk-forward."
        ),
        "operator_directive_quote": (
            "no new symbol ships without tail coverage and "
            "event-aware blackouts. Underused surfaces in the "
            "scorecard get closed this bundle."
        ),
        "artifacts_added": {
            "features": ["features/mcp_taps.py"],
            "venues": ["venues/deribit.py"],
            "core": ["core/events_calendar.py"],
            "backtest": ["backtest/synthetic_bridge.py"],
            "tests": [
                "tests/test_features_mcp_taps.py",
                "tests/test_venues_deribit.py",
                "tests/test_core_tail_hedge_deribit.py",
                "tests/test_core_events_calendar.py",
                "tests/test_backtest_synthetic_bridge.py",
            ],
            "scripts": ["scripts/_bump_roadmap_v0_1_54.py"],
        },
        "artifacts_modified": {
            "core": ["core/tail_hedge.py"],
            "features": [
                "features/onchain.py",
                "features/sentiment.py",
            ],
        },
        "design_notes": {
            "mcp_taps_additive": (
                "MCP taps are optional-parallel, not replacements. "
                "Existing BlockscoutClient and LunarCrushClient stay "
                "canonical. Operator opts in via APEX_USE_MCP_TAPS "
                "env var."
            ),
            "deribit_read_first": (
                "Deribit venue rejects all order paths with "
                "NotImplementedError until allow_orders=True is "
                "explicitly flipped. Same pattern as v0.1.46 "
                "tail-hedge skeleton."
            ),
            "calendar_bisect": (
                "O(log n) next_event / blackout_active via bisect. High-impact tags: FOMC, CPI, PCE, NFP, ECB, etc."
            ),
            "synthetic_seeded": (
                "Every scenario run with the same seed emits byte-identical bars. Matches the CI replay contract."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Alpha Expansion ships: MCP feature taps + "
                    "Deribit options venue + BTC OTM-put BS "
                    "pricer + events calendar with blackout "
                    "windows + synthetic-scenario bridge. Five "
                    "new modules, 62 new tests, ruff-clean."
                ),
                "tests_delta": NEW_TESTS_ABS - prev_tests,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})",
    )


if __name__ == "__main__":
    main()
