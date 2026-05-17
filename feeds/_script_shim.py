"""Helpers for feed compatibility shims that delegate to ``eta_engine.scripts``."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType


def build_script_shim(
    feed_module_name: str,
    script_module_name: str,
) -> tuple[ModuleType, list[str], Callable[[str], object], Callable[[], list[str]]]:
    """Build module-level delegation helpers for a feed/script compatibility pair."""
    script_module = import_module(script_module_name)
    module_public_names = getattr(script_module, "__all__", None)
    if module_public_names is None:
        module_public_names = [name for name in dir(script_module) if not name.startswith("_")]
    public_names = list(dict.fromkeys(module_public_names))
    directory_names = {"__all__", "__dir__", "__getattr__"}
    if hasattr(script_module, "main"):
        directory_names.add("main")

    def _module_getattr(name: str) -> object:
        try:
            return getattr(script_module, name)
        except AttributeError as exc:
            raise AttributeError(f"module {feed_module_name!r} has no attribute {name!r}") from exc

    def _module_dir() -> list[str]:
        return sorted(set(public_names) | directory_names)

    return script_module, public_names, _module_getattr, _module_dir
