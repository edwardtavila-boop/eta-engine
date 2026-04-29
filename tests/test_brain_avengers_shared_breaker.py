from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.brain.avengers.circuit_breaker import BreakerState, BreakerTripped
from eta_engine.brain.avengers.shared_breaker import (
    SCHEMA_VERSION,
    SharedCircuitBreaker,
    read_shared_status,
    reset_shared,
)


def test_reset_shared_writes_closed_schema(tmp_path) -> None:
    path = tmp_path / "breaker.json"

    assert reset_shared(path) is True
    status = read_shared_status(path)

    assert status is not None
    assert status["version"] == SCHEMA_VERSION
    assert status["state"] == BreakerState.CLOSED.value
    assert status["last_reason"] == "operator_reset"


def test_read_shared_status_returns_none_for_missing_or_bad_json(tmp_path) -> None:
    assert read_shared_status(tmp_path / "missing.json") is None

    bad_path = tmp_path / "breaker.json"
    bad_path.write_text("{not json", encoding="utf-8")

    assert read_shared_status(bad_path) is None


def test_shared_breaker_rehydrates_open_state_and_blocks_dispatch(tmp_path) -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    path = tmp_path / "breaker.json"
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "state": BreakerState.OPEN.value,
                "tripped_at": now.isoformat(),
                "reopen_at": (now + timedelta(minutes=5)).isoformat(),
                "last_reason": "unit trip",
                "written_at": now.isoformat(),
                "writer_pid": 123,
            }
        ),
        encoding="utf-8",
    )

    breaker = SharedCircuitBreaker(path=path, clock=lambda: now)

    with pytest.raises(BreakerTripped, match="unit trip"):
        breaker.pre_dispatch()
    assert breaker.status().state is BreakerState.OPEN


def test_shared_breaker_moves_expired_open_state_to_half_open(tmp_path) -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    path = tmp_path / "breaker.json"
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "state": BreakerState.OPEN.value,
                "tripped_at": (now - timedelta(minutes=10)).isoformat(),
                "reopen_at": (now - timedelta(minutes=1)).isoformat(),
                "last_reason": "cooldown elapsed",
                "written_at": now.isoformat(),
                "writer_pid": 123,
            }
        ),
        encoding="utf-8",
    )

    breaker = SharedCircuitBreaker(path=path, clock=lambda: now)
    breaker.pre_dispatch()

    assert breaker.status().state is BreakerState.HALF_OPEN
