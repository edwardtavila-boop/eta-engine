"""Tests for :class:`BrokerEquityPoller`.

R1 wiring -- the async→sync bridge that powers
:class:`BrokerEquityReconciler` from a venue adapter's
``get_net_liquidation()`` coroutine.
"""

from __future__ import annotations

import asyncio

import pytest

from eta_engine.core.broker_equity_poller import BrokerEquityPoller
from eta_engine.core.broker_equity_reconciler import BrokerEquityReconciler


@pytest.mark.asyncio
async def test_start_eagerly_fetches_before_returning() -> None:
    calls: list[int] = []

    async def fetch() -> float | None:
        calls.append(1)
        return 50_000.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=5.0,
    )
    await poller.start()
    try:
        assert poller.current() == 50_000.0
        assert poller.fetch_ok == 1
        assert len(calls) == 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_polling_refreshes_on_interval() -> None:
    results: list[float | None] = [50_000.0, 50_100.0, 50_200.0]

    async def fetch() -> float | None:
        return results.pop(0) if results else 50_200.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.02,
        stale_after_s=5.0,
    )
    await poller.start()
    try:
        await asyncio.sleep(0.15)
        assert poller.current() == 50_200.0
        assert poller.fetch_ok >= 3
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_none_result_does_not_poison_cache() -> None:
    idx = [0]

    async def fetch() -> float | None:
        idx[0] += 1
        # First call returns real data, subsequent calls return None
        return 50_000.0 if idx[0] == 1 else None

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.02,
        stale_after_s=5.0,
    )
    await poller.start()
    try:
        await asyncio.sleep(0.1)
        # Cache still serves the last good value
        assert poller.current() == 50_000.0
        assert poller.fetch_ok == 1
        assert poller.fetch_none >= 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_exception_in_fetch_increments_error_counter() -> None:
    idx = [0]

    async def fetch() -> float | None:
        idx[0] += 1
        if idx[0] == 1:
            return 50_000.0
        msg = "backend down"
        raise RuntimeError(msg)

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.02,
        stale_after_s=5.0,
    )
    await poller.start()
    try:
        await asyncio.sleep(0.1)
        assert poller.current() == 50_000.0  # last good value
        assert poller.fetch_error >= 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_stale_cache_returns_none() -> None:
    idx = [0]

    async def fetch() -> float | None:
        idx[0] += 1
        return 50_000.0 if idx[0] == 1 else None

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.02,
        stale_after_s=0.03,
    )
    await poller.start()
    try:
        # Immediately after start the cache is fresh
        assert poller.current() == 50_000.0
        await asyncio.sleep(0.1)
        # Now the cached value is older than stale_after_s AND every
        # subsequent fetch returns None -- current() must surface None
        # rather than silently serve a stale MTM.
        assert poller.current() is None
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_never_succeeded_returns_none() -> None:
    async def fetch() -> float | None:
        return None

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.02,
        stale_after_s=5.0,
    )
    await poller.start()
    try:
        assert poller.current() is None
        assert poller.fetch_ok == 0
        assert poller.fetch_none >= 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_integrates_with_reconciler() -> None:
    async def fetch() -> float | None:
        return 49_900.0  # $100 below our logical

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=5.0,
    )
    await poller.start()
    try:
        rec = BrokerEquityReconciler(
            broker_equity_source=poller.current,
            tolerance_usd=50.0,
            tolerance_pct=0.005,
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.broker_equity_usd == 49_900.0
        assert result.drift_usd == 100.0
        assert not result.in_tolerance
        assert result.reason == "broker_below_logical"
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    async def fetch() -> float | None:
        return 50_000.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=5.0,
    )
    await poller.start()
    try:
        task1 = poller._task
        await poller.start()  # second start is a no-op
        assert poller._task is task1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_stop_without_start_is_safe() -> None:
    async def fetch() -> float | None:
        return 50_000.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=5.0,
    )
    # No start; stop must not raise
    await poller.stop()
    assert not poller.is_running()


def test_invalid_refresh_s_rejected() -> None:
    async def fetch() -> float | None:
        return 50_000.0

    with pytest.raises(ValueError, match="refresh_s"):
        BrokerEquityPoller(
            name="stub",
            fetch_fn=fetch,
            refresh_s=0.0,
            stale_after_s=1.0,
        )


