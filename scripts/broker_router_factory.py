from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def build_router(
    *,
    pending_dir: Path,
    state_root: Path,
    interval_s: float,
    dry_run: bool,
    max_retries: int,
    broker_router_cls: type,
    smart_router_cls: type,
    journal_factory: Callable[[], object],
) -> object:
    """Construct a broker-router instance with injected runtime dependencies."""
    smart_router = smart_router_cls()
    journal = journal_factory()
    return broker_router_cls(
        pending_dir=Path(pending_dir),
        state_root=Path(state_root),
        smart_router=smart_router,
        journal=journal,
        interval_s=float(interval_s),
        dry_run=bool(dry_run),
        max_retries=int(max_retries),
    )
