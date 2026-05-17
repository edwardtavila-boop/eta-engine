"""Compatibility shim for ``eta_engine.scripts.schedule_weekly_review``."""

from __future__ import annotations

from eta_engine.feeds._script_shim import build_script_shim

_script_module, __all__, __getattr__, __dir__ = build_script_shim(
    "eta_engine.feeds.schedule_weekly_review",
    "eta_engine.scripts.schedule_weekly_review",
)


def main() -> int:
    return _script_module.main()


if __name__ == "__main__":
    raise SystemExit(main())
