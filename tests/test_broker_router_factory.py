from __future__ import annotations

from pathlib import Path

from eta_engine.scripts.broker_router_factory import build_router


def test_build_router_constructs_dependencies_and_threads_kwargs(tmp_path: Path) -> None:
    actions: list[str] = []
    captured: dict[str, object] = {}

    class _SmartRouter:
        def __init__(self) -> None:
            actions.append("smart_router")

    class _Router:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    router = build_router(
        pending_dir=tmp_path / "pending",
        state_root=tmp_path / "state",
        interval_s=3.5,
        dry_run=True,
        max_retries=9,
        broker_router_cls=_Router,
        smart_router_cls=_SmartRouter,
        journal_factory=lambda: "journal",
    )

    assert isinstance(router, _Router)
    assert actions == ["smart_router"]
    assert captured["pending_dir"] == tmp_path / "pending"
    assert captured["state_root"] == tmp_path / "state"
    assert captured["interval_s"] == 3.5
    assert captured["dry_run"] is True
    assert captured["max_retries"] == 9
    assert captured["journal"] == "journal"
    assert isinstance(captured["smart_router"], _SmartRouter)
