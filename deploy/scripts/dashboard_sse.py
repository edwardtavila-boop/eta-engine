"""SSE tail-follow generator (Wave-7, 2026-04-27).

Yields SSE-formatted events as new lines are appended to a JSONL file.
Handles missing files gracefully (yields nothing, retries) and skips
invalid JSON lines without crashing the stream.

Designed for the dashboard's /api/live/stream endpoint:
  * audit JSONL  -> 'verdict' events
  * fills JSONL  -> 'fill' events
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.5  # seconds


def _format_sse(event_type: str, data: dict) -> str:
    """Format a single SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def tail_follow(
    path: Path,
    *,
    event_type: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_iterations: int | None = None,
) -> AsyncIterator[str]:
    """Yield SSE-formatted events as new lines appear in ``path``.

    Re-resolves the path each iteration so midnight rotation
    (state/jarvis_audit/<today>.jsonl) is handled transparently.

    ``max_iterations`` lets tests bound the loop; None = run forever.
    """
    last_size = 0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        if not path.exists():
            await asyncio.sleep(poll_interval)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            await asyncio.sleep(poll_interval)
            continue
        if size > last_size:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    fh.seek(last_size)
                    new = fh.read()
                last_size = size
            except OSError as exc:
                logger.debug("tail read failed at %s: %s", path, exc)
                await asyncio.sleep(poll_interval)
                continue
            for line in new.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield _format_sse(event_type, row)
        elif size < last_size:
            # File rotated / truncated -- reset cursor
            last_size = 0
        await asyncio.sleep(poll_interval)


async def stream_audit_and_fills(
    audit_path: Path,
    fills_path: Path,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> AsyncIterator[str]:
    """Multiplex the two streams into one SSE stream."""
    audit_iter = tail_follow(audit_path, event_type="verdict", poll_interval=poll_interval)
    fills_iter = tail_follow(fills_path, event_type="fill", poll_interval=poll_interval)
    audit_task = asyncio.create_task(audit_iter.__anext__())
    fills_task = asyncio.create_task(fills_iter.__anext__())
    while True:
        done, _pending = await asyncio.wait(
            [audit_task, fills_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            try:
                yield task.result()
            except StopAsyncIteration:
                continue
            if task is audit_task:
                audit_task = asyncio.create_task(audit_iter.__anext__())
            elif task is fills_task:
                fills_task = asyncio.create_task(fills_iter.__anext__())