def test_invalid_stale_after_s_rejected() -> None:
    async def fetch() -> float | None:
        return 50_000.0

    with pytest.raises(ValueError, match="stale_after_s"):
        BrokerEquityPoller(
            name="stub",
            fetch_fn=fetch,
            refresh_s=1.0,
            stale_after_s=0.0,
        )


# ---------------------------------------------------------------------------
# H4 partial closure (v0.1.69) -- byte-identical-poll detection
# ---------------------------------------------------------------------------


def test_consecutive_identical_starts_at_zero() -> None:
    async def fetch() -> float | None:
        return 50_000.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=1.0,
        stale_after_s=2.0,
    )
    assert poller.consecutive_identical == 0


def test_invalid_identical_warn_after_rejected() -> None:
    async def fetch() -> float | None:
        return 50_000.0

    with pytest.raises(ValueError, match="identical_warn_after"):
        BrokerEquityPoller(
            name="stub",
            fetch_fn=fetch,
            refresh_s=1.0,
            stale_after_s=2.0,
            identical_warn_after=-1,
        )


@pytest.mark.asyncio
async def test_identical_polls_increment_counter() -> None:
    """Successive polls returning the same value bump consecutive_identical."""

    async def fetch() -> float | None:
        return 50_000.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=10.0,
    )
    # Drive _poll_once directly (bypasses the asyncio loop)
    await poller._poll_once()  # noqa: SLF001
    # First poll has no prior value; counter stays at 0.
    assert poller.consecutive_identical == 0
    await poller._poll_once()  # noqa: SLF001
    assert poller.consecutive_identical == 1
    await poller._poll_once()  # noqa: SLF001
    assert poller.consecutive_identical == 2


@pytest.mark.asyncio
async def test_changed_value_resets_counter() -> None:
    """A value change resets consecutive_identical to 0."""
    values = iter([50_000.0, 50_000.0, 50_000.0, 50_100.0, 50_100.0])

    async def fetch() -> float | None:
        return next(values)

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=10.0,
    )
    await poller._poll_once()  # noqa: SLF001 -- first
    await poller._poll_once()  # noqa: SLF001 -- identical (count=1)
    await poller._poll_once()  # noqa: SLF001 -- identical (count=2)
    assert poller.consecutive_identical == 2
    await poller._poll_once()  # noqa: SLF001 -- value changes (count=0)
    assert poller.consecutive_identical == 0
    await poller._poll_once()  # noqa: SLF001 -- identical to prev (count=1)
    assert poller.consecutive_identical == 1


@pytest.mark.asyncio
async def test_warn_fires_once_at_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warn log fires exactly at the threshold, not before, not after."""
    import logging

    async def fetch() -> float | None:
        return 50_000.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=10.0,
        identical_warn_after=3,
    )
    target_logger = "eta_engine.core.broker_equity_poller"
    with caplog.at_level(logging.WARNING, logger=target_logger):
        # First 3 polls: counter goes 0, 1, 2. No warn yet.
        for _ in range(3):
            await poller._poll_once()  # noqa: SLF001
        warn_records_before = [r for r in caplog.records if "consecutive identical" in r.getMessage()]
        assert len(warn_records_before) == 0
        # Fourth poll: counter goes to 3 == threshold -> warn fires.
        await poller._poll_once()  # noqa: SLF001
        warn_records_after = [r for r in caplog.records if "consecutive identical" in r.getMessage()]
        assert len(warn_records_after) == 1
        # Fifth poll: counter goes to 4. No additional warn (single-fire).
        await poller._poll_once()  # noqa: SLF001
        warn_records_no_spam = [r for r in caplog.records if "consecutive identical" in r.getMessage()]
        assert len(warn_records_no_spam) == 1


@pytest.mark.asyncio
async def test_warn_disabled_when_identical_warn_after_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default ``identical_warn_after=0`` disables the warn entirely."""
    import logging

    async def fetch() -> float | None:
        return 50_000.0

    poller = BrokerEquityPoller(
        name="stub",
        fetch_fn=fetch,
        refresh_s=0.05,
        stale_after_s=10.0,
        # Default identical_warn_after=0
    )
    with caplog.at_level(logging.WARNING, logger="eta_engine.core.broker_equity_poller"):
        for _ in range(20):
            await poller._poll_once()  # noqa: SLF001
        warn_records = [r for r in caplog.records if "consecutive identical" in r.getMessage()]
        assert len(warn_records) == 0
        # But the counter still tracks for observability
        assert poller.consecutive_identical >= 19
