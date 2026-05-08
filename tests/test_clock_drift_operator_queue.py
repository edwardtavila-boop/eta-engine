"""Regression tests for clock-drift readiness surfacing."""

from __future__ import annotations

from email.utils import formatdate

from eta_engine.scripts import live_tiny_preflight_dryrun as preflight
from eta_engine.scripts import operator_action_queue


class _FakeHttpResponse:
    def __init__(self, date_header: str | None) -> None:
        self.headers = {}
        if date_header is not None:
            self.headers["Date"] = date_header

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return b"ok"


def test_clock_drift_gate_passes_when_date_header_is_close(monkeypatch) -> None:
    ticks = iter([1_000.0, 1_000.2])

    monkeypatch.setattr("time.time", lambda: next(ticks))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _FakeHttpResponse(formatdate(1_000, usegmt=True)),
    )

    gate = preflight._gate_clock_drift()

    assert gate.name == "clock_drift"
    assert gate.status == "PASS"
    assert gate.required is False
    assert gate.evidence["drift_seconds"] < 3.0


def test_clock_drift_gate_fails_when_date_header_is_far(monkeypatch) -> None:
    ticks = iter([1_000.0, 1_000.2])

    monkeypatch.setattr("time.time", lambda: next(ticks))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _FakeHttpResponse(formatdate(990, usegmt=True)),
    )

    gate = preflight._gate_clock_drift()

    assert gate.status == "FAIL"
    assert "sync NTP" in gate.detail or "timestamps will break" in gate.detail


def test_clock_drift_gate_skips_when_probe_has_no_date(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _FakeHttpResponse(None),
    )

    gate = preflight._gate_clock_drift()

    assert gate.status == "SKIP"
    assert "Date header" in gate.detail


def test_clock_drift_gate_is_registered_before_alert_probe() -> None:
    names = [name for name, _fn in preflight.GATE_FNS]

    assert "clock_drift" in names
    assert names.index("clock_drift") < names.index("alert_dispatcher_echo")


def test_op9_missing_clock_gate_is_observed_not_unknown() -> None:
    item = operator_action_queue._op9_clock_drift({"gates": []})

    assert item.verdict == operator_action_queue.VERDICT_OBSERVED
    assert item.evidence["launch_blocker"] is False
    assert item.evidence["gate_missing"] is True


def test_op9_skip_clock_gate_is_observed_not_blocked() -> None:
    item = operator_action_queue._op9_clock_drift(
        {"gates": [{"name": "clock_drift", "status": "SKIP", "detail": "offline"}]}
    )

    assert item.verdict == operator_action_queue.VERDICT_OBSERVED
    assert item.evidence["launch_blocker"] is False


def test_op9_fail_clock_gate_blocks_launch() -> None:
    item = operator_action_queue._op9_clock_drift(
        {"gates": [{"name": "clock_drift", "status": "FAIL", "detail": "drift 4s"}]}
    )

    assert item.verdict == operator_action_queue.VERDICT_BLOCKED
    assert item.evidence["launch_blocker"] is True
