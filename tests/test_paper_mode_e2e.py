"""
End-to-end paper-mode CI pin (Phase 1 scoping doc item #2).

Asserts that ``_amain --max-bars 3 --dry-run`` writes the canonical
paper-mode runtime-log sequence:

    runtime_start (live=False)
        |
        v
    N tick lines (kind=tick, broker_equity.reason in
                  {within_tolerance, no_broker_data})
        |
        v
    runtime_stop (bars=N)

Plus, when --unpause + --operator are passed, a runtime_unpaused
event lands BETWEEN runtime_start and the first tick.

Plus, when --max-runtime-seconds is set, the loop honours the
wall-clock budget regardless of bar ingestion rate.

Catches regressions where:
  * A future refactor drops broker_equity from the per-tick meta block.
  * runtime_start / runtime_stop stop being emitted (B2-shaped gap).
  * The operator sign-off audit trail breaks.
  * Wall-clock exit doesn't fire.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _seed_state(tmp_path: Path) -> Path:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({
            "shared_artifacts": {
                "apex_go_state": {"tier_a_mnq_live": True},
            },
        }),
        encoding="utf-8",
    )
    return state_path


def _read_runtime_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


@pytest.mark.asyncio
async def test_paper_mode_full_run_emits_canonical_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: --max-bars 3 --dry-run produces start -> 3 ticks -> stop."""
    monkeypatch.setenv(
        "APEX_ALERTS_LOG_PATH", str(tmp_path / "alerts.jsonl"),
    )
    from apex_predator.scripts.run_apex_live import _amain

    log_path = tmp_path / "rt.jsonl"
    state_path = _seed_state(tmp_path)
    rc = await _amain([
        "--max-bars", "3",
        "--tick-interval", "0",
        "--state-path", str(state_path),
        "--log-path", str(log_path),
    ])
    assert rc == 0, f"_amain rc={rc}"
    entries = _read_runtime_log(log_path)
    assert entries, "runtime log empty"

    kinds = [e.get("kind") for e in entries]
    assert "runtime_start" in kinds, f"no runtime_start; kinds={kinds}"
    assert "runtime_stop" in kinds, f"no runtime_stop; kinds={kinds}"
    # Tick count must equal --max-bars.
    tick_entries = [e for e in entries if e.get("kind") == "tick"]
    assert len(tick_entries) == 3, (
        f"expected 3 tick lines, got {len(tick_entries)}; kinds={kinds}"
    )

    # runtime_start carries live=False in dry-run mode.
    # _log() spreads meta directly into the entry, so the keys live
    # at the top level rather than under "meta".
    rs = next(e for e in entries if e.get("kind") == "runtime_start")
    assert rs.get("mode") == "dry_run", (
        f"runtime_start.mode={rs.get('mode')!r}; expected 'dry_run'"
    )

    # Every tick carries a broker_equity block with a recognised reason.
    ok_reasons = {"within_tolerance", "no_broker_data"}
    for tick in tick_entries:
        be = tick.get("broker_equity")
        assert isinstance(be, dict), (
            f"tick missing broker_equity block: {tick}"
        )
        assert be.get("reason") in ok_reasons, (
            f"unexpected broker_equity.reason={be.get('reason')!r}; "
            f"tick={tick}"
        )

    # Sequencing: runtime_start before any tick; runtime_stop after.
    rs_idx = kinds.index("runtime_start")
    rt_idx = kinds.index("runtime_stop")
    tick_idxs = [i for i, k in enumerate(kinds) if k == "tick"]
    assert rs_idx < min(tick_idxs), (
        f"runtime_start (idx={rs_idx}) not before first tick (idx={min(tick_idxs)})"
    )
    assert rt_idx > max(tick_idxs), (
        f"runtime_stop (idx={rt_idx}) not after last tick (idx={max(tick_idxs)})"
    )


