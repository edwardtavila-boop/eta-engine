"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_live_adapter.

Factory-level tests for v0.1.45. Verifies
:func:`strategies.live_adapter.build_live_adapter` returns a
:class:`RouterAdapter` with:

  * a :class:`RuntimeAllowlistCache` using the configured TTL.
  * an :class:`AllowlistScheduler` with the configured
    :class:`RefreshTrigger`.
  * ``allowlist_scheduler`` already plugged in so
    :meth:`RouterAdapter.push_bar` ticks the scheduler before
    :func:`dispatch`.
  * sane live defaults (buffer = 300, TTL = 7200s, refresh = 288
    bars or 3600s, warmup = 200 bars).

End-to-end tests use a stub qualifier forwarded through
``scheduler_kwargs`` so we exercise the real tick + cache
refresh path without a real walk-forward run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.strategies.allowlist_scheduler import (
    AllowlistScheduler,
    RefreshTrigger,
)
from eta_engine.strategies.engine_adapter import (
    DEFAULT_BUFFER_BARS,
    RouterAdapter,
)
from eta_engine.strategies.live_adapter import (
    DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST,
    DEFAULT_LIVE_REFRESH_EVERY_N_BARS,
    DEFAULT_LIVE_REFRESH_EVERY_SECONDS,
    DEFAULT_LIVE_TTL_SECONDS,
    build_live_adapter,
)
from eta_engine.strategies.models import Bar, StrategyId
from eta_engine.strategies.oos_qualifier import (
    DEFAULT_QUALIFICATION_GATE,
    QualificationReport,
    StrategyQualification,
)
from eta_engine.strategies.runtime_allowlist import RuntimeAllowlistCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(n: int, *, start_price: float = 100.0) -> list[Bar]:
    return [
        Bar(
            ts=i,
            open=start_price + i * 0.1,
            high=start_price + i * 0.1 + 0.5,
            low=start_price + i * 0.1 - 0.5,
            close=start_price + i * 0.1 + 0.25,
            volume=1000.0,
        )
        for i in range(n)
    ]


def _dict_bar(i: int, *, price: float = 100.0) -> dict[str, float]:
    return {
        "open": price,
        "high": price + 0.5,
        "low": price - 0.5,
        "close": price + 0.25,
        "volume": 1000.0,
        "ts": float(i),
    }


def _make_clock(
    start: datetime,
    step: timedelta,
) -> tuple[list[datetime], callable]:
    times: list[datetime] = [start]

    def clock() -> datetime:
        current = times[-1]
        times.append(current + step)
        return current

    return times, clock


def _stub_report(
    asset: str,
    passing: tuple[StrategyId, ...],
) -> QualificationReport:
    """Build a minimal QualificationReport whose `passing_strategies`
    property returns `passing`. We synthesize one StrategyQualification
    per strategy in `passing` with `passes_gate=True`."""
    qualifications = tuple(
        StrategyQualification(
            strategy=sid,
            asset=asset,
            n_windows=1,
            avg_is_sharpe=1.5,
            avg_oos_sharpe=1.2,
            avg_degradation_pct=0.1,
            dsr=0.7,
            n_trades_is_total=40,
            n_trades_oos_total=30,
            passes_gate=True,
        )
        for sid in passing
    )
    return QualificationReport(
        asset=asset,
        gate=DEFAULT_QUALIFICATION_GATE,
        n_windows_requested=1,
        n_windows_executed=1,
        per_window=(),
        qualifications=qualifications,
    )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestLiveDefaults:
    def test_ttl_seconds_is_seven_thousand_two_hundred(self) -> None:
        assert DEFAULT_LIVE_TTL_SECONDS == 7200.0

    def test_refresh_every_n_bars_is_288(self) -> None:
        assert DEFAULT_LIVE_REFRESH_EVERY_N_BARS == 288

    def test_refresh_every_seconds_is_3600(self) -> None:
        assert DEFAULT_LIVE_REFRESH_EVERY_SECONDS == 3600.0

    def test_min_bars_before_first_is_200(self) -> None:
        assert DEFAULT_LIVE_MIN_BARS_BEFORE_FIRST == 200

    def test_ttl_is_at_least_twice_the_seconds_trigger(self) -> None:
        """TTL must be >= 2x the seconds trigger so the cache stays
        fresh between scheduler ticks on a normally-ticking bot."""
        assert DEFAULT_LIVE_TTL_SECONDS >= 2.0 * DEFAULT_LIVE_REFRESH_EVERY_SECONDS


