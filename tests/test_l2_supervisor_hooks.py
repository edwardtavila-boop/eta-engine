"""Tests for l2_supervisor_hooks — drop-in hooks for the live order
supervisor."""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import l2_observability as obs
from eta_engine.scripts import l2_supervisor_hooks as hooks
from eta_engine.strategies import trading_gate

if TYPE_CHECKING:
    import pytest

# ────────────────────────────────────────────────────────────────────
# Stub bot + rec dataclasses (mirror supervisor shape)
# ────────────────────────────────────────────────────────────────────


@dataclass
class _StubBot:
    bot_id: str
    strategy_id: str
    symbol: str


@dataclass
class _StubRec:
    signal_id: str
    symbol: str
    side: str
    qty: float
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    confidence: float = 0.0
    rationale: str = ""


# ────────────────────────────────────────────────────────────────────
# pre_trade_check
# ────────────────────────────────────────────────────────────────────


def test_pre_trade_check_allows_when_no_blocking_signals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With GREEN disk + GREEN capture, gate allows."""
    monkeypatch.setattr(trading_gate, "DISK_LOG", tmp_path / "disk.jsonl")
    monkeypatch.setattr(trading_gate, "CAPTURE_HEALTH_LOG", tmp_path / "cap.jsonl")
    monkeypatch.setattr(trading_gate, "GATE_LOG", tmp_path / "gate.jsonl")
    trading_gate._reset_cache_for_tests()
    now_iso = datetime.now(UTC).isoformat()
    for path, content in [
        (tmp_path / "disk.jsonl", {"ts": now_iso, "verdict": "GREEN"}),
        (tmp_path / "cap.jsonl", {"ts": now_iso, "verdict": "GREEN"}),
    ]:
        path.write_text(json.dumps(content) + "\n", encoding="utf-8")
    bot = _StubBot(bot_id="test_bot", strategy_id="test", symbol="MNQ")
    rec = _StubRec(signal_id="sig-1", symbol="MNQ", side="BUY", qty=1)
    assert hooks.pre_trade_check(bot, rec) is True


def test_pre_trade_check_blocks_when_disk_critical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_gate, "DISK_LOG", tmp_path / "disk.jsonl")
    monkeypatch.setattr(trading_gate, "CAPTURE_HEALTH_LOG", tmp_path / "cap.jsonl")
    monkeypatch.setattr(trading_gate, "GATE_LOG", tmp_path / "gate.jsonl")
    trading_gate._reset_cache_for_tests()
    now_iso = datetime.now(UTC).isoformat()
    (tmp_path / "disk.jsonl").write_text(json.dumps({"ts": now_iso, "verdict": "CRITICAL"}) + "\n", encoding="utf-8")
    (tmp_path / "cap.jsonl").write_text(json.dumps({"ts": now_iso, "verdict": "GREEN"}) + "\n", encoding="utf-8")
    bot = _StubBot(bot_id="test_bot", strategy_id="test", symbol="MNQ")
    rec = _StubRec(signal_id="sig-1", symbol="MNQ", side="BUY", qty=1)
    assert hooks.pre_trade_check(bot, rec) is False


def test_pre_trade_check_fails_open_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """If trading_gate raises, hook returns True (don't block trading)."""

    def _raising(*args, **kwargs):
        raise RuntimeError("simulated gate failure")

    monkeypatch.setattr(trading_gate, "check_pre_trade_gate", _raising)
    bot = _StubBot(bot_id="test_bot", strategy_id="test", symbol="MNQ")
    rec = _StubRec(signal_id="sig-1", symbol="MNQ", side="BUY", qty=1)
    assert hooks.pre_trade_check(bot, rec) is True


# ────────────────────────────────────────────────────────────────────
# record_signal
# ────────────────────────────────────────────────────────────────────


def test_record_signal_writes_to_signal_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obs, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    bot = _StubBot(bot_id="mnq_book_imbalance_shadow", strategy_id="book_imbalance_v1", symbol="MNQ")
    rec = _StubRec(
        signal_id="MNQ-LONG-test",
        symbol="MNQ",
        side="BUY",
        qty=1,
        entry_price=29270.25,
        stop_price=29268.25,
        target_price=29274.25,
        confidence=0.65,
        rationale="test signal from supervisor stub",
    )
    hooks.record_signal(bot, rec)
    written = (tmp_path / "sig.jsonl").read_text().splitlines()
    assert len(written) == 1
    parsed = json.loads(written[0])
    assert parsed["signal_id"] == "MNQ-LONG-test"
    assert parsed["bot_id"] == "mnq_book_imbalance_shadow"
    assert parsed["strategy_id"] == "book_imbalance_v1"
    assert parsed["entry_price"] == 29270.25
    assert parsed["intended_stop_price"] == 29268.25
    assert parsed["intended_target_price"] == 29274.25


def test_record_signal_silent_when_no_signal_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No signal_id → skip without crashing."""
    monkeypatch.setattr(obs, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    bot = _StubBot(bot_id="t", strategy_id="t", symbol="MNQ")
    rec = _StubRec(signal_id="", symbol="MNQ", side="BUY", qty=1)
    hooks.record_signal(bot, rec)
    # No file written
    assert not (tmp_path / "sig.jsonl").exists() or (tmp_path / "sig.jsonl").read_text() == ""


def test_record_signal_defensive_on_missing_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rec missing stop_price/target_price → uses 0.0 default."""
    monkeypatch.setattr(obs, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    bot = _StubBot(bot_id="t", strategy_id="t", symbol="MNQ")
    rec = _StubRec(signal_id="s1", symbol="MNQ", side="BUY", qty=1)
    hooks.record_signal(bot, rec)
    parsed = json.loads((tmp_path / "sig.jsonl").read_text().strip())
    assert parsed["intended_stop_price"] == 0.0


# ────────────────────────────────────────────────────────────────────
# record_fill
# ────────────────────────────────────────────────────────────────────


def test_record_fill_writes_to_fill_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obs, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    hooks.record_fill(
        signal_id="MNQ-LONG-test",
        broker_exec_id="abc123",
        exit_reason="TARGET",
        side="LONG",
        actual_fill_price=29274.25,
        qty_filled=1,
        commission_usd=0.62,
    )
    parsed = json.loads((tmp_path / "fill.jsonl").read_text().strip())
    assert parsed["signal_id"] == "MNQ-LONG-test"
    assert parsed["exit_reason"] == "TARGET"
    assert parsed["actual_fill_price"] == 29274.25


def test_record_fill_computes_slip_ticks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When intended_price is passed, slip_ticks_vs_intended is filled."""
    monkeypatch.setattr(obs, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    # LONG stop at 100.0, actual fill 99.50 → 2 ticks adverse slip
    hooks.record_fill(
        signal_id="s1",
        broker_exec_id="x",
        exit_reason="STOP",
        side="LONG",
        actual_fill_price=99.50,
        qty_filled=1,
        intended_price=100.0,
        tick_size=0.25,
    )
    parsed = json.loads((tmp_path / "fill.jsonl").read_text().strip())
    assert parsed["slip_ticks_vs_intended"] == 2.0


def test_record_fill_no_slip_when_intended_not_provided(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obs, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    hooks.record_fill(
        signal_id="s1",
        broker_exec_id="x",
        exit_reason="TARGET",
        side="LONG",
        actual_fill_price=100.0,
        qty_filled=1,
    )
    parsed = json.loads((tmp_path / "fill.jsonl").read_text().strip())
    assert parsed["slip_ticks_vs_intended"] is None


# ────────────────────────────────────────────────────────────────────
# record_bulk_fills
# ────────────────────────────────────────────────────────────────────


def test_record_bulk_fills_writes_all_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obs, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    fills = [
        {
            "signal_id": "s1",
            "exit_reason": "TARGET",
            "side": "LONG",
            "actual_fill_price": 100.0,
            "qty_filled": 1,
            "broker_exec_id": "x1",
        },
        {
            "signal_id": "s2",
            "exit_reason": "STOP",
            "side": "SHORT",
            "actual_fill_price": 101.0,
            "qty_filled": 1,
            "broker_exec_id": "x2",
        },
    ]
    n = hooks.record_bulk_fills(fills)
    assert n == 2
    lines = (tmp_path / "fill.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_record_bulk_fills_skips_bad_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(obs, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    fills = [
        {
            "signal_id": "s1",
            "exit_reason": "TARGET",
            "side": "LONG",
            "actual_fill_price": 100.0,
            "broker_exec_id": "x1",
        },
        {"signal_id": "missing_required_field"},  # bad
    ]
    n = hooks.record_bulk_fills(fills)
    assert n == 1  # only 1 valid record written
