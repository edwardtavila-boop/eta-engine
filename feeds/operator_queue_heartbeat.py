"""Compatibility entrypoint for the canonical operator queue heartbeat."""

from __future__ import annotations

from eta_engine.scripts import operator_queue_heartbeat as _canonical

build_heartbeat = _canonical.build_heartbeat
build_snapshot_with_drift = _canonical.build_snapshot_with_drift
main = _canonical.main
render_text = _canonical.render_text

__all__ = [
    "build_heartbeat",
    "build_snapshot_with_drift",
    "main",
    "render_text",
]


def __getattr__(name: str) -> object:
    return getattr(_canonical, name)


if __name__ == "__main__":
    raise SystemExit(main())
