"""Compatibility entrypoint for the canonical operator queue snapshot.

Historically the ``feeds`` package carried a copy of this command. Keep this
module as a thin shim so older schedulers/imports use the same freshness logic
as ``eta_engine.scripts.operator_queue_snapshot``.
"""

from __future__ import annotations

from eta_engine.scripts import operator_queue_snapshot as _canonical

build_snapshot = _canonical.build_snapshot
compare_snapshots = _canonical.compare_snapshots
default_previous_path_for = _canonical.default_previous_path_for
load_snapshot = _canonical.load_snapshot
main = _canonical.main
write_snapshot = _canonical.write_snapshot

__all__ = [
    "build_snapshot",
    "compare_snapshots",
    "default_previous_path_for",
    "load_snapshot",
    "main",
    "write_snapshot",
]


def __getattr__(name: str) -> object:
    return getattr(_canonical, name)


if __name__ == "__main__":
    raise SystemExit(main())
