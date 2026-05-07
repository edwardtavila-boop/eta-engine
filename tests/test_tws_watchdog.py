from __future__ import annotations

import json
from pathlib import Path


def test_unhealthy_watchdog_status_includes_latest_ibgateway_jvm_oom(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import tws_watchdog

    crash_dir = tmp_path / "ibgateway"
    crash_dir.mkdir()
    (crash_dir / "hs_err_pid16004.log").write_text(
        "\n".join(
            [
                "# There is insufficient memory for the Java Runtime Environment to continue.",
                "# Native memory allocation (malloc) failed to allocate 1065696 bytes.",
                "#  Out of Memory Error (arena.cpp:191), pid=16004, tid=7540",
                "# Command Line: -Xmx768m -XX:ParallelGCThreads=20 -XX:ConcGCThreads=5",
            ],
        ),
        encoding="utf-8",
    )
    status_path = tmp_path / "tws_watchdog.json"
    monkeypatch.setattr(tws_watchdog, "_STATUS_PATH", status_path)
    monkeypatch.setattr(tws_watchdog, "_check_socket", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        tws_watchdog,
        "_gateway_process_snapshot",
        lambda *_args, **_kwargs: {
            "running": True,
            "pid": 8072,
            "name": "ibgateway.exe",
            "working_set_mb": 149.3,
            "command_line": r"C:\Jts\ibgateway\1046\ibgateway.exe -login=apexpredatoribkr",
        },
    )

    rc = tws_watchdog.main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "4002",
            "--alert-after",
            "99",
            "--crash-log-dir",
            str(crash_dir),
        ],
    )

    assert rc == 1
    data = json.loads(status_path.read_text(encoding="utf-8"))
    crash = data["details"]["gateway_crash"]
    assert crash["reason_code"] == "jvm_native_memory_oom"
    assert crash["summary"] == "IB Gateway JVM native-memory OOM"
    assert "Native memory allocation" in crash["native_allocation"]
    assert crash["xmx"] == "768m"
    assert data["details"]["gateway_process"]["running"] is True
    assert data["details"]["gateway_process"]["pid"] == 8072


def test_gateway_process_snapshot_reports_not_running_when_tasklist_is_empty(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from eta_engine.scripts import tws_watchdog

    monkeypatch.setattr(tws_watchdog.os, "name", "nt")
    monkeypatch.setattr(
        tws_watchdog.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout='INFO: No tasks are running which match the specified criteria.\n',
        ),
    )

    snapshot = tws_watchdog._gateway_process_snapshot(Path(r"C:\Jts\ibgateway\1046"))

    assert snapshot == {
        "running": False,
        "gateway_dir": r"C:\Jts\ibgateway\1046",
        "name": "ibgateway.exe/ibgateway1.exe",
    }


