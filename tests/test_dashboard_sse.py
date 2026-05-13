"""Tests for SSE tail-follow generator."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_tail_yields_new_lines(tmp_path: Path) -> None:
    """Appending to the file emits SSE events."""
    from eta_engine.deploy.scripts.dashboard_sse import tail_follow

    audit = tmp_path / "audit.jsonl"
    audit.write_text("", encoding="utf-8")

    received: list[str] = []

    async def collect() -> None:
        async for event in tail_follow(audit, event_type="verdict", poll_interval=0.05, max_iterations=10):
            received.append(event)
            if len(received) >= 2:
                return

    async def feed() -> None:
        await asyncio.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"a": 1}) + "\n")
        await asyncio.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"a": 2}) + "\n")

    await asyncio.gather(collect(), feed())
    assert len(received) == 2
    assert "event: verdict" in received[0]
    assert "data: " in received[0]
    assert '"a": 1' in received[0] or '"a":1' in received[0]


@pytest.mark.asyncio
async def test_tail_handles_missing_file_gracefully(tmp_path: Path) -> None:
    """A missing file should yield no events and not crash."""
    from eta_engine.deploy.scripts.dashboard_sse import tail_follow

    received = []
    async for event in tail_follow(
        tmp_path / "missing.jsonl", event_type="verdict", poll_interval=0.05, max_iterations=3
    ):
        received.append(event)
    assert received == []


@pytest.mark.asyncio
async def test_tail_skips_invalid_json(tmp_path: Path) -> None:
    """Garbage lines don't break the stream."""
    from eta_engine.deploy.scripts.dashboard_sse import tail_follow

    audit = tmp_path / "audit.jsonl"
    audit.write_text("", encoding="utf-8")

    received = []

    async def collect() -> None:
        async for event in tail_follow(audit, event_type="verdict", poll_interval=0.05, max_iterations=20):
            received.append(event)
            if len(received) >= 1:
                return

    async def feed() -> None:
        await asyncio.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"good": True}) + "\n")

    await asyncio.gather(collect(), feed())
    assert len(received) == 1
    assert "good" in received[0]
