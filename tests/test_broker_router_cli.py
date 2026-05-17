from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import broker_router


def test_broker_router_resolve_wrappers_use_router_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ETA_BROKER_ROUTER_PENDING_DIR", str(tmp_path / "env-pending"))
    monkeypatch.setenv("ETA_BROKER_ROUTER_STATE_ROOT", str(tmp_path / "env-state"))
    monkeypatch.setenv("ETA_BROKER_ROUTER_INTERVAL_S", "oops")
    monkeypatch.setenv("ETA_BROKER_ROUTER_DRY_RUN", "true")
    monkeypatch.setenv("ETA_BROKER_ROUTER_MAX_RETRIES", "7")

    assert broker_router._resolve_pending_dir(None) == tmp_path / "env-pending"
    assert broker_router._resolve_state_root(None) == tmp_path / "env-state"
    assert broker_router._resolve_interval(None) == broker_router.DEFAULT_INTERVAL_S
    assert broker_router._resolve_dry_run(False) is True
    assert broker_router._resolve_max_retries(None) == 7


def test_broker_router_main_delegates_to_bound_entrypoint(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = argv
        return 17

    monkeypatch.setattr(broker_router, "_broker_router_main", _fake_main)

    assert broker_router.main(["--once"]) == 17
    assert seen["argv"] == ["--once"]
