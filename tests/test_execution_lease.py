from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.safety.execution_lease import (
    ExecutionLeaseHeld,
    acquire_execution_lease,
    read_execution_lease,
    refresh_execution_lease,
    release_execution_lease,
)


def test_execution_lease_blocks_competing_owner(tmp_path: Path) -> None:
    lease = acquire_execution_lease(
        venue="ibkr",
        account="DUQ319869",
        owner="jarvis_strategy_supervisor",
        client_id=187,
        ttl_s=60,
        root=tmp_path,
        now=100.0,
    )

    with pytest.raises(ExecutionLeaseHeld) as held:
        acquire_execution_lease(
            venue="ibkr",
            account="DUQ319869",
            owner="broker_router",
            client_id=188,
            ttl_s=60,
            root=tmp_path,
            now=110.0,
        )

    holder = held.value.holder
    assert holder["owner"] == "jarvis_strategy_supervisor"
    assert holder["client_id"] == 187
    assert holder["expires_at"] == 160.0
    assert lease.path.name == "ibkr_DUQ319869.json"


def test_execution_lease_allows_same_owner_refresh(tmp_path: Path) -> None:
    lease = acquire_execution_lease(
        venue="ibkr",
        account="DUQ319869",
        owner="jarvis_strategy_supervisor",
        client_id=187,
        ttl_s=60,
        root=tmp_path,
        now=100.0,
    )

    refreshed = acquire_execution_lease(
        venue="ibkr",
        account="DUQ319869",
        owner="jarvis_strategy_supervisor",
        client_id=187,
        ttl_s=60,
        root=tmp_path,
        now=120.0,
    )

    assert refreshed.path == lease.path
    assert refreshed.updated_at == 120.0
    assert refreshed.expires_at == 180.0
    assert read_execution_lease("ibkr", "DUQ319869", root=tmp_path)["updated_at"] == 120.0


def test_execution_lease_can_be_stolen_after_expiry(tmp_path: Path) -> None:
    acquire_execution_lease(
        venue="ibkr",
        account="DUQ319869",
        owner="jarvis_strategy_supervisor",
        client_id=187,
        ttl_s=60,
        root=tmp_path,
        now=100.0,
    )

    stolen = acquire_execution_lease(
        venue="ibkr",
        account="DUQ319869",
        owner="broker_router",
        client_id=188,
        ttl_s=60,
        root=tmp_path,
        now=161.0,
    )

    assert stolen.owner == "broker_router"
    assert stolen.client_id == 188
    assert stolen.expires_at == 221.0


def test_execution_lease_refresh_and_release_are_owner_scoped(tmp_path: Path) -> None:
    lease = acquire_execution_lease(
        venue="ibkr",
        account="DUQ319869",
        owner="jarvis_strategy_supervisor",
        client_id=187,
        ttl_s=60,
        root=tmp_path,
        now=100.0,
    )

    refreshed = refresh_execution_lease(lease, now=130.0)

    assert refreshed.updated_at == 130.0
    assert refreshed.expires_at == 190.0

    competing = acquire_execution_lease(
        venue="ibkr",
        account="DUQ319869",
        owner="broker_router",
        client_id=188,
        ttl_s=60,
        root=tmp_path,
        now=191.0,
    )
    release_execution_lease(refreshed)

    assert read_execution_lease("ibkr", "DUQ319869", root=tmp_path)["owner"] == "broker_router"

    release_execution_lease(competing)

    assert read_execution_lease("ibkr", "DUQ319869", root=tmp_path) is None
