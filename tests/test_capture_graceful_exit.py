"""Regression tests for capture_depth_snapshots + capture_tick_stream
graceful-exit behavior.

2026-05-13: subscription-gap errors (CME Depth of Book, real-time ticks)
must NOT crash with rc=1. The operator-facing scheduled tasks need to
distinguish "ops backlog item" (missing paid IBKR data subscription) from
"real bug" so the task runner doesn't fire alarms 96 times/day.
"""

from __future__ import annotations

import argparse

# ── capture_depth_snapshots ─────────────────────────────────────────


def test_depth_capture_returns_0_on_connection_refused(monkeypatch) -> None:
    """If IBKR gateway is down (cold-start ordering), exit cleanly."""
    from eta_engine.scripts import capture_depth_snapshots as mod

    def boom_connect(self: object) -> None:  # noqa: ARG001
        raise ConnectionRefusedError("port 4002 refused")

    monkeypatch.setattr(mod.DepthSnapshotCapture, "connect", boom_connect)
    args = argparse.Namespace(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=99,
        depth_rows=5,
        snapshot_interval_ms=1000,
        log_level="WARNING",
    )
    rc = mod._run_capture(args)
    assert rc == 0


def test_depth_capture_returns_0_on_missing_subscription(monkeypatch) -> None:
    """Subscription-gap RuntimeError -> exit 0 + warning log."""
    from eta_engine.scripts import capture_depth_snapshots as mod

    monkeypatch.setattr(mod.DepthSnapshotCapture, "connect", lambda self: None)

    def no_subscription(self: object) -> None:  # noqa: ARG001
        raise RuntimeError("MNQ: no contract details returned by IBKR")

    monkeypatch.setattr(mod.DepthSnapshotCapture, "subscribe", no_subscription)
    args = argparse.Namespace(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=99,
        depth_rows=5,
        snapshot_interval_ms=1000,
        log_level="WARNING",
    )
    rc = mod._run_capture(args)
    assert rc == 0


def test_depth_capture_returns_1_on_unexpected_error(monkeypatch) -> None:
    """Genuine bugs still surface as rc=1 (not silenced)."""
    from eta_engine.scripts import capture_depth_snapshots as mod

    monkeypatch.setattr(mod.DepthSnapshotCapture, "connect", lambda self: None)

    def real_bug(self: object) -> None:  # noqa: ARG001
        raise RuntimeError("unrelated bug — division by zero")

    monkeypatch.setattr(mod.DepthSnapshotCapture, "subscribe", real_bug)
    args = argparse.Namespace(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=99,
        depth_rows=5,
        snapshot_interval_ms=1000,
        log_level="WARNING",
    )
    rc = mod._run_capture(args)
    assert rc == 1


# ── capture_tick_stream ─────────────────────────────────────────────


def test_tick_capture_returns_0_on_connection_refused(monkeypatch) -> None:
    from eta_engine.scripts import capture_tick_stream as mod

    def boom_connect(self: object) -> None:  # noqa: ARG001
        raise ConnectionRefusedError("port 4002 refused")

    # Find the capture class (different name vs depth)
    cls = next(
        v for k, v in vars(mod).items()
        if isinstance(v, type) and "Capture" in k and v.__module__ == mod.__name__
    )
    monkeypatch.setattr(cls, "connect", boom_connect)
    args = argparse.Namespace(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=99,
        log_level="WARNING",
    )
    rc = mod._run_capture(args)
    assert rc == 0


def test_tick_capture_returns_0_on_missing_subscription(monkeypatch) -> None:
    from eta_engine.scripts import capture_tick_stream as mod

    cls = next(
        v for k, v in vars(mod).items()
        if isinstance(v, type) and "Capture" in k and v.__module__ == mod.__name__
    )
    monkeypatch.setattr(cls, "connect", lambda self: None)

    def no_subscription(self: object) -> None:  # noqa: ARG001
        raise RuntimeError("market data subscription required for CME")

    monkeypatch.setattr(cls, "subscribe", no_subscription)
    monkeypatch.setattr(cls, "stats", lambda self: {})
    args = argparse.Namespace(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=99,
        log_level="WARNING",
    )
    rc = mod._run_capture(args)
    assert rc == 0


def test_tick_capture_returns_0_on_operator_session_blocker(monkeypatch) -> None:
    from eta_engine.scripts import capture_tick_stream as mod

    cls = next(
        v for k, v in vars(mod).items()
        if isinstance(v, type) and "Capture" in k and v.__module__ == mod.__name__
    )
    monkeypatch.setattr(cls, "connect", lambda self: None)
    monkeypatch.setattr(cls, "subscribe", lambda self: None)

    def blocked_run(self: object) -> None:
        self._blocked_reason = {
            "code": 10189,
            "summary": (
                "Tick-by-tick data blocked because another trading TWS session is connected "
                "from a different IP address."
            ),
        }

    monkeypatch.setattr(cls, "run", blocked_run)
    monkeypatch.setattr(cls, "stats", lambda self: {})
    args = argparse.Namespace(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=99,
        log_level="WARNING",
    )
    rc = mod._run_capture(args)
    assert rc == 0


def test_tick_capture_returns_1_on_unexpected_error(monkeypatch) -> None:
    from eta_engine.scripts import capture_tick_stream as mod

    cls = next(
        v for k, v in vars(mod).items()
        if isinstance(v, type) and "Capture" in k and v.__module__ == mod.__name__
    )
    monkeypatch.setattr(cls, "connect", lambda self: None)

    def real_bug(self: object) -> None:  # noqa: ARG001
        raise RuntimeError("unrelated bug — KeyError in processor")

    monkeypatch.setattr(cls, "subscribe", real_bug)
    monkeypatch.setattr(cls, "stats", lambda self: {})
    args = argparse.Namespace(
        symbols=["MNQ"],
        host="127.0.0.1",
        port=4002,
        client_id=99,
        log_level="WARNING",
    )
    rc = mod._run_capture(args)
    assert rc == 1


def test_tick_ops_blocker_detects_different_ip() -> None:
    from eta_engine.scripts import capture_tick_stream as mod

    blocker = mod._tick_ops_blocker(
        10189,
        "Failed to request tick-by-tick data. Trading TWS session is connected from a different IP address",
    )
    assert blocker is not None
    assert blocker["code"] == 10189
    assert blocker["slug"] == "different_ip_trading_session"
