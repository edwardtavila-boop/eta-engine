from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts.broker_router_factory import build_router

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def load_build_default_chain(*, root: Path, sys_path: list[str] | None = None) -> Callable[..., object]:
    """Lazy-import ``build_default_chain`` from the firm submodule."""
    search_path = sys_path if sys_path is not None else sys.path
    firm_src = Path(root).parent / "firm" / "eta_engine" / "src"
    if firm_src.is_dir() and str(firm_src) not in search_path:
        search_path.insert(0, str(firm_src))
    from mnq.risk.gate_chain import build_default_chain  # type: ignore[import-not-found]

    return build_default_chain


def resolve_pending_dir(arg: str | None, *, default_pending_dir: Path, env: dict[str, str] | None = None) -> Path:
    source_env = env if env is not None else os.environ
    if arg:
        return Path(arg)
    env_value = source_env.get("ETA_BROKER_ROUTER_PENDING_DIR")
    if env_value:
        return Path(env_value)
    return Path(default_pending_dir)


def resolve_state_root(arg: str | None, *, default_state_root: Path, env: dict[str, str] | None = None) -> Path:
    source_env = env if env is not None else os.environ
    if arg:
        return Path(arg)
    env_value = source_env.get("ETA_BROKER_ROUTER_STATE_ROOT")
    if env_value:
        return Path(env_value)
    return Path(default_state_root)


def resolve_interval(
    arg: float | None,
    *,
    default_interval_s: float,
    logger: logging.Logger,
    env: dict[str, str] | None = None,
) -> float:
    source_env = env if env is not None else os.environ
    if arg is not None:
        return float(arg)
    env_value = source_env.get("ETA_BROKER_ROUTER_INTERVAL_S")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            logger.warning("invalid ETA_BROKER_ROUTER_INTERVAL_S=%r; using default", env_value)
    return float(default_interval_s)


def resolve_dry_run(arg: bool, *, env: dict[str, str] | None = None) -> bool:
    source_env = env if env is not None else os.environ
    if arg:
        return True
    return source_env.get("ETA_BROKER_ROUTER_DRY_RUN", "").strip() in ("1", "true", "yes")


def resolve_max_retries(
    arg: int | None,
    *,
    default_max_retries: int,
    env: dict[str, str] | None = None,
) -> int:
    source_env = env if env is not None else os.environ
    if arg is not None:
        return int(arg)
    env_value = source_env.get("ETA_BROKER_ROUTER_MAX_RETRIES")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    return int(default_max_retries)


def build_parser(*, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="broker_router",
        description=description,
    )
    parser.add_argument("--interval", type=float, default=None, help="Poll interval seconds (default 5).")
    parser.add_argument(
        "--pending-dir", type=str, default=None, help="Where the supervisor writes *.pending_order.json files."
    )
    parser.add_argument(
        "--state-root", type=str, default=None, help="Router state root for processing/blocked/archive."
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and gate-check, but do not submit or move files.")
    parser.add_argument("--once", action="store_true", help="Single pass, then exit.")
    parser.add_argument("--max-retries", type=int, default=None, help="Max venue rejections before moving to failed/.")
    parser.add_argument("--log-level", type=str, default="INFO")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    description: str,
    default_pending_dir: Path,
    default_state_root: Path,
    default_interval_s: float,
    default_max_retries: int,
    broker_router_cls: type,
    smart_router_cls: type,
    default_journal_factory: Callable[[], object],
    logger: logging.Logger,
    logging_module=logging,
    asyncio_run: Callable[[object], object] = asyncio.run,
    build_router_fn: Callable[..., object] = build_router,
    env: dict[str, str] | None = None,
) -> int:
    parser = build_parser(description=description)
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging_module.basicConfig(
        level=getattr(logging_module, args.log_level.upper(), logging_module.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    pending_dir = resolve_pending_dir(args.pending_dir, default_pending_dir=default_pending_dir, env=env)
    state_root = resolve_state_root(args.state_root, default_state_root=default_state_root, env=env)
    interval_s = resolve_interval(
        args.interval,
        default_interval_s=default_interval_s,
        logger=logger,
        env=env,
    )
    dry_run = resolve_dry_run(args.dry_run, env=env)
    max_retries = resolve_max_retries(
        args.max_retries,
        default_max_retries=default_max_retries,
        env=env,
    )

    router = build_router_fn(
        pending_dir=pending_dir,
        state_root=state_root,
        interval_s=interval_s,
        dry_run=dry_run,
        max_retries=max_retries,
        broker_router_cls=broker_router_cls,
        smart_router_cls=smart_router_cls,
        journal_factory=default_journal_factory,
    )
    if args.once:
        asyncio_run(router.run_once())
    else:
        asyncio_run(router.run())
    return 0