# ---------------------------------------------------------------------------
# Factory shape
# ---------------------------------------------------------------------------


class TestFactoryShape:
    def test_returns_router_adapter(self) -> None:
        adapter = build_live_adapter("MNQ")
        assert isinstance(adapter, RouterAdapter)

    def test_asset_is_uppercased(self) -> None:
        adapter = build_live_adapter("mnq")
        assert adapter.asset == "MNQ"

    def test_default_buffer_bars_matches_adapter_default(self) -> None:
        adapter = build_live_adapter("MNQ")
        assert adapter.max_bars == DEFAULT_BUFFER_BARS

    def test_scheduler_is_wired_in(self) -> None:
        adapter = build_live_adapter("MNQ")
        assert adapter.allowlist_scheduler is not None
        assert isinstance(adapter.allowlist_scheduler, AllowlistScheduler)

    def test_cache_is_reachable_via_scheduler(self) -> None:
        adapter = build_live_adapter("MNQ")
        assert adapter.allowlist_scheduler is not None
        assert isinstance(
            adapter.allowlist_scheduler.cache,
            RuntimeAllowlistCache,
        )


# ---------------------------------------------------------------------------
# Knob forwarding
# ---------------------------------------------------------------------------


class TestKnobForwarding:
    def test_ttl_seconds_forwarded_to_cache(self) -> None:
        adapter = build_live_adapter("MNQ", ttl_seconds=60.0)
        assert adapter.allowlist_scheduler is not None
        assert adapter.allowlist_scheduler.cache.ttl_seconds == 60.0

    def test_buffer_bars_forwarded(self) -> None:
        adapter = build_live_adapter("MNQ", buffer_bars=50)
        assert adapter.max_bars == 50

    def test_refresh_cadence_forwarded_to_trigger(self) -> None:
        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=10,
            refresh_every_seconds=5.0,
            min_bars_before_first=2,
        )
        assert adapter.allowlist_scheduler is not None
        trg = adapter.allowlist_scheduler.trigger
        assert isinstance(trg, RefreshTrigger)
        assert trg.every_n_bars == 10
        assert trg.every_seconds == 5.0
        assert trg.min_bars_before_first == 2

    def test_base_eligibility_forwarded_to_cache(self) -> None:
        custom = {"MNQ": (StrategyId.MTF_TREND_FOLLOWING,)}
        adapter = build_live_adapter("MNQ", base_eligibility=custom)
        assert adapter.allowlist_scheduler is not None
        assert adapter.allowlist_scheduler.cache.base_eligibility is custom

    def test_eligibility_override_forwarded_to_adapter(self) -> None:
        override = {"MNQ": (StrategyId.MTF_TREND_FOLLOWING,)}
        adapter = build_live_adapter("MNQ", eligibility_override=override)
        assert adapter.eligibility == override

    def test_kill_switch_active_forwarded(self) -> None:
        adapter = build_live_adapter("MNQ", kill_switch_active=True)
        assert adapter.kill_switch_active is True

    def test_session_allows_entries_forwarded(self) -> None:
        adapter = build_live_adapter("MNQ", session_allows_entries=False)
        assert adapter.session_allows_entries is False

    def test_scheduler_kwargs_forwarded(self) -> None:
        kwargs = {"gate": "relaxed"}
        adapter = build_live_adapter("MNQ", scheduler_kwargs=kwargs)
        assert adapter.scheduler_kwargs == kwargs

    def test_clock_forwarded_to_both_scheduler_and_cache(self) -> None:
        _, clock = _make_clock(
            datetime(2026, 4, 17, 0, 0, 0, tzinfo=UTC),
            timedelta(seconds=1),
        )
        adapter = build_live_adapter("MNQ", clock=clock)
        assert adapter.allowlist_scheduler is not None
        # Both should share the SAME injected callable.
        assert adapter.allowlist_scheduler.clock is clock
        assert adapter.allowlist_scheduler.cache.clock is clock


