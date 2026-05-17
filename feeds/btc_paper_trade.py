"""Compatibility shim for ``eta_engine.scripts.btc_paper_trade``."""

from __future__ import annotations

from eta_engine.feeds._script_shim import build_script_shim

_script_module, __all__, __getattr__, __dir__ = build_script_shim(
    "eta_engine.feeds.btc_paper_trade",
    "eta_engine.scripts.btc_paper_trade",
)


def main() -> None:
    _script_module.main()


if __name__ == "__main__":
    main()
