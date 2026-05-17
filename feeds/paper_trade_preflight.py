"""Compatibility shim for ``eta_engine.scripts.paper_trade_preflight``."""

from __future__ import annotations

from eta_engine.feeds._script_shim import build_script_shim

_script_module, __all__, __getattr__, __dir__ = build_script_shim(
    "eta_engine.feeds.paper_trade_preflight",
    "eta_engine.scripts.paper_trade_preflight",
)


def main(argv: list[str] | None = None) -> int:
    return _script_module.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
