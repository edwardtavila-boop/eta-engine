"""Tests for :mod:`eta_engine.strategies.runtime_allowlist`.

The runtime allowlist cache sits between the OOS qualifier and the
policy router. It owns three responsibilities:

  1. Intersect ``report.passing_strategies`` with the base eligibility
     table, preserving base-table order.
  2. Cache the intersection with a per-entry TTL.
  3. Produce a ``dict[asset, tuple[StrategyId, ...]]`` suitable for
     ``dispatch(eligibility=...)``.

Tests below exercise:

  - :class:`AllowlistEntry` surface (shape + as_dict)
  - Pure intersection invariants (asset upper-casing, order preservation,
    empty passing, no base entry, non-passing stripped)
  - Cache lifecycle: update/get/is_stale/invalidate/assets
  - TTL semantics: fresh -> get returns entry; stale -> get returns None
    AND is_stale is True AND as_eligibility_map drops the asset
  - ``ensure_fresh`` miss-path invokes the qualifier
  - ``ensure_fresh`` hit-path does NOT invoke the qualifier
  - End-to-end: cache + dispatch integration. Build a tape, qualify,
    install, then run :func:`dispatch` with the cache's eligibility
    map and verify the router only dispatches against allowed sids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.strategies.eta_policy import StrategyContext
from eta_engine.strategies.backtest_harness import HarnessConfig
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.oos_qualifier import (
    DEFAULT_QUALIFICATION_GATE,
    QualificationGate,
    QualificationReport,
    StrategyQualification,
    qualify_strategies,
)
from eta_engine.strategies.policy_router import DEFAULT_ELIGIBILITY, dispatch
from eta_engine.strategies.runtime_allowlist import (
    DEFAULT_TTL_SECONDS,
    AllowlistEntry,
    RuntimeAllowlistCache,
    intersect_passing_with_base,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(ts: datetime, px: float) -> Bar:
    return Bar(
        ts=ts,
        open=px,
        high=px + 0.5,
        low=px - 0.5,
        close=px,
        volume=1000.0,
    )


def _quals(*items: tuple[StrategyId, bool]) -> tuple[StrategyQualification, ...]:
    """Build a minimal qualifications tuple for testing.

    Each item is (strategy, passes_gate). All other fields default to
    zeros which is fine because only ``passes_gate``/``strategy`` are
    read by the intersection.
    """
    out: list[StrategyQualification] = []
    for sid, passes in items:
        out.append(
            StrategyQualification(
                strategy=sid,
                asset="MNQ",
                n_windows=1,
                avg_is_sharpe=0.0,
                avg_oos_sharpe=0.0,
                avg_degradation_pct=0.0,
                dsr=0.0,
                n_trades_is_total=0,
                n_trades_oos_total=0,
                passes_gate=passes,
                fail_reasons=() if passes else ("dsr_below_threshold",),
            ),
        )
    return tuple(out)


def _report(
    asset: str,
    *items: tuple[StrategyId, bool],
    gate: QualificationGate | None = None,
) -> QualificationReport:
    return QualificationReport(
        asset=asset,
        gate=gate if gate is not None else DEFAULT_QUALIFICATION_GATE,
        n_windows_requested=1,
        n_windows_executed=1,
        per_window=(),
        qualifications=_quals(*items),
    )


@dataclass
class _ManualClock:
    """A mutable clock with a timestamp that tests can advance."""

    now_: datetime = field(
        default_factory=lambda: datetime(2026, 4, 17, tzinfo=UTC),
    )

    def __call__(self) -> datetime:
        return self.now_

    def advance(self, seconds: float) -> None:
        self.now_ = self.now_ + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# AllowlistEntry
# ---------------------------------------------------------------------------


class TestAllowlistEntry:
    def test_as_dict_round_trip(self) -> None:
        entry = AllowlistEntry(
            asset="MNQ",
            allowed=(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,),
            passing=(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,),
            base_eligible=(
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                StrategyId.OB_BREAKER_RETEST,
            ),
            report_asset="mnq",
            refreshed_at_utc="2026-04-17T00:00:00+00:00",
        )
        d = entry.as_dict()
        assert d["asset"] == "MNQ"
        assert d["allowed"] == [StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT.value]
        assert d["passing"] == [StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT.value]
        assert d["base_eligible"] == [
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT.value,
            StrategyId.OB_BREAKER_RETEST.value,
        ]
        assert d["report_asset"] == "mnq"
        assert d["refreshed_at_utc"] == "2026-04-17T00:00:00+00:00"

    def test_frozen(self) -> None:
        entry = AllowlistEntry(
            asset="MNQ",
            allowed=(),
            passing=(),
            base_eligible=(),
            report_asset="MNQ",
            refreshed_at_utc="2026-04-17T00:00:00+00:00",
        )
        with pytest.raises((AttributeError, TypeError)):
            entry.asset = "BTC"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# intersect_passing_with_base
# ---------------------------------------------------------------------------


class TestIntersectPassingWithBase:
    def test_order_preserved_from_base(self) -> None:
        # Base order: LSD, OB, FVG, MTF
        # Report passes: FVG, LSD (out of order)
        report = _report(
            "MNQ",
            (StrategyId.FVG_FILL_CONFLUENCE, True),
            (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
        )
        entry = intersect_passing_with_base(report)
        # Allowed must follow BASE order: LSD before FVG
        assert entry.allowed == (
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            StrategyId.FVG_FILL_CONFLUENCE,
        )

    def test_asset_upper_cased(self) -> None:
        report = _report(
            "mnq",
            (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
        )
        entry = intersect_passing_with_base(report)
        assert entry.asset == "MNQ"
        # report_asset preserves the original casing
        assert entry.report_asset == "mnq"

    def test_empty_passing_empty_allowed(self) -> None:
        report = _report(
            "MNQ",
            (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, False),
            (StrategyId.OB_BREAKER_RETEST, False),
        )
        entry = intersect_passing_with_base(report)
        assert entry.allowed == ()
        assert entry.passing == ()

    def test_failing_strategies_not_in_allowed(self) -> None:
        report = _report(
            "MNQ",
            (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
            (StrategyId.OB_BREAKER_RETEST, False),
        )
        entry = intersect_passing_with_base(report)
        assert entry.allowed == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)
        # passing tuple only carries strategies that cleared the gate
        assert entry.passing == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)

    def test_asset_not_in_base_yields_empty_allowed(self) -> None:
        report = _report(
            "XAU",  # not in DEFAULT_ELIGIBILITY
            (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
        )
        entry = intersect_passing_with_base(report)
        assert entry.base_eligible == ()
        assert entry.allowed == ()
        # passing is still reported so dashboards can see
        # "passed qualification but not in base table"
        assert entry.passing == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)

    def test_passing_strategy_not_in_base_is_stripped(self) -> None:
        # NQ base = LSD, OB, MTF (no FVG)
        report = _report(
            "NQ",
            (StrategyId.FVG_FILL_CONFLUENCE, True),
            (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
        )
        entry = intersect_passing_with_base(report)
        # FVG is passing but not in NQ base -> not in allowed
        assert StrategyId.FVG_FILL_CONFLUENCE not in entry.allowed
        assert StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT in entry.allowed
        # FVG is still reported in passing for diagnostic visibility
        assert StrategyId.FVG_FILL_CONFLUENCE in entry.passing

    def test_custom_base_eligibility(self) -> None:
        custom = {"XAU": (StrategyId.RL_FULL_AUTOMATION,)}
        report = _report(
            "XAU",
            (StrategyId.RL_FULL_AUTOMATION, True),
        )
        entry = intersect_passing_with_base(report, base_eligibility=custom)
        assert entry.allowed == (StrategyId.RL_FULL_AUTOMATION,)

    def test_injected_now_used_for_stamp(self) -> None:
        stamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        report = _report(
            "MNQ",
            (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
        )
        entry = intersect_passing_with_base(report, now=stamp)
        assert entry.refreshed_at_utc == stamp.isoformat()


# ---------------------------------------------------------------------------
# Cache basics
# ---------------------------------------------------------------------------


class TestRuntimeAllowlistCacheBasics:
    def test_defaults(self) -> None:
        cache = RuntimeAllowlistCache()
        assert cache.ttl_seconds == DEFAULT_TTL_SECONDS
        assert cache.base_eligibility is DEFAULT_ELIGIBILITY
        assert cache.assets() == ()

    def test_update_installs_entry(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        entry = cache.update(
            _report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)),
        )
        assert entry.asset == "MNQ"
        assert cache.assets() == ("MNQ",)
        got = cache.get("MNQ")
        assert got is entry

    def test_update_overwrites_prior_entry(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        second = cache.update(
            _report(
                "MNQ",
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, False),
                (StrategyId.OB_BREAKER_RETEST, True),
            ),
        )
        assert cache.get("MNQ") is second
        assert second.allowed == (StrategyId.OB_BREAKER_RETEST,)


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestTTLSemantics:
    def test_fresh_within_ttl(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(ttl_seconds=100.0, clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        clock.advance(50)
        assert cache.is_stale("MNQ") is False
        assert cache.get("MNQ") is not None

    def test_expired_after_ttl(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(ttl_seconds=100.0, clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        clock.advance(200)
        assert cache.is_stale("MNQ") is True
        assert cache.get("MNQ") is None

    def test_eligibility_map_drops_stale(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(ttl_seconds=100.0, clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        cache.update(_report("BTC", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        clock.advance(50)
        # refresh BTC
        cache.update(_report("BTC", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        clock.advance(75)  # now MNQ is stale (125s since insert), BTC is fresh (75s)
        mp = cache.as_eligibility_map()
        assert "MNQ" not in mp
        assert "BTC" in mp

    def test_boundary_exact_ttl_still_fresh(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(ttl_seconds=100.0, clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        clock.advance(100)  # exactly at the TTL boundary
        # inclusive check: <= ttl_seconds is fresh
        assert cache.is_stale("MNQ") is False
        assert cache.get("MNQ") is not None

    def test_missing_is_stale(self) -> None:
        cache = RuntimeAllowlistCache()
        assert cache.is_stale("MNQ") is True
        assert cache.get("MNQ") is None


# ---------------------------------------------------------------------------
# Invalidate
# ---------------------------------------------------------------------------


class TestInvalidate:
    def test_invalidate_single(self) -> None:
        cache = RuntimeAllowlistCache()
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        cache.update(_report("BTC", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        cache.invalidate("MNQ")
        assert cache.get("MNQ") is None
        assert cache.get("BTC") is not None

    def test_invalidate_case_insensitive(self) -> None:
        cache = RuntimeAllowlistCache()
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        cache.invalidate("mnq")
        assert cache.get("MNQ") is None

    def test_invalidate_all(self) -> None:
        cache = RuntimeAllowlistCache()
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        cache.update(_report("BTC", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        cache.invalidate()
        assert cache.assets() == ()

    def test_invalidate_missing_is_noop(self) -> None:
        cache = RuntimeAllowlistCache()
        cache.invalidate("XAU")  # should not raise
        assert cache.assets() == ()


# ---------------------------------------------------------------------------
# as_eligibility_map
# ---------------------------------------------------------------------------


class TestAsEligibilityMap:
    def test_shape_matches_dispatch_contract(self) -> None:
        cache = RuntimeAllowlistCache()
        cache.update(
            _report(
                "MNQ",
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
                (StrategyId.OB_BREAKER_RETEST, True),
            ),
        )
        mp = cache.as_eligibility_map()
        assert mp == {
            "MNQ": (
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                StrategyId.OB_BREAKER_RETEST,
            ),
        }

    def test_empty_when_no_entries(self) -> None:
        assert RuntimeAllowlistCache().as_eligibility_map() == {}

    def test_multi_asset(self) -> None:
        cache = RuntimeAllowlistCache()
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))
        cache.update(_report("BTC", (StrategyId.OB_BREAKER_RETEST, True)))
        mp = cache.as_eligibility_map()
        assert set(mp) == {"MNQ", "BTC"}
        assert mp["MNQ"] == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)
        assert mp["BTC"] == (StrategyId.OB_BREAKER_RETEST,)


# ---------------------------------------------------------------------------
# ensure_fresh
# ---------------------------------------------------------------------------


class TestEnsureFresh:
    def test_miss_invokes_qualifier(self) -> None:
        calls: list[tuple[str, int]] = []

        def fake_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            calls.append((asset, len(bars)))
            return _report(
                asset,
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
            )

        cache = RuntimeAllowlistCache()
        bars = [_bar(datetime(2026, 4, 17, tzinfo=UTC), 100.0 + i) for i in range(5)]
        entry = cache.ensure_fresh("MNQ", bars, qualifier=fake_qualifier)
        assert entry.asset == "MNQ"
        assert entry.allowed == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)
        assert calls == [("MNQ", 5)]

    def test_hit_does_not_invoke_qualifier(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(ttl_seconds=100.0, clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))

        def should_not_run(*_: object, **__: object) -> QualificationReport:
            msg = "qualifier must not run on cache hit"
            raise AssertionError(msg)

        bars = [_bar(datetime(2026, 4, 17, tzinfo=UTC), 100.0)]
        entry = cache.ensure_fresh("MNQ", bars, qualifier=should_not_run)
        assert entry.asset == "MNQ"

    def test_stale_refreshes(self) -> None:
        clock = _ManualClock()
        cache = RuntimeAllowlistCache(ttl_seconds=10.0, clock=clock)
        cache.update(_report("MNQ", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, False)))
        clock.advance(50)  # entry is now stale

        calls: list[str] = []

        def fake_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            calls.append(asset)
            return _report(
                asset,
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
            )

        bars = [_bar(datetime(2026, 4, 17, tzinfo=UTC), 100.0)]
        entry = cache.ensure_fresh("MNQ", bars, qualifier=fake_qualifier)
        assert calls == ["MNQ"]
        assert entry.allowed == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)

    def test_kwargs_forwarded_to_qualifier(self) -> None:
        captured: dict[str, object] = {}

        def fake_qualifier(
            bars: list[Bar],
            asset: str,
            **kwargs: object,
        ) -> QualificationReport:
            captured.update(kwargs)
            return _report(
                asset,
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
            )

        cache = RuntimeAllowlistCache()
        bars = [_bar(datetime(2026, 4, 17, tzinfo=UTC), 100.0)]
        custom_gate = QualificationGate(
            dsr_threshold=0.1,
            max_degradation_pct=0.9,
            min_trades_per_window=1,
        )
        cache.ensure_fresh(
            "MNQ",
            bars,
            qualifier=fake_qualifier,
            gate=custom_gate,
            n_windows=2,
        )
        assert captured["gate"] is custom_gate
        assert captured["n_windows"] == 2

    def test_default_qualifier_is_qualify_strategies(self) -> None:
        # With real bars + real qualify_strategies, a very relaxed gate
        # and no registry should still produce a report we can cache.
        cache = RuntimeAllowlistCache()
        base_ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(base_ts + timedelta(minutes=i), 100.0) for i in range(80)]
        entry = cache.ensure_fresh(
            "MNQ",
            bars,
            gate=QualificationGate(
                dsr_threshold=-10.0,
                max_degradation_pct=10.0,
                min_trades_per_window=0,
            ),
            n_windows=2,
            registry={},  # no strategies -> empty report, still valid
        )
        # Empty registry produces an empty report, which is still a
        # valid cache entry with empty allowed.
        assert entry.asset == "MNQ"
        assert cache.get("MNQ") is entry


# ---------------------------------------------------------------------------
# End-to-end: cache -> dispatch
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_dispatch_respects_cached_allowlist(self) -> None:
        # Build a report where only LSD passes for MNQ. The base table
        # for MNQ has LSD, OB, FVG, MTF -- the cached allowlist should
        # narrow the router to LSD only.
        cache = RuntimeAllowlistCache()
        cache.update(
            _report(
                "MNQ",
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True),
                (StrategyId.OB_BREAKER_RETEST, False),
                (StrategyId.FVG_FILL_CONFLUENCE, False),
                (StrategyId.MTF_TREND_FOLLOWING, False),
            ),
        )

        calls: list[StrategyId] = []

        def make_fn(sid: StrategyId) -> object:
            def fn(bars: list[Bar], ctx: StrategyContext) -> StrategySignal:
                calls.append(sid)
                return StrategySignal(
                    strategy=sid,
                    side=Side.LONG,
                    confidence=6.0,
                    entry=100.0,
                    stop=99.0,
                    target=102.0,
                    risk_mult=1.0,
                    rationale_tags=(sid.value,),
                )

            return fn

        registry = {
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: make_fn(
                StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            ),
            StrategyId.OB_BREAKER_RETEST: make_fn(StrategyId.OB_BREAKER_RETEST),
            StrategyId.FVG_FILL_CONFLUENCE: make_fn(StrategyId.FVG_FILL_CONFLUENCE),
            StrategyId.MTF_TREND_FOLLOWING: make_fn(StrategyId.MTF_TREND_FOLLOWING),
        }

        base_ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(base_ts + timedelta(minutes=i), 100.0) for i in range(5)]
        ctx = StrategyContext()

        decision = dispatch(
            "MNQ",
            bars,
            ctx,
            eligibility=cache.as_eligibility_map(),
            registry=registry,
        )

        # Only LSD was dispatched; the three non-passing strategies
        # never fired.
        assert calls == [StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT]
        assert decision.eligible == (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,)
        assert decision.winner.strategy == StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT

    def test_dispatch_uses_base_fallback_when_asset_missing_from_map(self) -> None:
        # MNQ was never qualified -> cache has no entry for it.
        # dispatch() should fall back to DEFAULT_ELIGIBILITY for MNQ.
        cache = RuntimeAllowlistCache()
        cache.update(_report("BTC", (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, True)))

        fired: list[StrategyId] = []

        def fn(bars: list[Bar], ctx: StrategyContext) -> StrategySignal:
            fired.append(StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT)
            return StrategySignal(
                strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
                side=Side.FLAT,
                rationale_tags=("noop",),
            )

        registry = {StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn}

        base_ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(base_ts, 100.0)]
        ctx = StrategyContext()

        mp = cache.as_eligibility_map()
        assert "MNQ" not in mp
        # With the cache map alone (MNQ missing) dispatch routes MNQ
        # through its hard-coded fallback (LSD + OB + FVG + MTF). Only
        # LSD is registered so only it fires.
        decision = dispatch("MNQ", bars, ctx, eligibility=mp, registry=registry)
        assert fired == [StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT]
        # eligible reflects the DISPATCH fallback, not the cache map
        assert StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT in decision.eligible

    def test_empty_allowlist_produces_noop_decision(self) -> None:
        # Every strategy fails qualification on MNQ -> empty allowlist.
        cache = RuntimeAllowlistCache()
        cache.update(
            _report(
                "MNQ",
                (StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT, False),
                (StrategyId.OB_BREAKER_RETEST, False),
            ),
        )

        def fn(bars: list[Bar], ctx: StrategyContext) -> StrategySignal:
            msg = "no strategies should be invoked"
            raise AssertionError(msg)

        registry = {
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT: fn,
            StrategyId.OB_BREAKER_RETEST: fn,
        }

        base_ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(base_ts, 100.0)]
        ctx = StrategyContext()

        decision = dispatch(
            "MNQ",
            bars,
            ctx,
            eligibility=cache.as_eligibility_map(),
            registry=registry,
        )
        assert decision.eligible == ()
        assert decision.candidates == ()
        assert decision.winner.is_actionable is False
        assert "no_candidates" in decision.winner.rationale_tags


# ---------------------------------------------------------------------------
# Real qualifier integration (smoke)
# ---------------------------------------------------------------------------


class TestRealQualifierIntegration:
    """End-to-end sanity check: run the real qualifier, cache it, dispatch.

    Uses an empty registry so the qualifier produces an empty report
    and the cache winds up with a valid but empty allowlist for the
    asset. This is the light-touch version of the full pipeline -- it
    proves the two modules wire together with no adapter needed.
    """

    def test_empty_registry_produces_valid_empty_entry(self) -> None:
        base_ts = datetime(2026, 4, 17, tzinfo=UTC)
        bars = [_bar(base_ts + timedelta(minutes=i), 100.0) for i in range(80)]
        report = qualify_strategies(
            bars,
            "MNQ",
            gate=QualificationGate(
                dsr_threshold=-10.0,
                max_degradation_pct=10.0,
                min_trades_per_window=0,
            ),
            n_windows=2,
            harness_config=HarnessConfig(),
            registry={},
        )
        cache = RuntimeAllowlistCache()
        entry = cache.update(report)
        assert entry.asset == "MNQ"
        assert entry.allowed == ()
        # dispatch() with an empty allowed tuple yields a no-candidates
        # decision -- this is the "qualified but no passing strategies"
        # steady state.
        ctx = StrategyContext()
        decision = dispatch(
            "MNQ",
            bars,
            ctx,
            eligibility=cache.as_eligibility_map(),
            registry={},
        )
        assert decision.eligible == ()
        assert decision.candidates == ()
