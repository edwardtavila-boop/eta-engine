"""
EVOLUTIONARY TRADING ALGO  //  tests.test_data_bybit_ws
===========================================
Exercise the BybitWSCapture real-path code with an injected fake websocket.
No network hit.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path  # noqa: TC003 - used as runtime annotation on pytest tmp_path fixtures
from typing import Any

import pytest

from eta_engine.data import bybit_ws as mod


class _FakeWS:
    """Minimal async-iterable websocket stand-in."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []
        self.closed: bool = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._frames.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_capture_stub_mode_is_no_op(tmp_path: Path) -> None:
    cap = mod.BybitWSCapture(
        symbols=["ETHUSDT"],
        data_root=tmp_path,
        stub=True,
        max_retries=1,
    )
    await cap.start(stop_on_clean_close=True)
    # Stub mode connects, subscribes (no-op), recv loop returns immediately.
    assert cap.retry_count == 0
    assert cap.alert_fired is False


@pytest.mark.asyncio
async def test_capture_writes_gzipped_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent_frames = [
        json.dumps({"topic": "kline.1.ETHUSDT", "data": [{"close": "2500"}]}),
        json.dumps({"topic": "publicTrade.ETHUSDT", "data": [{"p": "2500.1"}]}),
        json.dumps({"op": "subscribe", "success": True}),  # ack - should be skipped
        json.dumps({"topic": "orderbook.50.BTCUSDT", "data": {"b": [], "a": []}}),
    ]
    fake_ws = _FakeWS(sent_frames)

    async def fake_connect(*args: Any, **kwargs: Any) -> _FakeWS:  # noqa: ANN401 - websockets.connect signature
        return fake_ws

    # Patch the lazy-imported websockets.connect by stubbing the module
    import types

    fake_mod = types.SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_mod)

    cap = mod.BybitWSCapture(
        symbols=["ETHUSDT", "BTCUSDT"],
        data_root=tmp_path,
        max_retries=1,
    )
    await cap.start(stop_on_clean_close=True)

    assert fake_ws.sent, "expected a subscribe frame to be sent"
    sub = json.loads(fake_ws.sent[0])
    assert sub["op"] == "subscribe"
    assert any("ETHUSDT" in a for a in sub["args"])
    assert any("BTCUSDT" in a for a in sub["args"])

    # Captured files should exist and contain the 3 routed frames
    eth_files = list((tmp_path / "ETHUSDT").glob("*.jsonl.gz"))
    btc_files = list((tmp_path / "BTCUSDT").glob("*.jsonl.gz"))
    assert len(eth_files) == 1
    assert len(btc_files) == 1

    with gzip.open(eth_files[0], "rb") as fh:
        eth_lines = fh.read().decode("utf-8").strip().splitlines()
    with gzip.open(btc_files[0], "rb") as fh:
        btc_lines = fh.read().decode("utf-8").strip().splitlines()
    assert len(eth_lines) == 2  # 2 ETH frames
    assert len(btc_lines) == 1  # 1 BTC frame
    # Sanity: _recv_ts added
    assert "_recv_ts" in json.loads(eth_lines[0])
    # Fake ws got closed on teardown
    assert fake_ws.closed is True


@pytest.mark.asyncio
async def test_capture_reconnects_on_transport_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    async def fake_connect(*args: Any, **kwargs: Any) -> _FakeWS:  # noqa: ANN401 - websockets.connect signature
        attempts.append(1)
        if len(attempts) < 2:
            raise OSError("transient")
        return _FakeWS([])  # empty frames -> clean exit

    import types

    fake_mod = types.SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_mod)

    # Patch sleep to avoid 1s+ real delay
    async def _sleep_zero(_d: float) -> None:
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _sleep_zero)

    cap = mod.BybitWSCapture(
        symbols=["ETHUSDT"],
        data_root=tmp_path,
        max_retries=3,
    )
    await cap.start(stop_on_clean_close=True)
    assert len(attempts) >= 2
    # After success the retry_count resets to 0 on clean exit
    assert cap.retry_count == 0


@pytest.mark.asyncio
async def test_capture_alert_fires_after_max_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def always_fail(*args: Any, **kwargs: Any) -> _FakeWS:  # noqa: ANN401 - websockets.connect signature
        raise OSError("permanent")

    import types

    fake_mod = types.SimpleNamespace(connect=always_fail)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_mod)

    async def _sleep_zero(_d: float) -> None:
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _sleep_zero)

    cap = mod.BybitWSCapture(
        symbols=["ETHUSDT"],
        data_root=tmp_path,
        max_retries=2,
    )
    await cap.start()
    assert cap.retry_count == 2
    assert cap.alert_fired is True


@pytest.mark.asyncio
async def test_symbol_from_topic_edge_cases() -> None:
    assert mod.BybitWSCapture._symbol_from_topic("kline.1.ETHUSDT") == "ETHUSDT"
    assert mod.BybitWSCapture._symbol_from_topic("publicTrade.BTCUSDT") == "BTCUSDT"
    assert mod.BybitWSCapture._symbol_from_topic("") is None or mod.BybitWSCapture._symbol_from_topic("") == ""


@pytest.mark.asyncio
async def test_stop_flushes_files(tmp_path: Path) -> None:
    cap = mod.BybitWSCapture(
        symbols=["ETHUSDT"],
        data_root=tmp_path,
        stub=True,
    )
    await cap._write_line("ETHUSDT", {"topic": "kline.1.ETHUSDT", "k": 1})
    assert cap._file_handles
    await cap.stop()
    assert not cap._file_handles
