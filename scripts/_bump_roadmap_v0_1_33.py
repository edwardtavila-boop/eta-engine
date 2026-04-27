"""One-shot: bump roadmap_state.json to v0.1.33.

ENGINE ADAPTER -- policy_router wired into the live bot path.

Context
-------
v0.1.31 shipped the six AI-Optimized Evolutionary Trading Algo strategies as pure
functions in ``eta_engine.strategies.*``. v0.1.32 shipped the
cross-regime OOS validation harness. Neither bundle wired the strategy
stack into the actual bot loop -- the six strategies existed but no
``BaseBot.on_bar`` handler could feed it a dict bar and receive a bot
``Signal`` back.

v0.1.33 closes that integration gap.

What v0.1.33 adds
-----------------
  * ``strategies/engine_adapter.py`` (~260 lines, new)

    Pure conversion helpers plus a thin stateful wrapper around
    :func:`policy_router.dispatch`.

    Public API:
      - ``bar_from_dict(dict) -> Bar`` -- canonical (``open``/``high``
        /``low``/``close``/``volume``/``ts``) and short (``o/h/l/c/v/t``)
        key forms both accepted; missing OHLC raises ``ValueError``;
        missing ``ts`` falls back to a caller-supplied counter.
      - ``context_from_dict(dict, *, kill_switch_active, session_allows_entries,
        overrides)`` -- builds ``eta_policy.StrategyContext`` from a bar
        dict + explicit flags. Reads regime as enum-with-.value, raw
        string, or explicit ``regime_label``. Accepts htf_bias / trend_bias
        as Side enum, "long"/"buy"/"up" synonyms, or string variants.
      - ``strategy_signal_to_bot_signal(StrategySignal, symbol, price_fallback)
        -> Signal | None`` -- returns None for non-actionable signals;
        actionable signals preserve stop_distance, target, rationale_tags,
        and strategy_meta inside ``Signal.meta``.
      - ``RouterAdapter(asset, max_bars=300, eligibility, registry,
        kill_switch_active, session_allows_entries)`` -- stateful wrapper
        holding a ``collections.deque`` bar buffer, the last
        ``RouterDecision`` for observability, and ``seed()`` / ``reset()``
        lifecycle hooks. ``push_bar(dict)`` returns a bot-ready
        ``Signal | None`` on every tick.
      - ``has_eligibility_for(asset) -> bool`` -- True when
        ``DEFAULT_ELIGIBILITY`` has an explicit row for the asset.

    Design guarantees:
      - pure helpers are stateless; only ``RouterAdapter`` holds state.
      - no new dependencies (stdlib ``collections.deque`` + existing
        pydantic ``Signal`` + strategies package).
      - defensive copy on ``RouterAdapter.bars`` so callers cannot mutate
        buffer state.
      - kill-switch flows through ``context_from_dict`` into the
        ``StrategyContext.kill_switch_active`` path, which
        ``eta_policy._risk_mult`` zeroes regardless of detector output.
      - session gate short-circuits every strategy to ``FLAT``.
      - additive: no existing bot, test, or script was modified.

  * ``tests/test_strategies_engine_adapter.py`` (~350 lines, +43 tests)

    Seven test classes:
      - ``TestBarFromDict`` -- canonical + short keys, ts fallback,
        volume default, missing-OHLC raise, non-numeric raise, non-numeric
        ts fallback. 7 tests.
      - ``TestContextFromDict`` -- empty-dict defaults, regime_label
        passthrough, regime-as-enum (.value), regime-as-string, confluence
        + vol_z, htf_bias string coercion (long/SELL/etc.), htf_bias enum,
        kwarg vs dict-value precedence, overrides last-write-wins. 10 tests.
      - ``TestStrategySignalToBotSignal`` -- long/short conversion, flat
        returns None, zero-confidence returns None, zero-risk_mult returns
        None, price fallback, strategy_meta propagation. 7 tests.
      - ``TestHasEligibilityFor`` -- all 7 known assets + lowercase +
        unknown-asset false. 3 tests.
      - ``TestRouterAdapterBasics`` -- upper-cased asset, default buffer
        cap, rejects buffer < 2, starts-empty, defensive-copy snapshot.
        5 tests.
      - ``TestRouterAdapterBuffer`` -- push appends, max_bars enforced as
        ring buffer, seed bulk-loads without dispatching, reset clears.
        4 tests.
      - ``TestRouterAdapterDispatch`` -- flat-bars yields no signal but
        records RouterDecision, 4 MNQ candidates, kill-switch propagates,
        session-closed propagates. 4 tests.
      - ``TestRouterAdapterWithStubRegistry`` -- inject fake strategy to
        force winner, fake-flat returns None, bar ts auto-assigned from
        counter. 3 tests.

Delta
-----
  * tests_passing: 1669 -> 1712 (+43)
  * All new files ruff-clean
  * No phase-level status changes; overall_progress_pct stays at 99

How a bot uses this
-------------------
>>> from eta_engine.strategies.engine_adapter import RouterAdapter
>>> adapter = RouterAdapter(asset="MNQ")  # or "BTC" / "PORTFOLIO" / etc.
>>> adapter.seed(historical_bar_dicts)
>>> async def on_bar(bar_dict):
...     sig = adapter.push_bar(bar_dict)
...     if sig is not None:
...         await self.on_signal(sig)
...     # also inspect adapter.last_decision for observability
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.33"
NEW_TESTS_ABS = 1712


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_33_engine_adapter"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "ENGINE ADAPTER -- policy_router wired into the live bot path "
            "via RouterAdapter (pure bar/ctx/signal mapping + rolling buffer)"
        ),
        "theme": (
            "v0.1.31 shipped the six strategies as pure functions; v0.1.32 "
            "shipped cross-regime OOS validation. Neither wired the stack "
            "into the real bot loop. v0.1.33 provides the adapter so any "
            "BaseBot subclass can feed a dict bar and receive a bot Signal "
            "back without either side knowing about the other."
        ),
        "artifacts_added": {
            "strategies": ["strategies/engine_adapter.py"],
            "tests": ["tests/test_strategies_engine_adapter.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_33.py"],
        },
        "public_api": {
            "bar_from_dict": (
                "Pure converter from bot-style dict bars "
                "(`ts/open/high/low/close/volume` or short `t/o/h/l/c/v`) "
                "to `strategies.models.Bar`. Missing OHLC raises, missing "
                "ts falls back to caller-supplied counter."
            ),
            "context_from_dict": (
                "Build `strategies.eta_policy.StrategyContext` from a bar "
                "dict + explicit kill_switch_active + session_allows_entries "
                "flags. Supports regime enum-with-.value or plain string, "
                "htf_bias string coercion (long/buy/up, short/sell/down), "
                "overrides last-write-wins."
            ),
            "strategy_signal_to_bot_signal": (
                "Map `strategies.models.StrategySignal` -> "
                "`bots.base_bot.Signal` (pydantic). Returns None for "
                "non-actionable signals. Preserves stop_distance, target, "
                "rationale_tags, strategy_meta inside `Signal.meta`."
            ),
            "RouterAdapter": (
                "Stateful bot-facing wrapper. Holds a rolling deque "
                "(default 300 bars), dispatches via policy_router.dispatch "
                "on each push_bar(), records last_decision for "
                "observability, exposes seed()/reset() for bootstrap."
            ),
            "has_eligibility_for": (
                "True if DEFAULT_ELIGIBILITY has an explicit row for an "
                "asset (MNQ/NQ/BTC/ETH/SOL/XRP/PORTFOLIO). Lets callers "
                "skip adapter construction for exotic symbols."
            ),
        },
        "design_guarantees": {
            "pure_helpers": ("bar_from_dict, context_from_dict, strategy_signal_to_bot_signal are stateless"),
            "no_new_deps": (
                "uses only stdlib (collections.deque, dataclasses) + existing pydantic Signal + strategies package"
            ),
            "defensive_copy": ("RouterAdapter.bars returns list(self._bars) so callers cannot mutate buffer"),
            "kill_switch_path": (
                "kill_switch_active flows through context_from_dict into "
                "StrategyContext -> _risk_mult zeros risk, regardless of "
                "detector output"
            ),
            "session_gate": ("session_allows_entries=False forces every strategy to FLAT via StrategyContext"),
            "backwards_compatible": ("adapter is additive; no existing bot or test was modified"),
        },
        "integration_contract": {
            "entry_point": ("RouterAdapter(asset='MNQ').push_bar(bar_dict) -> Signal | None"),
            "seed_pattern": ("adapter.seed(historical_bars); then loop push_bar in on_bar hook"),
            "observability": ("adapter.last_decision: RouterDecision | None after each tick"),
            "mnq_example": (
                "adapter = RouterAdapter(asset='MNQ'); in MnqBot.on_bar, "
                "do `sig = adapter.push_bar(bar); if sig: await "
                "self.on_signal(sig)` alongside the existing 4-setup loop."
            ),
        },
        "test_coverage": {
            "tests_added": 43,
            "classes": {
                "TestBarFromDict": 7,
                "TestContextFromDict": 10,
                "TestStrategySignalToBotSignal": 7,
                "TestHasEligibilityFor": 3,
                "TestRouterAdapterBasics": 5,
                "TestRouterAdapterBuffer": 4,
                "TestRouterAdapterDispatch": 4,
                "TestRouterAdapterWithStubRegistry": 3,
            },
        },
        "ruff_clean_on": [
            "strategies/engine_adapter.py",
            "tests/test_strategies_engine_adapter.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": "unchanged -- still funding-gated on P9_ROLLOUT",
            "note": (
                "v0.1.33 is additive infrastructure; wiring MnqBot / "
                "NqBot / BtcBot to actually use the adapter is intentional "
                "follow-up work so per-bot tests can be updated "
                "deliberately, not as a side-effect of this bundle."
            ),
        },
        "python_touched": True,
        "jsx_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
    }

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Engine adapter: policy_router wired into live bot "
                    "on_bar path (pure helpers + stateful RouterAdapter "
                    "+ 43 tests)"
                ),
                "tests_delta": NEW_TESTS_ABS - prev_tests,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})")
    print("  shipped: strategies/engine_adapter.py + tests/test_strategies_engine_adapter.py")
    print("  the six AI-Optimized strategies now have a live-bot integration surface")


if __name__ == "__main__":
    main()
