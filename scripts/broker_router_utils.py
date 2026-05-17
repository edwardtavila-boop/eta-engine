from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

from eta_engine.scripts.broker_router_entrypoint import load_build_default_chain


def env_int(name: str, default: int, *, logger: object, environ: dict[str, str] | None = None) -> int:
    source = os.environ if environ is None else environ
    try:
        return int(str(source.get(name, "")).strip() or default)
    except ValueError:
        logger.warning("invalid integer env %s=%r; using %s", name, source.get(name), default)
        return default


def env_float(name: str, default: float, *, logger: object, environ: dict[str, str] | None = None) -> float:
    source = os.environ if environ is None else environ
    try:
        return float(str(source.get(name, "")).strip() or default)
    except ValueError:
        logger.warning("invalid float env %s=%r; using %s", name, source.get(name), default)
        return default


def gate_bootstrap_enabled(*, environ: dict[str, str] | None = None, env_name: str = "ETA_GATE_BOOTSTRAP") -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(env_name, "")).strip() == "1"


def readiness_enforced(
    *, environ: dict[str, str] | None = None, env_name: str = "ETA_BROKER_ROUTER_ENFORCE_READINESS"
) -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(env_name, "")).strip() == "1"


def truthy_env(name: str, *, environ: dict[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(name, "")).strip().lower() in {"1", "true", "yes", "on", "y"}


def load_build_default_chain_for_router(*, root: Path, sys_path: Sequence[str]) -> Callable[..., object]:
    return load_build_default_chain(root=root, sys_path=sys_path)


def first_nonempty_text(*values: object) -> str:
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def extract_broker_fill_ts(result: object) -> str:
    """Best-effort broker fill timestamp from an OrderResult."""
    canonical = first_nonempty_text(getattr(result, "filled_at", None))
    if canonical:
        return canonical
    raw = getattr(result, "raw", None)
    if not isinstance(raw, dict):
        return ""
    server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
    direct = first_nonempty_text(
        raw.get("filled_at"),
        raw.get("execution_time"),
        raw.get("executed_at"),
        server.get("filled-at"),
        server.get("filled_at"),
        server.get("execution-time"),
        server.get("execution_time"),
        server.get("executed-at"),
        server.get("executed_at"),
        server.get("updated-at"),
        server.get("updated_at"),
    )
    if direct:
        return direct
    ib_statuses = raw.get("ib_statuses")
    if isinstance(ib_statuses, list):
        for item in ib_statuses:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").strip().lower() != "filled":
                continue
            candidate = first_nonempty_text(
                item.get("filled_at"),
                item.get("execution_time"),
                item.get("executed_at"),
                item.get("time"),
                item.get("timestamp"),
                item.get("lastFillTime"),
            )
            if candidate:
                return candidate
    return ""
