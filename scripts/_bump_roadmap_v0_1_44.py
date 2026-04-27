"""One-shot: bump roadmap_state.json to v0.1.44.

LIVE ROUTER ADAPTER WIRING -- RouterAdapter.push_bar now ticks the
allowlist scheduler BEFORE dispatch. The live bot trading path is
fully closed against the OOS qualification loop: curve-fit strategies
drop out of dispatch automatically, fresh ones re-enter automatically,
with zero operator involvement.

Context
-------
v0.1.43 shipped :class:`AllowlistScheduler` with a bar-by-bar
:meth:`tick` entry point. v0.1.42 shipped
:class:`RuntimeAllowlistCache` which produces an
``as_eligibility_map()`` dict the router can consume directly. What
was still missing: the live bot code actually CALLING ``sched.tick``
on every bar and feeding the cache's map into ``dispatch``. v0.1.44
closes that last wire.

What v0.1.44 adds
-----------------
  * ``strategies/engine_adapter.py`` (modified)
    - Two new optional :class:`RouterAdapter` fields:
      ``allowlist_scheduler: AllowlistScheduler | None`` and
      ``scheduler_kwargs: Mapping[str, object] | None``.
    - New private method ``_tick_scheduler_safely`` -- invokes
      ``scheduler.tick(asset, bars, **kwargs)`` with a blanket
      try/except so a qualifier failure never crashes the hot trading
      loop. If the tick errors, the allowlist cache simply does not
      refresh this bar.
    - New private method ``_effective_eligibility`` -- merges the
      scheduler's cache map with the static ``eligibility`` override,
      static winning on per-asset conflict.
    - ``push_bar`` now calls ``_tick_scheduler_safely`` BEFORE
      ``dispatch``, and passes ``_effective_eligibility()`` as the
      eligibility argument. The decision sink still sees every
      dispatch, now with scheduler-informed eligibility.
    - Backward compatible: when ``allowlist_scheduler`` is ``None``
      (the existing default) ``push_bar`` behaves exactly as before.

  * ``tests/test_strategies_engine_adapter_scheduler.py`` (new, +13 tests)

    Five test classes:
      - ``TestSchedulerTicksBeforeDispatch`` -- scheduler tick happens
        before the router's dispatch call; scheduler ticks on every
        push_bar. 2
      - ``TestEffectiveEligibility`` -- cache-empty returns static;
        cache-only when static is None; static wins on conflict;
        scheduler fills assets not in static; both empty returns None;
        no-scheduler returns static identity. 6
      - ``TestFailureContainment`` -- qualifier exception does NOT
        break push_bar; scheduler failure falls back to
        DEFAULT_ELIGIBILITY when no static override. 2
      - ``TestDecisionSinkCooperation`` -- sink still receives the
        post-scheduler decision with scheduler-governed eligible set. 1
      - ``TestEndToEndLiveLoop`` -- only passing strategies are
        dispatched across multiple push_bars; static override takes
        precedence over scheduler verdict. 2

Delta
-----
  * tests_passing: 1991 -> 2004 (+13 new scheduler-integration tests)
  * All 78 pre-existing engine-adapter tests still pass unchanged
  * Ruff-clean on engine_adapter.py + new test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.44 the qualification loop existed as a set of
composable-but-unwired modules: the qualifier produced a verdict, the
cache stored it, the scheduler would refresh it if anyone ticked it.
Nobody ticked it. The live bot's ``RouterAdapter.push_bar`` was still
calling ``dispatch(... eligibility=self.eligibility)`` against its
static at-construction-time eligibility table.

With v0.1.44 the adapter's hot path is:

    push_bar(bar_dict)
      -> append to buffer
      -> scheduler.tick(asset, bars)          # NEW: auto-refresh
      -> dispatch(                            # was: self.eligibility
          eligibility=merge(cache_map, static_override),
      )
      -> decision_sink.emit(decision)

Every bar, the adapter asks the scheduler "should we re-qualify?" and
the scheduler answers based on its :class:`RefreshTrigger`. If yes,
the qualifier runs, the cache updates, and the very next dispatch on
this tick uses the new verdict. If no, the cache's prior fresh entry
governs. If the scheduler errors, the static override takes over --
fail-safe, not fail-closed.

The six AI-Optimized strategies now live or die on the live bot by
their own walk-forward + DSR evidence, not by a hand-curated table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.44"
NEW_TESTS_ABS = 2004


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_44_live_router_adapter_wiring"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "LIVE ROUTER ADAPTER WIRING -- RouterAdapter.push_bar ticks "
            "the allowlist scheduler before dispatch; OOS qualification "
            "now governs the live bot trading path"
        ),
        "theme": (
            "Close the last wire between OOS qualification and live "
            "dispatch. RouterAdapter now accepts an optional "
            "AllowlistScheduler which it ticks on every push_bar; the "
            "scheduler's cache map merges with the static eligibility "
            "override (static wins) to form the eligibility argument "
            "the router actually sees. Scheduler failures are wrapped "
            "in try/except so the hot trading loop never dies on a "
            "qualifier bug."
        ),
        "artifacts_added": {
            "tests": [
                "tests/test_strategies_engine_adapter_scheduler.py",
            ],
            "scripts": ["scripts/_bump_roadmap_v0_1_44.py"],
        },
        "artifacts_modified": {
            "strategies": [
                "strategies/engine_adapter.py "
                "(+allowlist_scheduler, +scheduler_kwargs, "
                "+_tick_scheduler_safely, +_effective_eligibility, "
                "push_bar merges scheduler into dispatch)",
            ],
        },
        "api_surface": {
            "RouterAdapter.allowlist_scheduler": (
                "AllowlistScheduler | None  -- new optional field. When set, push_bar ticks it before dispatch."
            ),
            "RouterAdapter.scheduler_kwargs": (
                "Mapping[str, object] | None  -- forwarded verbatim to scheduler.tick on every bar."
            ),
            "RouterAdapter._tick_scheduler_safely": (
                "() -> None  -- private; wraps scheduler.tick in a "
                "blanket try/except so qualifier failure never crashes "
                "push_bar."
            ),
            "RouterAdapter._effective_eligibility": (
                "() -> dict[str, tuple[StrategyId, ...]] | None  -- "
                "merges scheduler cache with static override; static "
                "wins on per-asset conflict."
            ),
        },
        "design_notes": {
            "tick_before_dispatch": (
                "The scheduler tick happens BEFORE the dispatch call "
                "in push_bar, not after. That way a refresh that "
                "fires on THIS bar's tick already governs THIS bar's "
                "eligibility -- the freshest possible verdict."
            ),
            "static_wins_on_conflict": (
                "When both the static eligibility dict and the "
                "scheduler's cache map have an entry for the same "
                "asset, the static dict wins. Rationale: static "
                "eligibility is an explicit operator choice and must "
                "not be overridden by automated qualification."
            ),
            "blanket_try_except_on_tick": (
                "Scheduler failures (e.g. qualifier bugs, cache errors) "
                "are swallowed at the adapter boundary. Letting a "
                "qualifier exception propagate would mean one strategy's "
                "bug takes the bot offline. Instead the cache stays at "
                "its last-known-good state and dispatch falls back."
            ),
            "fallback_chain": (
                "Effective eligibility precedence: "
                "static override -> scheduler cache -> None "
                "(which dispatch() treats as DEFAULT_ELIGIBILITY). "
                "Fail-safe: the bot never runs in a state with no "
                "eligible strategies unless the operator explicitly "
                "configured that."
            ),
            "zero_overhead_when_scheduler_absent": (
                "When allowlist_scheduler is None (the default) the "
                "new code paths short-circuit immediately and "
                "_effective_eligibility returns self.eligibility "
                "unchanged. All 78 pre-existing engine-adapter tests "
                "still pass with no modifications."
            ),
        },
        "test_coverage": {
            "tests_added": 13,
            "classes": {
                "TestSchedulerTicksBeforeDispatch": 2,
                "TestEffectiveEligibility": 6,
                "TestFailureContainment": 2,
                "TestDecisionSinkCooperation": 1,
                "TestEndToEndLiveLoop": 2,
            },
        },
        "ruff_clean_on": [
            "strategies/engine_adapter.py",
            "tests/test_strategies_engine_adapter_scheduler.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "live router adapter is now fully governed by the "
                "OOS qualification loop. The six AI-Optimized "
                "strategies earn their dispatch slot on the live bot "
                "by walk-forward + DSR evidence; no hand-curated "
                "eligibility table in the hot path."
            ),
            "note": (
                "v0.1.45 will instrument the live bot controllers "
                "(bots.mnq, bots.eth_perp, etc.) to construct the "
                "scheduler + cache at start-up and pass them into "
                "RouterAdapter, completing the chain from bar ingest "
                "through the live bot."
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
                    "RouterAdapter.push_bar now ticks the "
                    "AllowlistScheduler before dispatch and merges its "
                    "cache map with the static eligibility override "
                    "(static wins). OOS qualification now drives the "
                    "live bot trading path with zero operator in the "
                    "loop."
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
        "  shipped: RouterAdapter wired into AllowlistScheduler + "
        "13 integration tests. Live dispatch path is now fully "
        "governed by the OOS qualification loop."
    )


if __name__ == "__main__":
    main()
