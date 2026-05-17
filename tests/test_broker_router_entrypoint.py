from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

from eta_engine.scripts import broker_router_entrypoint


def test_load_build_default_chain_returns_injected_builder(tmp_path: Path, monkeypatch) -> None:
    marker = object()
    gate_chain = types.ModuleType("mnq.risk.gate_chain")
    gate_chain.build_default_chain = marker
    risk = types.ModuleType("mnq.risk")
    mnq = types.ModuleType("mnq")
    risk.gate_chain = gate_chain
    mnq.risk = risk

    monkeypatch.setitem(sys.modules, "mnq", mnq)
    monkeypatch.setitem(sys.modules, "mnq.risk", risk)
    monkeypatch.setitem(sys.modules, "mnq.risk.gate_chain", gate_chain)

    builder = broker_router_entrypoint.load_build_default_chain(root=tmp_path, sys_path=[])

    assert builder is marker


def test_resolve_helpers_honor_args_and_env(monkeypatch, tmp_path: Path) -> None:
    env = {
        "ETA_BROKER_ROUTER_PENDING_DIR": str(tmp_path / "env-pending"),
        "ETA_BROKER_ROUTER_STATE_ROOT": str(tmp_path / "env-state"),
        "ETA_BROKER_ROUTER_INTERVAL_S": "oops",
        "ETA_BROKER_ROUTER_DRY_RUN": "true",
        "ETA_BROKER_ROUTER_MAX_RETRIES": "7",
    }

    assert broker_router_entrypoint.resolve_pending_dir(
        None,
        default_pending_dir=tmp_path / "default-pending",
        env=env,
    ) == tmp_path / "env-pending"
    assert broker_router_entrypoint.resolve_state_root(
        None,
        default_state_root=tmp_path / "default-state",
        env=env,
    ) == tmp_path / "env-state"
    assert broker_router_entrypoint.resolve_interval(
        None,
        default_interval_s=5.0,
        logger=logging.getLogger("test_broker_router_entrypoint"),
        env=env,
    ) == 5.0
    assert broker_router_entrypoint.resolve_dry_run(False, env=env) is True
    assert broker_router_entrypoint.resolve_max_retries(
        None,
        default_max_retries=3,
        env=env,
    ) == 7


def test_main_once_constructs_router_and_runs_once(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    actions: list[str] = []

    class _SmartRouter:
        def __init__(self) -> None:
            actions.append("smart_router")

    class _Router:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run_once(self) -> None:
            actions.append("run_once")

        async def run(self) -> None:
            actions.append("run")

    rc = broker_router_entrypoint.main(
        [
            "--once",
            "--pending-dir",
            str(tmp_path / "pending"),
            "--state-root",
            str(tmp_path / "state"),
            "--interval",
            "3.5",
            "--dry-run",
            "--max-retries",
            "9",
        ],
        description="broker router",
        default_pending_dir=tmp_path / "default-pending",
        default_state_root=tmp_path / "default-state",
        default_interval_s=5.0,
        default_max_retries=3,
        broker_router_cls=_Router,
        smart_router_cls=_SmartRouter,
        default_journal_factory=lambda: "journal",
        logger=logging.getLogger("test_broker_router_entrypoint"),
        asyncio_run=lambda coro: asyncio.run(coro),
    )

    assert rc == 0
    assert actions == ["smart_router", "run_once"]
    assert captured["pending_dir"] == tmp_path / "pending"
    assert captured["state_root"] == tmp_path / "state"
    assert captured["interval_s"] == 3.5
    assert captured["dry_run"] is True
    assert captured["max_retries"] == 9
    assert captured["journal"] == "journal"


def test_main_run_path_uses_env_defaults(tmp_path: Path) -> None:
    actions: list[str] = []

    class _SmartRouter:
        def __init__(self) -> None:
            actions.append("smart_router")

    class _Router:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def run_once(self) -> None:
            actions.append("run_once")

        async def run(self) -> None:
            actions.append("run")

    rc = broker_router_entrypoint.main(
        [],
        description="broker router",
        default_pending_dir=tmp_path / "default-pending",
        default_state_root=tmp_path / "default-state",
        default_interval_s=5.0,
        default_max_retries=3,
        broker_router_cls=_Router,
        smart_router_cls=_SmartRouter,
        default_journal_factory=lambda: "journal",
        logger=logging.getLogger("test_broker_router_entrypoint"),
        asyncio_run=lambda coro: asyncio.run(coro),
        env={
            "ETA_BROKER_ROUTER_PENDING_DIR": str(tmp_path / "env-pending"),
            "ETA_BROKER_ROUTER_STATE_ROOT": str(tmp_path / "env-state"),
            "ETA_BROKER_ROUTER_INTERVAL_S": "4.0",
            "ETA_BROKER_ROUTER_MAX_RETRIES": "6",
        },
    )

    assert rc == 0
    assert actions == ["smart_router", "run"]
