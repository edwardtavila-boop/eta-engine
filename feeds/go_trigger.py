"""Compatibility shim for ``eta_engine.scripts.go_trigger``."""

from __future__ import annotations

from eta_engine.feeds._script_shim import build_script_shim

_script_module, __all__, __getattr__, __dir__ = build_script_shim(
    "eta_engine.feeds.go_trigger",
    "eta_engine.scripts.go_trigger",
)


def main() -> int:
    return _script_module.main()


if __name__ == "__main__":
    raise SystemExit(main())
