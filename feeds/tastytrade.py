"""Compatibility shim for the canonical Tastytrade venue adapter."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


_MODULE_NAME = "eta_engine.feeds.tastytrade"
_TARGET_NAME = "eta_engine.venues.tastytrade"
_target: ModuleType = import_module(_TARGET_NAME)
_public_names = getattr(_target, "__all__", None)
if _public_names is None:
    _public_names = [name for name in dir(_target) if not name.startswith("_")]
__all__ = list(dict.fromkeys(_public_names))


def __getattr__(name: str) -> object:
    try:
        return getattr(_target, name)
    except AttributeError as exc:
        raise AttributeError(f"module {_MODULE_NAME!r} has no attribute {name!r}") from exc


def __dir__() -> list[str]:
    return sorted(set(__all__) | {"__all__", "__dir__", "__getattr__"})