class TestTriggerNoneDisablesAxis:
    def test_bar_only_trigger(self) -> None:
        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=10,
            refresh_every_seconds=None,
        )
        assert adapter.allowlist_scheduler is not None
        trg = adapter.allowlist_scheduler.trigger
        assert trg.every_n_bars == 10
        assert trg.every_seconds is None

    def test_time_only_trigger(self) -> None:
        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=None,
            refresh_every_seconds=5.0,
        )
        assert adapter.allowlist_scheduler is not None
        trg = adapter.allowlist_scheduler.trigger
        assert trg.every_n_bars is None
        assert trg.every_seconds == 5.0


class TestTriggerValidation:
    def test_rejects_both_triggers_none(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            build_live_adapter(
                "MNQ",
                refresh_every_n_bars=None,
                refresh_every_seconds=None,
            )


# ---------------------------------------------------------------------------
# End-to-end: factory adapter dispatches and scheduler ticks
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    def test_push_bar_does_not_crash_on_warmup(self) -> None:
        """Warmup guard -> scheduler returns None, dispatch still runs."""
        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=5,
            refresh_every_seconds=None,
            min_bars_before_first=10,
        )
        # A handful of bars, not enough to clear warmup.
        for i in range(3):
            adapter.push_bar(_dict_bar(i))
        assert adapter.buffered_count == 3
        # The scheduler bookkeeping stays empty because warmup blocked
        # every tick.
        assert adapter.allowlist_scheduler is not None
        assert adapter.allowlist_scheduler.tracked_assets() == ()

    def test_scheduler_fires_once_warmup_clears(self) -> None:
        """Feed enough bars that the scheduler actually refreshes."""
        passing = (StrategyId.MTF_TREND_FOLLOWING,)

        def stub_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            return _stub_report(asset, passing)

        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=3,
            refresh_every_seconds=None,
            min_bars_before_first=3,
            scheduler_kwargs={"qualifier": stub_qualifier},
        )
        # scheduler_kwargs of `qualifier=` is forwarded via tick ->
        # _do_refresh and replaces the default qualify_strategies
        # implementation.
        for i in range(5):
            adapter.push_bar(_dict_bar(i))
        assert adapter.allowlist_scheduler is not None
        cache_map = adapter.allowlist_scheduler.cache.as_eligibility_map()
        assert "MNQ" in cache_map
        # Allowed list is intersection of passing + base eligibility.
        # DEFAULT_ELIGIBILITY for MNQ includes MTF_TREND_FOLLOWING.
        assert StrategyId.MTF_TREND_FOLLOWING in cache_map["MNQ"]

    def test_cache_map_drives_effective_eligibility(self) -> None:
        passing = (StrategyId.MTF_TREND_FOLLOWING,)

        def stub_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            return _stub_report(asset, passing)

        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=2,
            refresh_every_seconds=None,
            min_bars_before_first=2,
            scheduler_kwargs={"qualifier": stub_qualifier},
        )
        for i in range(4):
            adapter.push_bar(_dict_bar(i))
        effective = adapter._effective_eligibility()
        assert effective is not None
        # Only MTF_TREND_FOLLOWING should survive -- everything else
        # in the base eligibility was not in the passing set.
        assert effective["MNQ"] == (StrategyId.MTF_TREND_FOLLOWING,)

    def test_static_override_wins_over_scheduler(self) -> None:
        """Operator-supplied eligibility wins on conflict."""
        static_override = {
            "MNQ": (StrategyId.FVG_FILL_CONFLUENCE,),
        }

        def stub_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            return _stub_report(asset, (StrategyId.MTF_TREND_FOLLOWING,))

        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=2,
            refresh_every_seconds=None,
            min_bars_before_first=2,
            eligibility_override=static_override,
            scheduler_kwargs={"qualifier": stub_qualifier},
        )
        for i in range(4):
            adapter.push_bar(_dict_bar(i))
        effective = adapter._effective_eligibility()
        assert effective is not None
        assert effective["MNQ"] == (StrategyId.FVG_FILL_CONFLUENCE,)


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    def test_qualifier_exception_does_not_crash_push_bar(self) -> None:
        def bad_qualifier(
            bars: list[Bar],
            asset: str,
            **_: object,
        ) -> QualificationReport:
            msg = "synthetic qualifier failure"
            raise RuntimeError(msg)

        adapter = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=2,
            refresh_every_seconds=None,
            min_bars_before_first=2,
            scheduler_kwargs={"qualifier": bad_qualifier},
        )
        # Should swallow the qualifier exception and keep dispatching.
        for i in range(4):
            adapter.push_bar(_dict_bar(i))
        # Cache is empty because every refresh attempt failed.
        assert adapter.allowlist_scheduler is not None
        assert adapter.allowlist_scheduler.cache.as_eligibility_map() == {}
        # Effective eligibility falls back to the static override
        # (which we didn't set -> None -> DEFAULT_ELIGIBILITY in dispatch).
        assert adapter._effective_eligibility() is None


