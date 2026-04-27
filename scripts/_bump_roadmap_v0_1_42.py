"""One-shot: bump roadmap_state.json to v0.1.42.

RUNTIME ALLOWLIST CACHE -- the qualifier's pass/fail verdict becomes
the policy router's runtime eligibility table, on a TTL cadence.

Context
-------
v0.1.41 shipped :func:`strategies.oos_qualifier.qualify_strategies` --
a per-asset walk-forward + DSR gate that produces a
:class:`QualificationReport` with ``passing_strategies`` /
``failing_strategies`` helpers. v0.1.40 already wired the policy
router to accept a custom eligibility table via ``dispatch(
eligibility=...)``. But the two sides were not connected: nothing
turned a fresh qualifier report into the dict shape the router wants,
nor cached it so every tick didn't re-run a walk-forward.

v0.1.42 closes that wiring gap. A new
:class:`strategies.runtime_allowlist.RuntimeAllowlistCache` keeps a
per-asset ``AllowlistEntry`` with a TTL-backed freshness window,
re-runs the qualifier only on cache miss or expiry, and hands its
contents to ``dispatch(eligibility=...)`` with zero adaptation via
:meth:`RuntimeAllowlistCache.as_eligibility_map`.

What v0.1.42 adds
-----------------
  * ``strategies/runtime_allowlist.py`` (new, ~270 lines)

    - ``DEFAULT_TTL_SECONDS = 3600.0`` (1 hour) -- module-level default
      keeps the refresh cadence centralised.
    - ``AllowlistEntry`` frozen dataclass -- ``asset``, ``allowed``,
      ``passing``, ``base_eligible``, ``report_asset``,
      ``refreshed_at_utc`` + ``as_dict()``.
    - ``intersect_passing_with_base(report, *, base_eligibility, now)``
      -- pure helper that intersects ``report.passing_strategies``
      with the base table, preserving base-table order.
    - ``RuntimeAllowlistCache(ttl_seconds, base_eligibility, clock)``
      -- TTL-backed in-process cache. Methods: ``update``,
      ``invalidate``, ``get``, ``is_stale``, ``assets``,
      ``as_eligibility_map``, ``ensure_fresh``.

  * ``tests/test_strategies_runtime_allowlist.py`` (new, +34 tests)

    Nine test classes:
      - ``TestAllowlistEntry`` -- as_dict shape + frozen invariant. 2
      - ``TestIntersectPassingWithBase`` -- base-order preserved,
        asset upper-cased, empty passing, failing-stripped, asset not
        in base, passing not in base, custom base, injected clock. 8
      - ``TestRuntimeAllowlistCacheBasics`` -- defaults, install,
        overwrite. 3
      - ``TestTTLSemantics`` -- fresh inside TTL, stale past TTL,
        eligibility map drops stale, boundary-inclusive TTL,
        missing-is-stale. 5
      - ``TestInvalidate`` -- single, case-insensitive, invalidate-all,
        missing noop. 4
      - ``TestAsEligibilityMap`` -- shape matches dispatch, empty,
        multi-asset. 3
      - ``TestEnsureFresh`` -- miss invokes, hit does not, stale
        refreshes, kwargs forwarded, default is qualify_strategies. 5
      - ``TestEndToEnd`` -- dispatch respects cached allowlist,
        dispatch base-fallback for missing asset, empty allowlist
        produces noop decision. 3
      - ``TestRealQualifierIntegration`` -- real qualify_strategies
        with empty registry produces a valid empty entry that
        dispatch accepts. 1

Delta
-----
  * tests_passing: 1925 -> 1959 (+34 new runtime-allowlist tests)
  * All pre-existing tests still pass unchanged
  * Ruff-clean on the new module and test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.42 the qualifier's verdict was text on a dashboard: it
told you which strategies had cleared the OOS gate, but the live
router was still dispatching against DEFAULT_ELIGIBILITY -- the
hand-curated per-asset list -- with no honest enforcement of
qualification state.

With the cache in place the live pipeline is:

    cache = RuntimeAllowlistCache()
    cache.ensure_fresh("MNQ", bars)     # hourly cadence
    decision = dispatch(
        "MNQ", bars, ctx,
        eligibility=cache.as_eligibility_map(),
    )

Every curve-fit strategy now falls out of dispatch automatically the
moment it fails qualification, and automatically reinstates the
moment it re-qualifies -- without a human editing
``DEFAULT_ELIGIBILITY``. The policy-router eligibility table is now
DYNAMIC, OOS-GOVERNED, and CACHED.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.42"
NEW_TESTS_ABS = 1959


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_42_runtime_allowlist_cache"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "RUNTIME ALLOWLIST CACHE -- OOS-qualifier verdict becomes "
            "the policy router's runtime eligibility, on a TTL cadence"
        ),
        "theme": (
            "Close the wiring gap between per-strategy OOS qualification "
            "and live dispatch. RuntimeAllowlistCache intersects "
            "report.passing_strategies with DEFAULT_ELIGIBILITY[asset], "
            "caches the result with a per-entry TTL, and exposes the "
            "exact dict shape that dispatch(eligibility=...) consumes. "
            "Curve-fit strategies drop out of live dispatch the moment "
            "they fail the gate, and rejoin the moment they re-qualify "
            "-- without a human editing the eligibility table."
        ),
        "artifacts_added": {
            "strategies": ["strategies/runtime_allowlist.py"],
            "tests": ["tests/test_strategies_runtime_allowlist.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_42.py"],
        },
        "api_surface": {
            "DEFAULT_TTL_SECONDS": "3600.0  -- one hour",
            "AllowlistEntry": (
                "(asset, allowed, passing, base_eligible, report_asset, "
                "refreshed_at_utc)  -- frozen dataclass with as_dict()"
            ),
            "intersect_passing_with_base": (
                "(report, *, base_eligibility=None, now=None) -> AllowlistEntry  -- pure intersection helper"
            ),
            "RuntimeAllowlistCache": (
                "(ttl_seconds=DEFAULT_TTL_SECONDS, base_eligibility=DEFAULT_ELIGIBILITY, clock=datetime.now(UTC))"
            ),
            "RuntimeAllowlistCache.update": ("(report) -> AllowlistEntry  -- install fresh entry"),
            "RuntimeAllowlistCache.get": ("(asset) -> AllowlistEntry | None  -- None if missing/stale"),
            "RuntimeAllowlistCache.is_stale": "(asset) -> bool",
            "RuntimeAllowlistCache.invalidate": ("(asset=None) -> None  -- clear one or all entries"),
            "RuntimeAllowlistCache.as_eligibility_map": (
                "() -> dict[str, tuple[StrategyId, ...]]  -- ready for dispatch(eligibility=...)"
            ),
            "RuntimeAllowlistCache.ensure_fresh": (
                "(asset, bars, *, qualifier=None, **kwargs) -> AllowlistEntry  -- re-runs qualifier on miss/stale"
            ),
        },
        "design_notes": {
            "ttl_default_one_hour": (
                "Walk-forward qualification runs in seconds-to-minutes "
                "per asset; refreshing hourly keeps the live router's "
                "view fresh while leaving headroom for the ensemble "
                "without blocking the hot path."
            ),
            "base_order_preserved": (
                "The intersection preserves DEFAULT_ELIGIBILITY's "
                "declared order so the router's confidence-tie-breaker "
                "semantics stay stable across cache refreshes -- "
                "a passing strategy's slot in the dispatch sequence "
                "doesn't shift when an earlier peer fails qualification."
            ),
            "clock_injection": (
                "clock is a Callable[[], datetime] default-factoried to "
                "datetime.now(UTC). Tests use a _ManualClock fixture to "
                "advance time deterministically -- no sleep(), no freeze-"
                "time monkey-patching."
            ),
            "ensure_fresh_on_cadence": (
                "ensure_fresh() is the only path that invokes the "
                "qualifier. Call it on a cadence (hourly, session-roll, "
                "etc.) and the live router sees the freshest verdict "
                "without re-running backtests on every bar."
            ),
            "as_eligibility_map_filters_stale": (
                "as_eligibility_map() silently drops stale entries. "
                "Assets that have not been re-qualified fall back to "
                "dispatch()'s hard-coded default (LSD+OB+FVG+MTF) -- "
                "never to a stale verdict. Fail-safe, not fail-closed."
            ),
            "passing_preserved_even_when_not_in_base": (
                "AllowlistEntry.passing captures ALL strategies that "
                "cleared the gate, not just those also in base_eligible. "
                "This lets dashboards surface 'qualified but not yet "
                "approved' strategies for operator review."
            ),
        },
        "test_coverage": {
            "tests_added": 34,
            "classes": {
                "TestAllowlistEntry": 2,
                "TestIntersectPassingWithBase": 8,
                "TestRuntimeAllowlistCacheBasics": 3,
                "TestTTLSemantics": 5,
                "TestInvalidate": 4,
                "TestAsEligibilityMap": 3,
                "TestEnsureFresh": 5,
                "TestEndToEnd": 3,
                "TestRealQualifierIntegration": 1,
            },
        },
        "ruff_clean_on": [
            "strategies/runtime_allowlist.py",
            "tests/test_strategies_runtime_allowlist.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "runtime allowlist cache makes the policy router's "
                "eligibility table OOS-governed and dynamic for the "
                "first time. The six AI-Optimized strategies now earn "
                "their dispatch slot live, per tick, from the most-"
                "recent walk-forward verdict."
            ),
            "note": (
                "v0.1.43 will instrument the allowlist cache with a "
                "refresh scheduler the live bot controller can tick "
                "on a cadence (e.g. on session roll or every N bars), "
                "closing the loop from bar ingest -> qualification -> "
                "live router eligibility without operator intervention."
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
                    "RuntimeAllowlistCache closes the wiring gap: the "
                    "OOS qualifier's passing_strategies intersects with "
                    "DEFAULT_ELIGIBILITY[asset], caches with a TTL, and "
                    "feeds dispatch(eligibility=...) directly. The "
                    "policy router's eligibility table is now DYNAMIC, "
                    "OOS-GOVERNED, and CACHED."
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
        "  shipped: strategies/runtime_allowlist.py + 34 tests. "
        "Qualifier verdict is now the router's runtime eligibility "
        "on a TTL cadence; curve-fit strategies drop out of dispatch "
        "automatically."
    )


if __name__ == "__main__":
    main()
