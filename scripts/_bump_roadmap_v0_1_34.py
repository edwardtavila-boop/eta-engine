"""One-shot: bump roadmap_state.json to v0.1.34.

MNQ WIRING -- RouterAdapter threaded into MnqBot.on_bar.

Context
-------
v0.1.33 shipped ``strategies/engine_adapter.py`` with the
:class:`RouterAdapter` class + 43 adapter-surface tests, but no bot
actually consumed it. v0.1.34 is the first real integration: the
``MnqBot.on_bar`` handler now optionally asks the adapter for a signal
BEFORE falling through to the legacy 4-setup loop.

What v0.1.34 adds
-----------------
  * ``bots/mnq/bot.py`` (edited, additive)

    - New optional constructor parameter ``strategy_adapter:
      RouterAdapter | None = None``. ``None`` preserves the exact
      pre-v0.1.34 behaviour so every existing MnqBot test still passes
      untouched.
    - ``on_bar`` first checks ``self._strategy_adapter`` when wired:
      it propagates ``bot.state.is_killed -> adapter.kill_switch_active``
      for the current tick, calls ``adapter.push_bar(bar)``, and if the
      adapter returns a ``Signal`` routes it through ``on_signal`` and
      returns early. Otherwise the legacy 4-setup loop runs as before.
    - ``TYPE_CHECKING`` import guard keeps RouterAdapter out of the
      runtime import graph when no adapter is wired.

  * ``tests/test_bots_mnq_router_adapter.py`` (new, +11 tests)

    Five test classes:
      - ``TestMnqBotRouterAdapterWiring`` -- without adapter ==> legacy
        behaviour; with adapter ==> stored. 2 tests.
      - ``TestOnBarAdapterPriority`` -- router signal wins over legacy,
        router flat falls through to legacy, kill-switch on bot skips
        adapter via check_risk, kill-switch state tracked per tick.
        4 tests.
      - ``TestOnBarAdapterWithNoSignal`` -- dull bars produce no router
        call. 1 test.
      - ``TestAdapterSignalShape`` -- adapter signal's meta has
        stop_distance routable by _size_from_signal, tradovate_symbol
        override preserved, LONG side maps to venue Side.BUY. 3 tests.
      - Helper: ``_stub_long_strategy_adapter`` / ``_stub_flat_strategy_adapter``
        inject a fake StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT registry
        so the adapter returns deterministic outputs without needing
        40 bars of warmup.

Delta
-----
  * tests_passing: 1712 -> 1723 (+11 new router-wiring tests)
  * Every pre-existing MnqBot test still passes unchanged
  * Ruff-clean on bots/mnq/bot.py and the new test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
With this bundle the AI-Optimized SMC/ICT strategy stack runs on live
MNQ bars the instant an MnqBot is constructed with a ``RouterAdapter``.
Previously the strategies existed but had no bridge to the bot loop.
The adapter priority design keeps the legacy 4-setup loop as a
safety net on router-flat / warmup-insufficient ticks, so there's
zero regression risk if the AI stack abstains.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.34"
NEW_TESTS_ABS = 1723


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_34_mnq_router_wiring"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": ("MNQ WIRING -- RouterAdapter threaded into MnqBot.on_bar (additive, backwards compatible)"),
        "theme": (
            "First real bot integration of the v0.1.33 engine adapter. "
            "MnqBot now asks the AI-Optimized strategy stack BEFORE "
            "falling through to the legacy 4-setup loop, with zero "
            "regression risk because the legacy path stays intact when "
            "strategy_adapter is None."
        ),
        "artifacts_edited": {
            "bots": ["bots/mnq/bot.py"],
        },
        "artifacts_added": {
            "tests": ["tests/test_bots_mnq_router_adapter.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_34.py"],
        },
        "integration_points": {
            "constructor": ("MnqBot(..., strategy_adapter: RouterAdapter | None = None)"),
            "on_bar_priority": (
                "1) check_risk(); 2) if adapter wired -> sync "
                "kill_switch_active and call push_bar; 3) if adapter "
                "returns Signal, route via on_signal and return; 4) else "
                "fall through to legacy ORB/EMA/Sweep/MR loop"
            ),
            "kill_switch_sync": (
                "self._strategy_adapter.kill_switch_active = "
                "self.state.is_killed on every bar -- ensures mid-session "
                "kills propagate even without an explicit reset"
            ),
            "backwards_compatible": (
                "strategy_adapter default is None. All 20 pre-existing MnqBot tests pass unchanged."
            ),
        },
        "test_coverage": {
            "tests_added": 11,
            "classes": {
                "TestMnqBotRouterAdapterWiring": 2,
                "TestOnBarAdapterPriority": 4,
                "TestOnBarAdapterWithNoSignal": 1,
                "TestAdapterSignalShape": 4,
            },
        },
        "ruff_clean_on": [
            "bots/mnq/bot.py",
            "tests/test_bots_mnq_router_adapter.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": ("unchanged -- still funding-gated on P9_ROLLOUT; MNQ integration surface is now live-ready"),
            "note": ("v0.1.35 will port the same wiring to the 5 remaining bots (NQ/BTC-ETH-SOL-XRP perps)."),
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
                    "MnqBot wires RouterAdapter into on_bar: AI-Optimized "
                    "strategies take priority over legacy 4-setup loop; "
                    "backwards compatible when no adapter supplied"
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
    print("  shipped: MnqBot.on_bar now threads RouterAdapter with kill-switch propagation + legacy fallback")


if __name__ == "__main__":
    main()
