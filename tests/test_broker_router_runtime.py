from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from eta_engine.scripts.broker_router_runtime import BrokerRouterRuntimeControl


class _Loop:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    def add_signal_handler(self, sig: object, callback) -> None:
        self.calls.append((sig, callback))


def _helper(
    tmp_path: Path,
    *,
    stopped: dict[str, bool] | None = None,
    tick=None,
    get_running_loop=None,
    sleep=None,
    signals=(object(), object()),
) -> BrokerRouterRuntimeControl:
    state = stopped if stopped is not None else {"value": False}

    async def _tick() -> None:
        return None

    async def _sleep(_seconds: float) -> None:
        return None

    return BrokerRouterRuntimeControl(
        pending_dir=tmp_path / "pending",
        state_root=tmp_path / "state",
        dry_run=False,
        interval_s=5.0,
        is_stopped=lambda: state["value"],
        set_stopped=lambda value: state.__setitem__("value", bool(value)),
        tick=tick or _tick,
        logger=logging.getLogger("test_broker_router_runtime"),
        get_running_loop=get_running_loop or (lambda: _Loop()),
        sleep=sleep or _sleep,
        signals=signals,
    )


def test_request_stop_sets_state(tmp_path: Path) -> None:
    stopped = {"value": False}
    helper = _helper(tmp_path, stopped=stopped)

    helper.request_stop()

    assert stopped["value"] is True


def test_run_once_invokes_tick_once(tmp_path: Path) -> None:
    calls: list[str] = []

    async def _tick() -> None:
        calls.append("tick")

    helper = _helper(tmp_path, tick=_tick)
    asyncio.run(helper.run_once())

    assert calls == ["tick"]


def test_run_registers_signals_and_stops_after_tick_requests_stop(tmp_path: Path) -> None:
    stopped = {"value": False}
    calls: list[str] = []
    loop = _Loop()
    sleep_calls: list[float] = []

    async def _tick() -> None:
        calls.append("tick")
        stopped["value"] = True

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    helper = _helper(
        tmp_path,
        stopped=stopped,
        tick=_tick,
        get_running_loop=lambda: loop,
        sleep=_sleep,
    )
    asyncio.run(helper.run())

    assert calls == ["tick"]
    assert sleep_calls == []
    assert len(loop.calls) == 2


def test_run_breaks_cleanly_when_sleep_is_cancelled(tmp_path: Path) -> None:
    loop = _Loop()
    calls: list[str] = []

    async def _tick() -> None:
        calls.append("tick")

    async def _sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    helper = _helper(
        tmp_path,
        tick=_tick,
        get_running_loop=lambda: loop,
        sleep=_sleep,
    )
    asyncio.run(helper.run())

    assert calls == ["tick"]
