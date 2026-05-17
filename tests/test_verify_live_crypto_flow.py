from __future__ import annotations

import asyncio
from types import SimpleNamespace

from eta_engine.scripts import verify_live_crypto_flow as mod
from eta_engine.venues.base import ConnectionStatus, VenueConnectionReport


def test_format_order_row_prefers_broker_fill_ts() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "broker_fill_ts": "2026-05-16T14:40:00+00:00",
            "filled_at": "2026-05-16T14:40:08+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-1",
        }
    )

    assert "ts=2026-05-16T14:40:00+00:00" in row
    assert "filled_avg_price=65000.0" in row


def test_format_order_row_falls_back_to_execution_time() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "execution_time": "2026-05-16T14:40:04+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-2",
        }
    )

    assert "ts=2026-05-16T14:40:04+00:00" in row


def test_format_order_row_falls_back_to_executed_at() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "executed_at": "2026-05-16T14:40:06+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-3",
        }
    )

    assert "ts=2026-05-16T14:40:06+00:00" in row


def test_format_order_row_falls_back_to_last_fill_time() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "lastFillTime": "2026-05-16T14:40:07+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-4",
        }
    )

    assert "ts=2026-05-16T14:40:07+00:00" in row


def test_format_order_row_falls_back_to_timestamp() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "timestamp": "2026-05-16T14:40:08+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-5",
        }
    )

    assert "ts=2026-05-16T14:40:08+00:00" in row


def test_format_order_row_falls_back_to_ts() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "ts": "2026-05-16T14:40:09+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-6",
        }
    )

    assert "ts=2026-05-16T14:40:09+00:00" in row


def test_format_order_row_falls_back_to_time() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "time": "2026-05-16T14:40:10+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-7",
        }
    )

    assert "ts=2026-05-16T14:40:10+00:00" in row


def test_format_order_row_falls_back_to_updated_at() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "updated_at": "2026-05-16T14:40:11+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-8",
        }
    )

    assert "ts=2026-05-16T14:40:11+00:00" in row


def test_format_order_row_falls_back_to_hyphenated_filled_at() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "filled-at": "2026-05-16T14:40:12+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-9",
        }
    )

    assert "ts=2026-05-16T14:40:12+00:00" in row


def test_format_order_row_falls_back_to_hyphenated_execution_time() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "execution-time": "2026-05-16T14:40:12.500000+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-9b",
        }
    )

    assert "ts=2026-05-16T14:40:12.500000+00:00" in row


def test_format_order_row_falls_back_to_hyphenated_executed_at() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "executed-at": "2026-05-16T14:40:12.750000+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-9c",
        }
    )

    assert "ts=2026-05-16T14:40:12.750000+00:00" in row


def test_format_order_row_falls_back_to_hyphenated_updated_at() -> None:
    row = mod._format_order_row(
        {
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.1",
            "status": "filled",
            "filled_avg_price": "65000.0",
            "updated-at": "2026-05-16T14:40:13+00:00",
            "submitted_at": "2026-05-16T14:39:50+00:00",
            "id": "ord-10",
        }
    )

    assert "ts=2026-05-16T14:40:13+00:00" in row


def test_watch_uses_broker_fill_ts_for_first_fill_summary(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-1",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "broker_fill_ts": "2026-05-16T14:40:00+00:00",
                "filled_at": "2026-05-16T14:40:08+00:00",
                "submitted_at": "2026-05-16T14:39:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:40:00+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:40:00+00:00" in transcript


def test_watch_uses_execution_time_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-2",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "execution_time": "2026-05-16T14:41:04+00:00",
                "submitted_at": "2026-05-16T14:40:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:41:04+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:41:04+00:00" in transcript


def test_watch_uses_executed_at_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-3",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "executed_at": "2026-05-16T14:42:06+00:00",
                "submitted_at": "2026-05-16T14:41:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:42:06+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:42:06+00:00" in transcript


def test_watch_uses_last_fill_time_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-4",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "lastFillTime": "2026-05-16T14:43:07+00:00",
                "submitted_at": "2026-05-16T14:42:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:43:07+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:43:07+00:00" in transcript


def test_watch_uses_timestamp_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-5",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "timestamp": "2026-05-16T14:44:08+00:00",
                "submitted_at": "2026-05-16T14:43:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:44:08+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:44:08+00:00" in transcript


def test_watch_uses_ts_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-6",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "ts": "2026-05-16T14:45:09+00:00",
                "submitted_at": "2026-05-16T14:44:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:45:09+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:45:09+00:00" in transcript


def test_watch_uses_time_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-7",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "time": "2026-05-16T14:46:10+00:00",
                "submitted_at": "2026-05-16T14:45:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:46:10+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:46:10+00:00" in transcript


def test_watch_uses_updated_at_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-8",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "updated_at": "2026-05-16T14:47:11+00:00",
                "submitted_at": "2026-05-16T14:46:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:47:11+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:47:11+00:00" in transcript


def test_watch_uses_hyphenated_filled_at_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-9",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "filled-at": "2026-05-16T14:48:12+00:00",
                "submitted_at": "2026-05-16T14:47:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:48:12+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:48:12+00:00" in transcript


def test_watch_uses_hyphenated_execution_time_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-9b",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "execution-time": "2026-05-16T14:48:12.500000+00:00",
                "submitted_at": "2026-05-16T14:47:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:48:12.500000+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:48:12.500000+00:00" in transcript


def test_watch_uses_hyphenated_executed_at_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-9c",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "executed-at": "2026-05-16T14:48:12.750000+00:00",
                "submitted_at": "2026-05-16T14:47:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:48:12.750000+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:48:12.750000+00:00" in transcript


def test_watch_uses_hyphenated_updated_at_when_fill_fields_missing(monkeypatch) -> None:
    printed: list[str] = []
    call_count = {"n": 0}
    monotonic_values = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(
        mod.AlpacaConfig,
        "from_env",
        classmethod(
            lambda cls: mod.AlpacaConfig(  # noqa: ARG005
                base_url="https://paper-api.alpaca.markets",
                api_key_id="PK1",
                api_secret_key="SK1",
            )
        ),
    )

    async def _fake_connect(self) -> VenueConnectionReport:  # noqa: ANN001
        return VenueConnectionReport(
            venue="alpaca",
            status=ConnectionStatus.READY,
            creds_present=True,
            details={"endpoint": "https://paper-api.alpaca.markets", "probe": "ok"},
            error="",
        )

    async def _fake_list_recent_orders(venue, *, limit=mod.DEFAULT_ORDER_LIMIT):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        return [
            {
                "id": "ord-10",
                "symbol": "BTC/USD",
                "side": "buy",
                "qty": "0.1",
                "status": "filled",
                "filled_avg_price": "65000.0",
                "updated-at": "2026-05-16T14:49:13+00:00",
                "submitted_at": "2026-05-16T14:48:50+00:00",
            }
        ]

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.AlpacaVenue, "connect", _fake_connect)
    monkeypatch.setattr(mod, "_list_recent_orders", _fake_list_recent_orders)
    monkeypatch.setattr(mod, "asyncio", SimpleNamespace(sleep=_fake_sleep))
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)))

    exit_code = asyncio.run(mod.watch(watch_seconds=1, poll_interval_s=0.0, require_paper=False))

    transcript = "\n".join(printed)
    assert exit_code == 0
    assert "ts=2026-05-16T14:49:13+00:00" in transcript
    assert "first_crypto_fill_ts=2026-05-16T14:49:13+00:00" in transcript