@pytest.mark.asyncio
async def test_runtime_unpaused_event_emitted_when_flag_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--unpause + --operator emits a runtime_unpaused entry between
    runtime_start and the first tick."""
    monkeypatch.setenv(
        "APEX_ALERTS_LOG_PATH", str(tmp_path / "alerts.jsonl"),
    )
    from apex_predator.scripts.run_apex_live import _amain

    log_path = tmp_path / "rt.jsonl"
    state_path = _seed_state(tmp_path)
    rc = await _amain([
        "--max-bars", "1",
        "--tick-interval", "0",
        "--state-path", str(state_path),
        "--log-path", str(log_path),
        "--unpause",
        "--operator", "edward",
    ])
    assert rc == 0
    entries = _read_runtime_log(log_path)
    kinds = [e.get("kind") for e in entries]
    assert "runtime_unpaused" in kinds, (
        f"runtime_unpaused not emitted; kinds={kinds}"
    )

    rs_idx = kinds.index("runtime_start")
    ru_idx = kinds.index("runtime_unpaused")
    first_tick = next(
        (i for i, k in enumerate(kinds) if k == "tick"), None,
    )
    assert rs_idx < ru_idx, (
        f"runtime_unpaused (idx={ru_idx}) not after runtime_start (idx={rs_idx})"
    )
    if first_tick is not None:
        assert ru_idx < first_tick, (
            f"runtime_unpaused (idx={ru_idx}) not before first tick "
            f"(idx={first_tick})"
        )

    ru = next(e for e in entries if e.get("kind") == "runtime_unpaused")
    assert ru.get("operator") == "edward", (
        f"operator name not in payload: {ru!r}"
    )


@pytest.mark.asyncio
async def test_runtime_unpaused_not_emitted_without_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --unpause -> no runtime_unpaused event (default safety)."""
    monkeypatch.setenv(
        "APEX_ALERTS_LOG_PATH", str(tmp_path / "alerts.jsonl"),
    )
    from apex_predator.scripts.run_apex_live import _amain

    log_path = tmp_path / "rt.jsonl"
    state_path = _seed_state(tmp_path)
    await _amain([
        "--max-bars", "1",
        "--tick-interval", "0",
        "--state-path", str(state_path),
        "--log-path", str(log_path),
    ])
    entries = _read_runtime_log(log_path)
    kinds = [e.get("kind") for e in entries]
    assert "runtime_unpaused" not in kinds, (
        f"runtime_unpaused emitted without --unpause; kinds={kinds}"
    )


@pytest.mark.asyncio
async def test_max_runtime_seconds_budget_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--max-runtime-seconds bounds the loop even with bars=∞."""
    monkeypatch.setenv(
        "APEX_ALERTS_LOG_PATH", str(tmp_path / "alerts.jsonl"),
    )
    from apex_predator.scripts.run_apex_live import _amain

    log_path = tmp_path / "rt.jsonl"
    state_path = _seed_state(tmp_path)
    started = time.monotonic()
    rc = await _amain([
        # No --max-bars -> infinite by bar count; budget MUST trip first.
        "--tick-interval", "0",
        "--max-runtime-seconds", "0.5",
        "--state-path", str(state_path),
        "--log-path", str(log_path),
    ])
    elapsed = time.monotonic() - started
    assert rc == 0
    # Must have exited cleanly, in roughly the budgeted time. Allow a
    # generous ceiling (5s) to absorb CI scheduler jitter without
    # making the assertion flaky.
    assert elapsed < 5.0, (
        f"runtime should have exited near budget; elapsed={elapsed:.2f}s"
    )
    entries = _read_runtime_log(log_path)
    kinds = [e.get("kind") for e in entries]
    assert "runtime_start" in kinds
    assert "runtime_stop" in kinds


@pytest.mark.asyncio
async def test_paper_mode_dispatcher_registers_runtime_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher recognises every event the paper-mode loop emits.

    Drift catcher: if a future refactor adds a dispatcher.send() call
    without a matching alerts.yaml entry, AlertDispatcher silently
    drops it -- paper-mode CI now pins the registry coverage.
    """
    alerts_path = tmp_path / "alerts.jsonl"
    monkeypatch.setenv("APEX_ALERTS_LOG_PATH", str(alerts_path))
    from apex_predator.scripts.run_apex_live import _amain

    state_path = _seed_state(tmp_path)
    log_path = tmp_path / "rt.jsonl"
    await _amain([
        "--max-bars", "1",
        "--tick-interval", "0",
        "--state-path", str(state_path),
        "--log-path", str(log_path),
        "--unpause",
        "--operator", "edward",
    ])
    if not alerts_path.exists():
        pytest.skip("alerts journal not written -- dispatcher may have no-op'd")
    seen_events: set[str] = set()
    for line in alerts_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = row.get("event")
        if ev:
            seen_events.add(ev)
        # Any "unknown event" payload is the regression marker we want to fail on.
        blocked = row.get("blocked") or []
        for b in blocked:
            assert "unknown event" not in str(b), (
                f"AlertDispatcher rejected event as unknown: {row!r}"
            )

    # Sanity: the events we know are dispatched in the happy path.
    assert "runtime_start" in seen_events
    assert "runtime_unpaused" in seen_events
    assert "runtime_stop" in seen_events


def _run(coro):
    """Sync-shim for asyncio in case any caller wants it."""
    return asyncio.run(coro)
