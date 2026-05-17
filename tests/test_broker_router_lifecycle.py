from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eta_engine.scripts.broker_router_lifecycle import BrokerRouterLifecycleDriver


def _make_driver(tmp_path: Path):
    counts = {"failed": 0}
    moved: list[tuple[Path, Path]] = []
    failed_moves: list[tuple[Path, dict[str, Any]]] = []
    events: list[tuple[str, str, str]] = []
    lifecycle_calls: list[tuple[Path, dict[str, Any]]] = []
    retry_meta_map: dict[Path, dict[str, Any]] = {}
    held_paths: set[Path] = set()

    async def _run_lifecycle(target: Path, *, retry_meta: dict[str, Any]) -> None:
        lifecycle_calls.append((target, dict(retry_meta)))

    driver = BrokerRouterLifecycleDriver(
        dry_run=False,
        processing_dir=tmp_path / "state" / "processing",
        retry_meta_suffix=".retry_meta.json",
        max_retries=3,
        interval_s=5.0,
        backoff_cap_s=300.0,
        counts=counts,
        empty_retry_meta=lambda: {"attempts": 0, "last_attempt_ts": "", "last_reject_reason": ""},
        hold_blocks_file=lambda path: path in held_paths,
        atomic_move=lambda src, dst: moved.append((src, dst)),
        load_retry_meta=lambda path: dict(retry_meta_map.get(path, {})),
        move_to_failed_with_meta=lambda path, meta: failed_moves.append((path, dict(meta))),
        record_event=lambda filename, kind, detail: events.append((filename, kind, detail)),
        run_lifecycle=_run_lifecycle,
        logger=logging.getLogger("test_broker_router_lifecycle"),
    )
    return driver, counts, moved, failed_moves, events, lifecycle_calls, retry_meta_map, held_paths


def test_process_pending_file_moves_then_runs_lifecycle(tmp_path: Path) -> None:
    driver, _counts, moved, _failed_moves, _events, lifecycle_calls, _retry_meta_map, _held_paths = _make_driver(tmp_path)
    path = tmp_path / "pending" / "alpha.pending_order.json"

    asyncio.run(driver.process_pending_file(path))

    assert moved == [(path, tmp_path / "state" / "processing" / path.name)]
    assert lifecycle_calls == [
        (tmp_path / "state" / "processing" / path.name, {"attempts": 0, "last_attempt_ts": "", "last_reject_reason": ""})
    ]


def test_process_retry_file_max_retries_moves_failed(tmp_path: Path) -> None:
    driver, counts, _moved, failed_moves, events, lifecycle_calls, retry_meta_map, _held_paths = _make_driver(tmp_path)
    target = tmp_path / "state" / "processing" / "alpha.pending_order.json"
    retry_meta_map[target] = {"attempts": 3, "last_attempt_ts": "", "last_reject_reason": "venue_rejected"}

    asyncio.run(driver.process_retry_file(target))

    assert counts["failed"] == 1
    assert events == [("alpha.pending_order.json", "failed", "max_retries_on_retry_scan")]
    assert failed_moves == [(target, {"attempts": 3, "last_attempt_ts": "", "last_reject_reason": "venue_rejected"})]
    assert lifecycle_calls == []


def test_should_backoff_and_retry_file_respects_elapsed_time(tmp_path: Path) -> None:
    driver, _counts, _moved, _failed_moves, _events, lifecycle_calls, retry_meta_map, _held_paths = _make_driver(tmp_path)
    target = tmp_path / "state" / "processing" / "alpha.pending_order.json"

    retry_meta_map[target] = {
        "attempts": 1,
        "last_attempt_ts": datetime.now(UTC).isoformat(),
        "last_reject_reason": "venue_rejected",
    }
    assert driver.should_backoff(retry_meta_map[target]) is True
    asyncio.run(driver.process_retry_file(target))
    assert lifecycle_calls == []

    retry_meta_map[target] = {
        "attempts": 1,
        "last_attempt_ts": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        "last_reject_reason": "venue_rejected",
    }
    assert driver.should_backoff(retry_meta_map[target]) is False
    asyncio.run(driver.process_retry_file(target))
    assert lifecycle_calls == [(target, retry_meta_map[target])]
