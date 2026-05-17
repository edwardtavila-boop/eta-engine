"""Compatibility shim for ``eta_engine.scripts.workspace_roots``."""

from __future__ import annotations

from eta_engine.feeds._script_shim import build_script_shim

_script_module, __all__, __getattr__, __dir__ = build_script_shim(
    "eta_engine.feeds.workspace_roots",
    "eta_engine.scripts.workspace_roots",
)
