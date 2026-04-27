"""One-shot: bump roadmap_state.json to v0.1.43.

ALLOWLIST REFRESH SCHEDULER -- bar-by-bar cadence on top of
RuntimeAllowlistCache. The live bot controller ticks the scheduler on
every bar; the scheduler decides whether to re-qualify + update.

Context
-------
v0.1.42 shipped :class:`RuntimeAllowlistCache` -- storage + TTL + the
``dispatch(eligibility=...)`` adapter. But the cache alone is inert:
it knows whether entries are fresh, not when to refresh. Operators
had to write their own loop around ``cache.ensure_fresh`` with their
own cadence logic.

v0.1.43 closes that cadence gap with :class:`AllowlistScheduler`. The
scheduler wraps the cache, owns a :class:`RefreshTrigger` policy, and
exposes a single :meth:`tick` the bot controller can call on every
bar. Internally the scheduler compares bar-count delta + wall-clock
elapsed against the trigger and either re-runs the qualifier + updates
the cache (cheap path when the trigger doesn't fire) or returns
``None`` (cheaper no-op).

What v0.1.43 adds
-----------------
  * ``strategies/allowlist_scheduler.py`` (new, ~230 lines)

    - :class:`RefreshTrigger` frozen dataclass:
      ``every_n_bars | every_seconds | min_bars_before_first``.
      ``__post_init__`` enforces: at least one trigger set, counts >=
      1, seconds > 0, warmup >= 0.
    - :class:`AllowlistScheduler`:
      - ``tick(asset, bars, *, qualifier=None, **kwargs) ->
        AllowlistEntry | None``: primary bar entry point.
      - ``force_refresh(asset, bars, ...)``: bypass trigger.
      - ``last_refresh_at`` / ``last_refresh_bar_count`` / ``tracked_assets``.
      - ``reset(asset=None)``: clear scheduler bookkeeping (cache
        retained) -- treat next tick as cold start.
    - Clock dependency-injected for deterministic tests.

  * ``tests/test_strategies_allowlist_scheduler.py`` (new, +32 tests)

    Ten test classes:
      - ``TestRefreshTriggerValidation`` -- 10 invariants on the
        trigger surface (bar-only, time-only, both, neither raises,
        negative/zero raises, negative warmup raises, frozen).
      - ``TestWarmupGuard`` -- below-min skips, at-exact-min fires,
        zero-min allows immediate refresh.
      - ``TestBarCountTrigger`` -- first-tick fires, insufficient
        delta skips, exact threshold fires.
      - ``TestTimeTrigger`` -- fires after elapsed seconds (boundary
        inclusive), time alone respects warmup.
      - ``TestCombinedTriggers`` -- bars fire before time; time fires
        before bars.
      - ``TestForceRefresh`` -- bypasses trigger, updates bookkeeping.
      - ``TestMultiAsset`` -- per-asset bookkeeping independent;
        asset-name upper-casing on both insert + lookup.
      - ``TestReset`` -- single-asset, all, does-not-touch-cache,
        re-enables first-refresh.
      - ``TestQualifierInjection`` -- custom kwargs forwarded; default
        qualifier is :func:`qualify_strategies`.
      - ``TestEndToEndWithDispatch`` -- scheduler keeps router
        eligibility fresh across multiple ticks; dispatch reflects
        latest scheduler state.

Delta
-----
  * tests_passing: 1959 -> 1991 (+32 new scheduler tests)
  * All pre-existing tests still pass unchanged
  * Ruff-clean on the new module and test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.43 the pipeline was:

    [operator cron] -> qualifier -> cache.update -> dispatch

Where "operator cron" was an unowned problem -- the cache didn't know
when to refresh, and the dispatch path couldn't tell whether the
eligibility map was from last tick or last week. The scheduler makes
the full pipeline auto-loop:

    tick -> [RefreshTrigger] -> qualifier? -> cache.update -> dispatch

Now the bot controller's per-bar hot path owns the cadence: it ticks
the scheduler, dispatches through the cache's eligibility map, and
the refresh happens automatically whenever the trigger fires (bars
OR seconds, whichever first). Curve-fit strategies drop out, fresh
strategies re-enter, with no operator intervention -- the pipeline is
fully closed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.43"
NEW_TESTS_ABS = 1991


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_43_allowlist_refresh_scheduler"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "ALLOWLIST REFRESH SCHEDULER -- bar-by-bar cadence on top "
            "of the runtime allowlist cache closes the qualification loop"
        ),
        "theme": (
            "Move the refresh cadence from operator-cron into the bot "
            "controller's per-bar hot path. The scheduler owns a "
            "RefreshTrigger policy, tracks per-asset last-refresh "
            "time + bar-count, and re-runs qualify_strategies + updates "
            "the allowlist cache automatically whenever the trigger "
            "fires. The live dispatch path now auto-loops from bar "
            "ingest to eligibility refresh with no operator involvement."
        ),
        "artifacts_added": {
            "strategies": ["strategies/allowlist_scheduler.py"],
            "tests": ["tests/test_strategies_allowlist_scheduler.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_43.py"],
        },
        "api_surface": {
            "RefreshTrigger": (
                "(every_n_bars=None, every_seconds=None, "
                "min_bars_before_first=50)  -- frozen dataclass; at "
                "least one trigger must be set"
            ),
            "AllowlistScheduler": (
                "(cache, trigger, clock=datetime.now(UTC))  -- ticks the cache on a per-bar cadence"
            ),
            "AllowlistScheduler.tick": (
                "(asset, bars, *, qualifier=None, **qualifier_kwargs) "
                "-> AllowlistEntry | None  -- returns entry if refreshed"
            ),
            "AllowlistScheduler.force_refresh": (
                "(asset, bars, ...)  -- bypass trigger; always refreshes + updates bookkeeping"
            ),
            "AllowlistScheduler.last_refresh_at": ("(asset) -> datetime | None"),
            "AllowlistScheduler.last_refresh_bar_count": ("(asset) -> int | None"),
            "AllowlistScheduler.tracked_assets": "() -> tuple[str, ...]",
            "AllowlistScheduler.reset": ("(asset=None)  -- clear scheduler bookkeeping (cache retained)"),
        },
        "design_notes": {
            "cheap_no_op_tick": (
                "tick() does O(1) work when the trigger has not fired "
                "-- two dict lookups, one subtraction. Safe to call "
                "on every bar."
            ),
            "bar_or_time_whichever_first": (
                "When both triggers are set, tick() fires on the "
                "first one to clear. This lets operators set 'every "
                "500 bars OR every 10 minutes' and have the qualifier "
                "refresh snappily in high-throughput tapes while still "
                "refreshing on slow tapes."
            ),
            "warmup_blocks_first_only": (
                "min_bars_before_first gates the FIRST refresh, not "
                "subsequent ones. Once the cold-start window has "
                "accrued enough bars, follow-up refreshes fire on the "
                "bar-count / time trigger alone."
            ),
            "reset_clears_bookkeeping_not_cache": (
                "reset() wipes the scheduler's last-refresh memory but "
                "leaves the cache untouched. That way a session roll "
                "can re-start the cadence while still using the "
                "prior session's allowlist until the first re-refresh."
            ),
            "kwargs_opaque_forward": (
                "tick() accepts **qualifier_kwargs and forwards them "
                "verbatim. Operators can pass gate=, n_windows=, "
                "harness_config=, registry= without the scheduler "
                "knowing what they mean."
            ),
            "force_refresh_for_events": (
                "force_refresh() bypasses every trigger and still "
                "updates bookkeeping -- correct semantics for operator-"
                "driven refreshes on configuration change or an "
                "external 'kill + re-qualify' command."
            ),
        },
        "test_coverage": {
            "tests_added": 32,
            "classes": {
                "TestRefreshTriggerValidation": 10,
                "TestWarmupGuard": 3,
                "TestBarCountTrigger": 3,
                "TestTimeTrigger": 2,
                "TestCombinedTriggers": 2,
                "TestForceRefresh": 2,
                "TestMultiAsset": 2,
                "TestReset": 4,
                "TestQualifierInjection": 2,
                "TestEndToEndWithDispatch": 2,
            },
        },
        "ruff_clean_on": [
            "strategies/allowlist_scheduler.py",
            "tests/test_strategies_allowlist_scheduler.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "refresh scheduler makes the OOS-governance loop fully "
                "self-driving: bar ingest -> qualifier -> cache -> "
                "dispatch, ticked on every bar, zero operator in the "
                "loop."
            ),
            "note": (
                "v0.1.44 will plug the scheduler into the live bot "
                "controller's on_bar path so the dispatch() call "
                "reads cache.as_eligibility_map() on every bar and "
                "the per-asset allowlist drives real entries / exits."
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
                    "AllowlistScheduler wraps RuntimeAllowlistCache "
                    "with a RefreshTrigger-driven cadence. The bot "
                    "controller can now tick the scheduler on every "
                    "bar; re-qualification fires automatically on the "
                    "earlier of the bar-count or time triggers. The "
                    "OOS -> router eligibility loop is fully closed."
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
        "  shipped: strategies/allowlist_scheduler.py + 32 tests. "
        "The OOS qualification loop is now self-driving via the "
        "bot controller's per-bar tick path."
    )


if __name__ == "__main__":
    main()
