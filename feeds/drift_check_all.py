"""Compatibility shim for ``eta_engine.scripts.drift_check_all``."""

from __future__ import annotations

from eta_engine.feeds._script_shim import build_script_shim

_script_module, __all__, __getattr__, __dir__ = build_script_shim(
    "eta_engine.feeds.drift_check_all",
    "eta_engine.scripts.drift_check_all",
)


def main() -> int:
    return _script_module.main()


if __name__ == "__main__":
    raise SystemExit(main())
