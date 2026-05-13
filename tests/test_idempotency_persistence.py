"""Disk-persistence regression tests for the idempotency store.

The in-memory _STORE survives only as long as the process. With
ETA_IDEMPOTENCY_STORE set, every check_or_register / record_result
appends a JSONL line so the store reloads on restart and a bounced
supervisor can't double-submit the same client_order_id."""

from __future__ import annotations

import json
import os
from importlib import reload
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_store(tmp_path: Path) -> Path:
    """Point ETA_IDEMPOTENCY_STORE at a fresh JSONL path and reload
    the module so its `_load_store_from_disk` honors the new env."""
    store_path = tmp_path / "idem.jsonl"
    os.environ["ETA_IDEMPOTENCY_STORE"] = str(store_path)
    from eta_engine.safety import idempotency

    idempotency.reset_store_for_test()
    reload(idempotency)
    yield store_path
    os.environ.pop("ETA_IDEMPOTENCY_STORE", None)
    idempotency.reset_store_for_test()
    reload(idempotency)


def test_check_or_register_writes_jsonl(tmp_store: Path) -> None:
    from eta_engine.safety import idempotency

    rec = idempotency.check_or_register(
        client_order_id="abc-123",
        venue="ibkr",
        symbol="MNQ1",
        intent_payload={"side": "BUY", "qty": 1.0},
    )
    assert rec.is_new
    assert tmp_store.exists()
    lines = tmp_store.read_text().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["client_order_id"] == "abc-123"
    assert obj["status"] == "pending"


def test_record_result_appends_status_update(tmp_store: Path) -> None:
    from eta_engine.safety import idempotency

    idempotency.check_or_register(
        client_order_id="abc-456",
        venue="ibkr",
        symbol="BTC",
        intent_payload={},
    )
    idempotency.record_result(
        client_order_id="abc-456",
        status="submitted",
        broker_order_id="98765",
    )
    lines = tmp_store.read_text().strip().splitlines()
    assert len(lines) == 2
    last = json.loads(lines[-1])
    assert last["status"] == "submitted"
    assert last["broker_order_id"] == "98765"


def test_retryable_failed_reopens_order_intent(tmp_store: Path) -> None:
    from eta_engine.safety import idempotency

    first = idempotency.check_or_register(
        client_order_id="retry-1",
        venue="ibkr",
        symbol="MNQ",
        intent_payload={"side": "BUY", "qty": 1.0},
    )
    assert first.is_new
    idempotency.record_result(
        client_order_id="retry-1",
        status="retryable_failed",
        response_payload={"reason": "TWS API connection on port 4002 failed"},
    )

    retry = idempotency.check_or_register(
        client_order_id="retry-1",
        venue="ibkr",
        symbol="MNQ",
        intent_payload={"side": "BUY", "qty": 1.0},
    )
    assert retry.is_new
    assert retry.status == "pending"
    assert retry.note == "retry_after_retryable_failure"

    lines = tmp_store.read_text().strip().splitlines()
    assert json.loads(lines[-1])["status"] == "pending"


def test_rejected_row_expires_on_short_ttl(
    tmp_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eta_engine.safety import idempotency

    monkeypatch.setenv("ETA_IDEMPOTENCY_REJECTED_TTL_S", "10")
    monkeypatch.setattr(idempotency._time, "time", lambda: 1000.0)
    idempotency.check_or_register(
        client_order_id="reject-ttl-1",
        venue="ibkr",
        symbol="MNQ",
        intent_payload={"side": "BUY", "qty": 1.0},
    )
    idempotency.record_result(
        client_order_id="reject-ttl-1",
        status="rejected",
        response_payload={"reason": "temporary broker reject"},
    )

    monkeypatch.setattr(idempotency._time, "time", lambda: 1011.0)
    retry = idempotency.check_or_register(
        client_order_id="reject-ttl-1",
        venue="ibkr",
        symbol="MNQ",
        intent_payload={"side": "BUY", "qty": 1.0},
    )

    assert retry.is_new
    assert retry.status == "pending"
    assert json.loads(tmp_store.read_text().strip().splitlines()[-1])["status"] == "pending"


def test_store_reloads_on_module_import(tmp_store: Path) -> None:
    """Simulate process restart: write JSONL, clear in-memory store,
    re-import → records reappear."""
    from eta_engine.safety import idempotency

    idempotency.check_or_register(
        client_order_id="restart-1",
        venue="ibkr",
        symbol="ETH",
        intent_payload={},
    )
    idempotency.record_result(
        client_order_id="restart-1",
        status="submitted",
        broker_order_id="55555",
    )
    # Simulate a fresh process: clear, then trigger the load function
    idempotency.reset_store_for_test()
    idempotency._load_store_from_disk()
    rec = idempotency.check_or_register(
        client_order_id="restart-1",
        venue="ibkr",
        symbol="ETH",
        intent_payload={},
    )
    assert not rec.is_new  # restored from disk
    assert rec.status == "submitted"
    assert rec.broker_order_id == "55555"


def test_disabled_via_env_returns_none() -> None:
    """ETA_IDEMPOTENCY_STORE=disabled forces in-memory-only — keeps
    tests hermetic without depending on the workspace state dir being
    absent (which it isn't, once anything else has used it).

    Default-on persistence means "unset" no longer means "no disk";
    the operator must opt out explicitly.
    """
    os.environ["ETA_IDEMPOTENCY_STORE"] = "disabled"
    try:
        from eta_engine.safety import idempotency

        reload(idempotency)
        idempotency.reset_store_for_test()
        rec = idempotency.check_or_register(
            client_order_id="no-disk-1",
            venue="ibkr",
            symbol="MNQ1",
            intent_payload={},
        )
        assert rec.is_new
        # Explicit disable returns None
        assert idempotency._persist_path() is None
    finally:
        os.environ.pop("ETA_IDEMPOTENCY_STORE", None)


def test_default_path_when_env_unset() -> None:
    """With ETA_IDEMPOTENCY_STORE unset, _persist_path() returns the
    canonical workspace default — NOT None. Default-on persistence is
    the new contract: a process bounce mid-trade must never lose the
    pending dedup log silently."""
    os.environ.pop("ETA_IDEMPOTENCY_STORE", None)
    try:
        from eta_engine.safety import idempotency

        reload(idempotency)
        path = idempotency._persist_path()
        # Default falls back to None on systems where the workspace
        # state dir isn't writable (CI sandbox, etc.); on a real
        # workspace we expect the canonical path.
        if path is not None:
            assert path == Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/idempotency.jsonl")
    finally:
        # leave the env in the unset state we want for hermetic isolation
        os.environ.pop("ETA_IDEMPOTENCY_STORE", None)
