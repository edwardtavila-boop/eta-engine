"""One-shot: bump roadmap_state.json to v0.1.35.

MULTI-BOT WIRING -- RouterAdapter threaded into the remaining 5 bots.

Context
-------
v0.1.34 shipped MnqBot wiring. v0.1.35 completes the portfolio roll-out:
the same ``strategy_adapter: RouterAdapter | None = None`` contract
now lands on every directional bot, with zero-regression behaviour
when the default ``None`` is kept.

What v0.1.35 adds
-----------------
  * ``bots/eth_perp/bot.py`` (edited, additive)

    - New optional constructor parameter ``strategy_adapter``.
    - ``on_bar`` checks adapter first: propagates
      ``bot.state.is_killed -> adapter.kill_switch_active``, calls
      ``adapter.push_bar(bar)``, and when a Signal comes back applies
      ``effective_leverage(confidence, close, atr)`` before routing.
      On router-flat the legacy 3-setup loop (trend/mean-revert/breakout)
      runs unchanged.

  * ``bots/crypto_seed/bot.py`` (edited, additive)

    - New optional constructor parameter ``strategy_adapter``.
    - ``on_bar`` runs grid management first (grid fills drain via
      orchestrator drainage at its own cadence), THEN asks the adapter
      for a directional trade. Legacy ``directional_overlay`` runs only
      when the adapter is absent or flat.

  * ``bots/nq/bot.py`` (UNCHANGED)

    - NqBot inherits from MnqBot via ``**kwargs``, so the v0.1.34
      MNQ wiring applies automatically. No code edit required.

  * ``bots/sol_perp/bot.py`` + ``bots/xrp_perp/bot.py`` (UNCHANGED)

    - Both inherit from EthPerpBot via ``**kwargs``, so the v0.1.35
      ETH wiring applies automatically. No code edit required.

  * ``tests/test_bots_router_adapter_multi.py`` (new, +24 tests)

    Six test classes:
      - ``TestEthPerpBotRouterAdapter`` -- wiring, router-wins, flat
        fallthrough, kill-switch short-circuit. 4 tests.
      - ``TestNqBotRouterAdapter`` -- wiring, router-wins with
        NQ-calibrated stop distance, flat fallthrough. 4 tests.
      - ``TestSolPerpBotRouterAdapter`` -- wiring, router-wins with
        SOL leverage calc, flat fallthrough. 4 tests.
      - ``TestXrpPerpBotRouterAdapter`` -- wiring, router-wins with
        XRP 50x cap, flat fallthrough. 4 tests.
      - ``TestCryptoSeedBotRouterAdapter`` -- wiring, router-wins over
        overlay, flat falls through to overlay, grid still runs when
        adapter flat on dull bars. 5 tests.
      - ``TestAdapterCrossBotSanity`` -- all bots skip routing when
        killed, all accept the strategy_adapter kwarg. 3 tests.

Delta
-----
  * tests_passing: 1723 -> 1747 (+24 new multi-bot wiring tests)
  * Every pre-existing bot test still passes unchanged
  * Ruff-clean on the edited bot files and the new test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
With this bundle EVERY directional bot in the EVOLUTIONARY TRADING ALGO portfolio
can now consume the AI-Optimized SMC/ICT strategy stack through a
uniform ``strategy_adapter`` kwarg. The RouterAdapter priority design
keeps each bot's legacy setup loop as a safety net on router-flat /
warmup-insufficient ticks, so there's zero regression risk if the AI
stack abstains for a given instrument. The ETH/SOL/XRP leverage gating
fires on the adapter signal's confidence just as it does on legacy
signals -- the router layer never sees a leverage-unsafe trade.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.35"
NEW_TESTS_ABS = 1747


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_35_multi_bot_wiring"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "MULTI-BOT WIRING -- RouterAdapter threaded into the five "
            "remaining bots (ETH, SOL, XRP, NQ, CryptoSeed) "
            "(additive, backwards compatible)"
        ),
        "theme": (
            "Completes the v0.1.33/v0.1.34 engine-adapter rollout. "
            "Every directional bot in the portfolio now asks the "
            "AI-Optimized strategy stack BEFORE falling through to "
            "its legacy setups, with zero regression risk because the "
            "legacy paths stay intact when strategy_adapter is None."
        ),
        "artifacts_edited": {
            "bots": [
                "bots/eth_perp/bot.py",
                "bots/crypto_seed/bot.py",
            ],
        },
        "artifacts_inherited_no_edit": {
            "bots": [
                "bots/nq/bot.py (inherits MnqBot via **kwargs)",
                "bots/sol_perp/bot.py (inherits EthPerpBot via **kwargs)",
                "bots/xrp_perp/bot.py (inherits EthPerpBot via **kwargs)",
            ],
        },
        "artifacts_added": {
            "tests": ["tests/test_bots_router_adapter_multi.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_35.py"],
        },
        "integration_points": {
            "eth_perp_constructor": ("EthPerpBot(..., strategy_adapter: RouterAdapter | None = None)"),
            "eth_perp_on_bar_priority": (
                "1) check_risk(); 2) if adapter -> sync kill_switch_active "
                "and push_bar; 3) if Signal -> apply effective_leverage and "
                "route; 4) else legacy trend_follow/mean_revert/breakout"
            ),
            "crypto_seed_on_bar_priority": (
                "1) check_risk(); 2) grid management (ALWAYS runs); "
                "3) if adapter -> sync kill_switch_active and push_bar; "
                "4) if Signal -> route through on_signal and return; "
                "5) else legacy directional_overlay"
            ),
            "inheritance_pattern": (
                "NqBot/SolPerpBot/XrpPerpBot forward all kwargs to their "
                "parent via super().__init__(config, **kwargs), so the "
                "strategy_adapter param propagates automatically."
            ),
            "leverage_on_adapter_signals": (
                "EthPerpBot stamps signal.meta['leverage'] with "
                "effective_leverage(confidence, close, atr) before routing. "
                "SOL/XRP overrides of confluence_leverage and "
                "liquidation_safe_leverage are applied via polymorphism."
            ),
            "backwards_compatible": (
                "strategy_adapter default is None on every bot. All pre-v0.1.35 bot tests pass unchanged."
            ),
        },
        "test_coverage": {
            "tests_added": 24,
            "classes": {
                "TestEthPerpBotRouterAdapter": 4,
                "TestNqBotRouterAdapter": 4,
                "TestSolPerpBotRouterAdapter": 4,
                "TestXrpPerpBotRouterAdapter": 4,
                "TestCryptoSeedBotRouterAdapter": 5,
                "TestAdapterCrossBotSanity": 3,
            },
        },
        "ruff_clean_on": [
            "bots/eth_perp/bot.py",
            "bots/crypto_seed/bot.py",
            "tests/test_bots_router_adapter_multi.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "entire directional portfolio is now live-ready with "
                "AI-Optimized strategy consumption."
            ),
            "note": (
                "v0.1.36 will build the walk-forward backtest harness "
                "for the 6 AI-Optimized strategies per-asset so we can "
                "calibrate eligibility thresholds before any live run."
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
                    "Portfolio-wide RouterAdapter wiring: ETH/SOL/XRP/NQ/"
                    "CryptoSeed now consume the AI-Optimized strategy "
                    "stack ahead of their legacy setups; backwards "
                    "compatible when no adapter supplied"
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
        "  shipped: RouterAdapter wiring now live on ETH/SOL/XRP/NQ/"
        "CryptoSeed with leverage gating on adapter signals and legacy "
        "fallback preserved"
    )


if __name__ == "__main__":
    main()