# ---------------------------------------------------------------------------
# Bot integration: MnqBot auto-wires an adapter at start()
# ---------------------------------------------------------------------------


class TestMnqBotAutoWire:
    @pytest.mark.asyncio
    async def test_auto_wire_builds_adapter(self) -> None:
        from eta_engine.bots.mnq.bot import MnqBot

        bot = MnqBot(
            auto_wire_ai_strategies=True,
            ai_strategy_config={
                "refresh_every_n_bars": 10,
                "refresh_every_seconds": None,
                "min_bars_before_first": 2,
            },
        )
        assert bot._strategy_adapter is None  # not built yet
        await bot.start()
        assert bot._strategy_adapter is not None
        assert bot._strategy_adapter.asset == "MNQ"
        assert bot._strategy_adapter.allowlist_scheduler is not None

    @pytest.mark.asyncio
    async def test_auto_wire_skipped_when_adapter_provided(self) -> None:
        from eta_engine.bots.mnq.bot import MnqBot

        preset = build_live_adapter(
            "MNQ",
            refresh_every_n_bars=5,
            refresh_every_seconds=None,
            min_bars_before_first=2,
        )
        bot = MnqBot(
            auto_wire_ai_strategies=True,
            strategy_adapter=preset,
        )
        await bot.start()
        # Operator-provided adapter must not be replaced.
        assert bot._strategy_adapter is preset

    @pytest.mark.asyncio
    async def test_auto_wire_disabled_by_default(self) -> None:
        from eta_engine.bots.mnq.bot import MnqBot

        bot = MnqBot()
        await bot.start()
        assert bot._strategy_adapter is None


# ---------------------------------------------------------------------------
# Bot integration: EthPerpBot auto-wires under the ETH asset name
# ---------------------------------------------------------------------------


class TestEthPerpBotAutoWire:
    @pytest.mark.asyncio
    async def test_auto_wire_strips_usdt_suffix(self) -> None:
        from eta_engine.bots.eth_perp.bot import EthPerpBot

        bot = EthPerpBot(
            auto_wire_ai_strategies=True,
            ai_strategy_config={
                "refresh_every_n_bars": 5,
                "refresh_every_seconds": None,
                "min_bars_before_first": 2,
            },
        )
        await bot.start()
        assert bot._strategy_adapter is not None
        # ETHUSDT -> ETH (upper-cased inside adapter)
        assert bot._strategy_adapter.asset == "ETH"

    @pytest.mark.asyncio
    async def test_auto_wire_respects_operator_override(self) -> None:
        from eta_engine.bots.eth_perp.bot import EthPerpBot

        preset = build_live_adapter(
            "ETH",
            refresh_every_n_bars=5,
            refresh_every_seconds=None,
            min_bars_before_first=2,
        )
        bot = EthPerpBot(
            auto_wire_ai_strategies=True,
            strategy_adapter=preset,
        )
        await bot.start()
        assert bot._strategy_adapter is preset
