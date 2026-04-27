"""One-shot: bump roadmap_state.json to v0.1.45.

LIVE BOT CONTROLLER WIRING -- MnqBot and EthPerpBot now auto-wire the
full AI-Optimized strategy stack (RuntimeAllowlistCache +
AllowlistScheduler + RouterAdapter) at start-up via the canonical
:func:`strategies.live_adapter.build_live_adapter` factory. The chain
from bar ingest -> OOS qualifier -> allowlist refresh -> dispatch ->
signal is now fully self-constructing on bot startup.

Context
-------
By v0.1.44 every mechanical piece of the OOS-governed trading path
existed. What was still missing: the operator had to import four
modules, pick sensible TTL/cadence/warmup numbers, wire them
together, and pass the adapter into the bot. That boilerplate was
error-prone and undocumented.

v0.1.45 closes that gap with:

  * ``strategies/live_adapter.py`` -- new. Exposes
    :func:`build_live_adapter` plus four canonical live defaults
    (:data:`DEFAULT_LIVE_TTL_SECONDS`,
    :data:`DEFAULT_LIVE_REFRESH_EVERY_N_BARS`,
    :data:`DEFAULT_LIVE_REFRESH_EVERY_SECONDS`,
    :data:`DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST`). The factory is pure
    in-process construction -- no I/O, no threads, safe in a bot's
    async ``start()`` coroutine.
  * ``bots/mnq/bot.py`` -- added two __init__ kwargs:
    ``auto_wire_ai_strategies: bool = False`` and
    ``ai_strategy_config: dict[str, Any] | None = None``. When
    ``auto_wire`` is True and no adapter was supplied, ``start()``
    calls ``build_live_adapter(config.symbol, **ai_strategy_config)``
    and installs the result on ``self._strategy_adapter``.
  * ``bots/eth_perp/bot.py`` -- same two kwargs; start() strips the
    ``USDT`` suffix from ``config.symbol`` before handing the asset
    to the factory (so ETH strategies run against the ``ETH`` key in
    :data:`DEFAULT_ELIGIBILITY`, not ``ETHUSDT``).
  * ``tests/test_strategies_live_adapter.py`` -- 32 tests covering
    the factory, bot auto-wire, and the hot-path failure containment
    contract.

Live defaults (see docstring on :mod:`strategies.live_adapter`):
  * ``DEFAULT_LIVE_TTL_SECONDS``           = 7200.0
  * ``DEFAULT_LIVE_REFRESH_EVERY_N_BARS``  = 288
  * ``DEFAULT_LIVE_REFRESH_EVERY_SECONDS`` = 3600.0
  * ``DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST`` = 200
  TTL >= 2 * wall-clock trigger so the cache is always fresh
  between normal scheduler ticks. The two trigger axes fire on
  whichever-first semantics, so fast tapes refresh hourly and slow
  tapes refresh once per trading day.

Delta
-----
  * tests_passing: 2004 -> 2036 (+32 new live-adapter tests)
  * All pre-existing tests still pass unchanged
  * Ruff-clean on new module and all touched files
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.45 the pipeline existed but required operator
assembly:

    operator -> build cache + scheduler + adapter -> bot(adapter=...)

With v0.1.45 the pipeline is a one-liner:

    bot = MnqBot(auto_wire_ai_strategies=True)
    await bot.start()
    # bot now loops bar -> scheduler.tick -> qualifier ->
    # cache.update -> dispatch -> signal on every tick.

The OOS qualification loop is truly end-to-end self-driving on any
bot that flips the `auto_wire_ai_strategies` flag. No operator in
the refresh loop. No hand-curated eligibility table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.45"
NEW_TESTS_ABS = 2036


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_45_live_bot_controller_wiring"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "LIVE BOT CONTROLLER WIRING -- MnqBot and EthPerpBot "
            "auto-wire the full AI-Optimized stack (cache + "
            "scheduler + adapter) at start-up via "
            "build_live_adapter()"
        ),
        "theme": (
            "Give live bots a one-line switch to self-construct the "
            "full OOS-governed dispatch pipeline. The factory module "
            "exposes canonical defaults (TTL=7200s, refresh=288 "
            "bars OR 3600s, warmup=200 bars). Bots get two new "
            "kwargs: auto_wire_ai_strategies (toggle) and "
            "ai_strategy_config (override). When the toggle is on "
            "and no operator-supplied adapter exists, start() "
            "builds and installs one."
        ),
        "artifacts_added": {
            "strategies": ["strategies/live_adapter.py"],
            "tests": ["tests/test_strategies_live_adapter.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_45.py"],
        },
        "artifacts_modified": {
            "bots": [
                "bots/mnq/bot.py (+auto_wire_ai_strategies, +ai_strategy_config, start() auto-builds adapter)",
                "bots/eth_perp/bot.py (same wiring; strips USDT suffix when deriving the strategy asset key)",
            ],
        },
        "api_surface": {
            "build_live_adapter": (
                "(asset, *, buffer_bars=300, ttl_seconds=7200.0, "
                "refresh_every_n_bars=288, refresh_every_seconds=3600.0, "
                "min_bars_before_first=200, base_eligibility=None, "
                "eligibility_override=None, decision_sink=None, "
                "scheduler_kwargs=None, clock=None, "
                "kill_switch_active=False, "
                "session_allows_entries=True) -> RouterAdapter"
            ),
            "DEFAULT_LIVE_TTL_SECONDS": "7200.0",
            "DEFAULT_LIVE_REFRESH_EVERY_N_BARS": "288",
            "DEFAULT_LIVE_REFRESH_EVERY_SECONDS": "3600.0",
            "DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST": "200",
            "MnqBot.auto_wire_ai_strategies": (
                "bool = False  -- when True + no strategy_adapter, start() builds one via build_live_adapter()"
            ),
            "MnqBot.ai_strategy_config": (
                "dict[str, Any] | None = None  -- forwarded as **kwargs to build_live_adapter on auto-wire"
            ),
            "EthPerpBot.auto_wire_ai_strategies": ("same contract; strips USDT suffix from symbol"),
        },
        "design_notes": {
            "defaults_sized_to_qualifier": (
                "TTL=7200s is 2x the 3600s wall-clock trigger so the "
                "cache is always fresh between scheduler ticks. "
                "288 bars = 24h of 5m data. 200-bar warmup matches "
                "the DSR estimator's minimum sample size."
            ),
            "bar_or_time_whichever_first": (
                "refresh_every_n_bars and refresh_every_seconds "
                "both fire on whichever-first semantics. Fast tapes "
                "refresh every hour (the time trigger wins); slow "
                "tapes refresh every 288 bars (the bar-count trigger "
                "wins)."
            ),
            "static_override_still_wins": (
                "eligibility_override is passed to the RouterAdapter's "
                "static eligibility field, not the cache. When both "
                "are populated, the RouterAdapter's _effective_eligibility "
                "merges them with static-wins semantics."
            ),
            "clock_shared_across_cache_and_scheduler": (
                "When `clock=` is injected, BOTH the cache and the "
                "scheduler receive the same callable. Their notions "
                "of 'now' stay aligned so TTL expiry and trigger "
                "bookkeeping don't drift under accelerated test clocks."
            ),
            "auto_wire_is_start_time_not_init_time": (
                "Auto-wire runs in async start(), not __init__. "
                "Rationale: __init__ must stay I/O-free (unit tests "
                "construct bots under pytest collection). start() "
                "is the lifecycle event the operator explicitly calls "
                "at bot launch, so that's where heavy construction "
                "belongs."
            ),
            "local_import_at_start_time": (
                "build_live_adapter is imported inside start() rather "
                "than at module top. Keeps the bot importable in "
                "environments that do not load the strategies "
                "subpackage (e.g. the legacy 4-setup test suite)."
            ),
            "eth_symbol_suffix_strip": (
                "EthPerpBot's config.symbol is ETHUSDT (Bybit format) "
                "but the strategy layer keys off the bare asset ETH. "
                "Start() strips the USDT suffix before handing the "
                "asset to the factory; this keeps the venue layer "
                "and strategy layer independent."
            ),
        },
        "test_coverage": {
            "tests_added": 32,
            "classes": {
                "TestLiveDefaults": 5,
                "TestFactoryShape": 5,
                "TestKnobForwarding": 9,
                "TestTriggerNoneDisablesAxis": 2,
                "TestTriggerValidation": 1,
                "TestEndToEndDispatch": 4,
                "TestFailureIsolation": 1,
                "TestMnqBotAutoWire": 3,
                "TestEthPerpBotAutoWire": 2,
            },
        },
        "ruff_clean_on": [
            "strategies/live_adapter.py",
            "bots/mnq/bot.py",
            "bots/eth_perp/bot.py",
            "tests/test_strategies_live_adapter.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "OOS-governed trading loop is now end-to-end "
                "self-driving from bar ingest through to the live "
                "bot's order-router boundary. No operator hand-wiring "
                "remains."
            ),
            "note": (
                "v0.1.46 shifts from pipeline-wiring to risk-model "
                "evolution: the ADAPTIVE SIZING ENGINE. Position size "
                "becomes a function of (regime x confluence x "
                "historical-success x equity-band) with tiered "
                "multipliers (CONVICTION, STANDARD, REDUCED, PROBE, "
                "SKIP). High-confluence trend setups earn 2-3x "
                "sizing; low-confluence ranging setups get probe-sized "
                "or skipped entirely. The engine self-evolves on live "
                "trade outcomes and emits a self-review signal when "
                "drawdown breaches a regime-specific band."
            ),
        },
        "python_touched": True,
        "jsx_touched": False,
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
                    "Live bots auto-wire the full AI-Optimized stack "
                    "(cache + scheduler + adapter) at start-up via "
                    "build_live_adapter. MnqBot and EthPerpBot both "
                    "expose auto_wire_ai_strategies + "
                    "ai_strategy_config kwargs. The OOS qualification "
                    "loop is end-to-end self-driving."
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
    print(f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})")
    print(
        "  shipped: strategies/live_adapter.py factory + MnqBot "
        "and EthPerpBot auto-wire. Live bots self-construct the "
        "OOS-governed dispatch stack at start-up."
    )


if __name__ == "__main__":
    main()
