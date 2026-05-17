"""Compatibility shim for ``eta_engine.scripts.score_policy_candidate``."""

from __future__ import annotations

from eta_engine.feeds._script_shim import build_script_shim

_script_module, __all__, __getattr__, __dir__ = build_script_shim(
    "eta_engine.feeds.score_policy_candidate",
    "eta_engine.scripts.score_policy_candidate",
)


def main(argv: list[str] | None = None) -> int:
    return _script_module.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