def test_gateway_process_snapshot_detects_ibc_renamed_gateway_runtime(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from eta_engine.scripts import tws_watchdog

    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        image_name = args[2]
        if image_name.endswith("ibgateway.exe"):
            return SimpleNamespace(
                returncode=0,
                stdout='INFO: No tasks are running which match the specified criteria.\n',
            )
        return SimpleNamespace(
            returncode=0,
            stdout='"ibgateway1.exe","11796","Console","1","324,988 K"\n',
        )

    monkeypatch.setattr(tws_watchdog.os, "name", "nt")
    monkeypatch.setattr(tws_watchdog.subprocess, "run", fake_run)

    snapshot = tws_watchdog._gateway_process_snapshot(Path(r"C:\Jts\ibgateway\1046"))

    assert calls[0][2] == "IMAGENAME eq ibgateway.exe"
    assert calls[1][2] == "IMAGENAME eq ibgateway1.exe"
    assert snapshot == {
        "running": True,
        "pid": 11796,
        "name": "ibgateway1.exe",
        "working_set_mb": 317.4,
        "gateway_dir": r"C:\Jts\ibgateway\1046",
    }


def test_successful_handshake_does_not_open_raw_socket_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import tws_watchdog

    status_path = tmp_path / "tws_watchdog.json"
    monkeypatch.setattr(tws_watchdog, "_STATUS_PATH", status_path)
    monkeypatch.setattr(
        tws_watchdog,
        "_check_ib_handshake",
        lambda *_args, **_kwargs: (True, "serverVersion=176; clientId=55; attempt=1"),
    )

    def fail_raw_socket_probe(*_args, **_kwargs):
        raise AssertionError("raw TCP probe should not run after a successful IB handshake")

    monkeypatch.setattr(tws_watchdog, "_check_socket", fail_raw_socket_probe)

    rc = tws_watchdog.main(["--host", "127.0.0.1", "--port", "4002"])

    assert rc == 0
    data = json.loads(status_path.read_text(encoding="utf-8"))
    assert data["healthy"] is True
    assert data["details"]["socket_ok"] is True
    assert data["details"]["handshake_ok"] is True


def test_failed_handshake_uses_raw_socket_probe_for_classification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import tws_watchdog

    status_path = tmp_path / "tws_watchdog.json"
    monkeypatch.setattr(tws_watchdog, "_STATUS_PATH", status_path)
    monkeypatch.setattr(
        tws_watchdog,
        "_check_ib_handshake",
        lambda *_args, **_kwargs: (False, "TimeoutError()"),
    )
    monkeypatch.setattr(tws_watchdog, "_check_socket", lambda *_args, **_kwargs: True)

    rc = tws_watchdog.main(["--host", "127.0.0.1", "--port", "4002", "--alert-after", "99"])

    assert rc == 1
    data = json.loads(status_path.read_text(encoding="utf-8"))
    assert data["healthy"] is False
    assert data["details"]["socket_ok"] is True
    assert data["details"]["handshake_ok"] is False
    assert data["details"]["handshake_detail"] == "TimeoutError()"


def test_watchdog_client_ids_use_reserved_low_id_pool(monkeypatch) -> None:
    from eta_engine.scripts import tws_watchdog

    monkeypatch.delenv("ETA_TWS_WATCHDOG_CLIENT_IDS", raising=False)

    assert tws_watchdog._watchdog_client_ids() == (55, 99, 101, 102)


def test_watchdog_client_ids_can_be_overridden(monkeypatch) -> None:
    from eta_engine.scripts import tws_watchdog

    monkeypatch.setenv("ETA_TWS_WATCHDOG_CLIENT_IDS", "55, bad, 102")

    assert tws_watchdog._watchdog_client_ids() == (55, 102)


def test_default_handshake_timeout_uses_shared_ibkr_timeout(monkeypatch) -> None:
    from eta_engine.scripts import tws_watchdog

    monkeypatch.delenv("ETA_TWS_WATCHDOG_HANDSHAKE_TIMEOUT_S", raising=False)
    monkeypatch.setenv("ETA_IBKR_CONNECT_TIMEOUT_S", "20")

    assert tws_watchdog._default_handshake_timeout() == 20.0


def test_default_handshake_timeout_prefers_watchdog_override(monkeypatch) -> None:
    from eta_engine.scripts import tws_watchdog

    monkeypatch.setenv("ETA_IBKR_CONNECT_TIMEOUT_S", "20")
    monkeypatch.setenv("ETA_TWS_WATCHDOG_HANDSHAKE_TIMEOUT_S", "26")

    assert tws_watchdog._default_handshake_timeout() == 26.0


def test_default_handshake_timeout_defaults_to_45_seconds(monkeypatch) -> None:
    from eta_engine.scripts import tws_watchdog

    monkeypatch.delenv("ETA_IBKR_CONNECT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("ETA_TWS_WATCHDOG_HANDSHAKE_TIMEOUT_S", raising=False)

    assert tws_watchdog._default_handshake_timeout() == 45.0


def test_ensure_asyncio_event_loop_creates_loop_when_missing(monkeypatch) -> None:
    from eta_engine.scripts import tws_watchdog

    loop = object()
    captured = {}

    def missing_loop():
        raise RuntimeError("There is no current event loop in thread 'MainThread'.")

    monkeypatch.setattr(tws_watchdog.asyncio, "get_event_loop", missing_loop)
    monkeypatch.setattr(tws_watchdog.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(tws_watchdog.asyncio, "set_event_loop", lambda value: captured.setdefault("loop", value))

    tws_watchdog._ensure_asyncio_event_loop()

    assert captured["loop"] is loop


def test_account_snapshot_masks_account_and_captures_executions() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from eta_engine.scripts import tws_watchdog

    contract = SimpleNamespace(
        symbol="MNQ",
        secType="FUT",
        exchange="CME",
        currency="USD",
        localSymbol="MNQM6",
        conId=770561201,
    )
    execution = SimpleNamespace(
        acctNumber="DUQ319869",
        side="BOT",
        shares=1,
        price=104.32,
        time=datetime(2026, 5, 5, 17, 49, tzinfo=UTC),
        orderId=123,
        permId=456,
        execId="58268.1777959080.11",
        orderRef="mnq_futures_sage",
    )
    commission = SimpleNamespace(commission=2.5, currency="USD", realizedPNL=18.0)

    class FakeIB:
        def positions(self):
            return [
                SimpleNamespace(
                    account="DUQ319869",
                    contract=contract,
                    position=6,
                    avgCost=123.45,
                ),
            ]

        def portfolio(self):
            return [
                SimpleNamespace(
                    account="DUQ319869",
                    contract=contract,
                    position=6,
                    marketPrice=123.75,
                    marketValue=742.5,
                    averageCost=123.45,
                    unrealizedPNL=1.8,
                    realizedPNL=18.0,
                ),
            ]

        def reqExecutions(self):  # noqa: N802 - mirrors ib_insync API.
            return [
                SimpleNamespace(
                    contract=contract,
                    execution=execution,
                    commissionReport=commission,
                ),
            ]

    snapshot = tws_watchdog._snapshot_from_ib(FakeIB())

    assert snapshot["accounts"] == ["DUQ...9869"]
    assert snapshot["summary"]["open_positions_count"] == 1
    assert snapshot["summary"]["executions_count"] == 1
    assert snapshot["summary"]["last_execution_symbol"] == "MNQ"
    assert snapshot["summary"]["realized_pnl"] == 18.0
    assert snapshot["positions"][0]["account"] == "DUQ...9869"
    assert snapshot["executions"][0]["account"] == "DUQ...9869"
    assert snapshot["executions"][0]["bot"] == "mnq_futures_sage"


def test_handshake_uses_readonly_ib_connection(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from eta_engine.scripts import tws_watchdog

    captured: dict[str, object] = {}

    class FakeIB:
        def __init__(self):
            self.client = SimpleNamespace(serverVersion=lambda: 176)
            self._connected = False

        def connect(self, host, port, *, clientId, timeout, readonly=False):
            captured.update(
                {
                    "host": host,
                    "port": port,
                    "clientId": clientId,
                    "timeout": timeout,
                    "readonly": readonly,
                }
            )
            self._connected = True

        def isConnected(self):
            return self._connected

        def disconnect(self):
            self._connected = False

    monkeypatch.setattr(tws_watchdog, "_ensure_asyncio_event_loop", lambda: None)
    monkeypatch.setattr(tws_watchdog, "_watchdog_client_ids", lambda: (55,))
    monkeypatch.setattr(
        tws_watchdog,
        "_snapshot_from_ib",
        lambda _ib: {"summary": {"positions_count": 0, "executions_count": 0}},
    )
    monkeypatch.setitem(sys.modules, "ib_insync", SimpleNamespace(IB=FakeIB))

    ok, detail = tws_watchdog._check_ib_handshake(
        "127.0.0.1",
        4002,
        attempts=1,
        timeout=12.0,
    )

    assert ok is True
    assert "clientId=55" in detail
    assert captured == {
        "host": "127.0.0.1",
        "port": 4002,
        "clientId": 55,
        "timeout": 12.0,
        "readonly": True,
    }
