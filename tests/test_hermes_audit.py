"""Tests for Half 4 of the JARVIS<->Hermes bridge: observability + safety.

Two surfaces exercised:

1. ``eta_engine.scripts.kaizen_loop.run_loop`` now emits a
   ``report["hermes_health"]`` section. We monkeypatch the module-level
   audit-log path so the read points at ``tmp_path`` rather than the
   real workspace state file, and assert the per-tool / auth-failure
   counts come out correct. The probe is best-effort: a missing or
   malformed log must surface as zeros, never as an exception.

2. ``eta_engine.brain.jarvis_v3.hermes_bridge.send_hermes_backoff_alert``
   fires a one-shot Telegram alert when the Hermes hot path enters or
   exits backoff. Rate-limited to one outage alert per 10 minutes (so a
   sustained outage doesn't spam the operator), but ``recovered=True``
   always sends so the operator sees the all-clear.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

from eta_engine.brain.jarvis_v3 import hermes_bridge
from eta_engine.scripts import kaizen_loop

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# Shared dependency mocks for run_loop (mirrors test_kaizen_loop_supercharge_hooks)
# ---------------------------------------------------------------------------


def _run_loop_with_audit_path(
    audit_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Invoke run_loop with all heavyweight deps stubbed out and the
    hermes audit-log path redirected to ``audit_path``.
    """
    monkeypatch.setattr(kaizen_loop, "_HERMES_AUDIT_LOG_PATH", audit_path)
    with (
        patch(
            "eta_engine.scripts.elite_scoreboard.analyze",
            return_value={"bots": {}, "tier_counts": {}},
        ),
        patch(
            "eta_engine.scripts.monte_carlo_validator.analyze",
            return_value={"bots": {}, "verdict_counts": {}},
        ),
        patch(
            "eta_engine.scripts.jarvis_wiring_audit.audit",
            return_value=[],
        ),
        patch(
            "eta_engine.brain.jarvis_v3.hot_learner.decay_overnight",
        ),
    ):
        return kaizen_loop.run_loop(
            since_iso=None, bootstraps=10, apply_actions=False,
        )


def _audit_record(
    *,
    ts: datetime,
    tool: str = "tool_a",
    auth: str = "ok",
) -> str:
    """Build a single audit-log line. ``ts`` is serialised with the
    trailing ``Z`` suffix that the production writer uses."""
    return json.dumps(
        {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": tool,
            "auth": auth,
        },
    )


# ---------------------------------------------------------------------------
# kaizen_loop hermes_health section
# ---------------------------------------------------------------------------


def test_hermes_health_present_when_audit_log_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing audit log shouldn't crash the pass — hermes_health
    appears with zeros."""
    missing = tmp_path / "hermes_actions.jsonl"
    assert not missing.exists()

    report = _run_loop_with_audit_path(missing, monkeypatch)

    assert "hermes_health" in report
    hh = report["hermes_health"]
    assert hh["calls_today"] == 0
    assert hh["calls_by_tool"] == {}
    assert hh["auth_failures_today"] == 0
    # Defaults for the probe-side fields when hermes_client isn't installed.
    assert hh["backoff_active"] is False


def test_hermes_health_counts_calls_in_24h(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Records inside the last 24h are counted; older records are dropped."""
    now = datetime.now(UTC)
    audit = tmp_path / "hermes_actions.jsonl"
    lines = [
        # Inside the window (3)
        _audit_record(ts=now - timedelta(minutes=5)),
        _audit_record(ts=now - timedelta(hours=2)),
        _audit_record(ts=now - timedelta(hours=23, minutes=30)),
        # Outside the window (2)
        _audit_record(ts=now - timedelta(hours=25)),
        _audit_record(ts=now - timedelta(days=3)),
    ]
    audit.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = _run_loop_with_audit_path(audit, monkeypatch)

    assert report["hermes_health"]["calls_today"] == 3


def test_hermes_health_groups_by_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-tool call counts surface in calls_by_tool."""
    now = datetime.now(UTC)
    audit = tmp_path / "hermes_actions.jsonl"
    lines = [
        _audit_record(ts=now - timedelta(minutes=1), tool="tool_a"),
        _audit_record(ts=now - timedelta(minutes=2), tool="tool_b"),
        _audit_record(ts=now - timedelta(minutes=3), tool="tool_b"),
    ]
    audit.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = _run_loop_with_audit_path(audit, monkeypatch)

    assert report["hermes_health"]["calls_by_tool"] == {
        "tool_a": 1,
        "tool_b": 2,
    }


def test_hermes_health_counts_auth_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Records with ``auth: "failed"`` increment the auth-failure counter."""
    now = datetime.now(UTC)
    audit = tmp_path / "hermes_actions.jsonl"
    lines = [
        _audit_record(ts=now - timedelta(minutes=1), auth="failed"),
        _audit_record(ts=now - timedelta(minutes=2), auth="failed"),
        _audit_record(ts=now - timedelta(minutes=3), auth="ok"),
    ]
    audit.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = _run_loop_with_audit_path(audit, monkeypatch)

    hh = report["hermes_health"]
    assert hh["auth_failures_today"] == 2
    assert hh["calls_today"] == 3


# ---------------------------------------------------------------------------
# send_hermes_backoff_alert — rate-limit semantics
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run a coroutine to completion using a fresh event loop.

    ``asyncio.get_event_loop()`` is deprecated on 3.14 and can return a
    loop that pytest-asyncio has already torn down — when that happens
    the coroutine returned by the wrapper is never actually awaited and
    you get a ``RuntimeWarning: coroutine '...' was never awaited`` plus
    spooky test failures whose root cause looks like state pollution.
    ``asyncio.run`` creates and closes its own loop every call, so this
    helper stays robust regardless of pytest-asyncio's mode.
    """
    return asyncio.run(coro)


def test_backoff_alert_rate_limited_to_10min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two activation-side calls within 10 minutes: the first sends, the
    second short-circuits and returns False without re-alerting."""
    # Reset the monotonic-time gate so this test starts at a clean slate.
    monkeypatch.setattr(hermes_bridge, "_LAST_BACKOFF_ALERT_AT", 0.0)

    sent: list[tuple] = []

    async def _fake_send(title, message, level="INFO"):
        sent.append((title, message, level))
        return True

    monkeypatch.setattr(hermes_bridge, "send_alert", _fake_send)

    first = _run_async(hermes_bridge.send_hermes_backoff_alert())
    second = _run_async(hermes_bridge.send_hermes_backoff_alert())

    assert first is True
    assert second is False
    assert len(sent) == 1


def test_backoff_alert_recovered_bypasses_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recovered=True always fires — the operator needs to see the
    all-clear regardless of when the last outage alert went out."""
    # Force the rate-limit window to be "active" by stamping the gate
    # at a recent monotonic timestamp. ``recovered=True`` must still send.
    import time as _time
    monkeypatch.setattr(
        hermes_bridge, "_LAST_BACKOFF_ALERT_AT", _time.monotonic(),
    )

    sent: list[tuple] = []

    async def _fake_send(title, message, level="INFO"):
        sent.append((title, message, level))
        return True

    monkeypatch.setattr(hermes_bridge, "send_alert", _fake_send)

    result = _run_async(hermes_bridge.send_hermes_backoff_alert(recovered=True))

    assert result is True
    assert len(sent) == 1
    title, _msg, _level = sent[0]
    assert "recovered" in title.lower()
