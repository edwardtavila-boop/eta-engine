"""Tests for the wave-25b shadow signal logger.

Captures every signal the supervisor's wave-25 conditional routing sends
to ``paper`` so kaizen + dashboards have visibility into observed-but-
not-taken trades.
"""
# ruff: noqa: PLR2004
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def test_log_appends_record(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import log_shadow_signal

    p = tmp_path / "shadow.jsonl"
    ok = log_shadow_signal(
        bot_id="m2k_sweep_reclaim",
        signal_id="sig1",
        symbol="M2K",
        side="BUY",
        qty_intended=1,
        lifecycle="EVAL_PAPER",
        route_target="paper",
        route_reason="lifecycle_eval_paper",
        prospective_loss_usd=250.0,
        path=p,
    )
    assert ok is True
    assert p.exists()
    line = p.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["bot_id"] == "m2k_sweep_reclaim"
    assert rec["signal_id"] == "sig1"
    assert rec["route_target"] == "paper"
    assert rec["prospective_loss_usd"] == 250.0
    assert rec["lifecycle"] == "EVAL_PAPER"


def test_multiple_appends_one_record_per_line(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import log_shadow_signal

    p = tmp_path / "shadow.jsonl"
    for i in range(5):
        log_shadow_signal(
            bot_id="m2k",
            signal_id=f"s{i}",
            symbol="M2K",
            side="BUY",
            qty_intended=1,
            lifecycle="EVAL_PAPER",
            route_target="paper",
            route_reason="ok",
            prospective_loss_usd=100.0,
            path=p,
        )
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5


def test_extra_dict_preserved(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import log_shadow_signal

    p = tmp_path / "shadow.jsonl"
    log_shadow_signal(
        bot_id="met",
        signal_id="sig1",
        symbol="MET",
        side="SELL",
        qty_intended=2,
        lifecycle="EVAL_PAPER",
        route_target="paper",
        route_reason="soft_dd",
        prospective_loss_usd=800.0,
        extra={"bar_ts": "2026-05-13T10:00:00+00:00", "size_mult_at_skip": 0.5},
        path=p,
    )
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["extra"]["bar_ts"] == "2026-05-13T10:00:00+00:00"
    assert rec["extra"]["size_mult_at_skip"] == 0.5


def test_read_filters_by_bot(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import (
        log_shadow_signal,
        read_shadow_signals,
    )

    p = tmp_path / "shadow.jsonl"
    for bot_id in ("m2k", "m2k", "met", "mes_v2"):
        log_shadow_signal(
            bot_id=bot_id,
            signal_id=f"sig_{bot_id}",
            symbol=bot_id.upper(),
            side="BUY",
            qty_intended=1,
            lifecycle="EVAL_PAPER",
            route_target="paper",
            route_reason="ok",
            prospective_loss_usd=100.0,
            path=p,
        )
    m2k_only = read_shadow_signals(bot_filter="m2k", path=p)
    assert len(m2k_only) == 2
    assert all(r["bot_id"] == "m2k" for r in m2k_only)


def test_read_filters_by_since(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import (
        log_shadow_signal,
        read_shadow_signals,
    )

    p = tmp_path / "shadow.jsonl"
    log_shadow_signal(
        bot_id="m2k",
        signal_id="old",
        symbol="M2K",
        side="BUY",
        qty_intended=1,
        lifecycle="EVAL_PAPER",
        route_target="paper",
        route_reason="ok",
        prospective_loss_usd=100.0,
        path=p,
    )
    # Read with since=now+1h, should return nothing (record is "now")
    future = datetime.now(UTC) + timedelta(hours=1)
    rows = read_shadow_signals(since=future, path=p)
    assert rows == []


def test_summarize_groups_by_bot(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import (
        log_shadow_signal,
        summarize_shadow_signals,
    )

    p = tmp_path / "shadow.jsonl"
    for bot_id, reason in [
        ("m2k", "lifecycle_eval_paper"),
        ("m2k", "lifecycle_eval_paper"),
        ("m2k", "soft_dd"),
        ("met", "lifecycle_eval_paper"),
    ]:
        log_shadow_signal(
            bot_id=bot_id,
            signal_id=f"{bot_id}_{reason}",
            symbol=bot_id.upper(),
            side="BUY",
            qty_intended=1,
            lifecycle="EVAL_PAPER",
            route_target="paper",
            route_reason=reason,
            prospective_loss_usd=200.0,
            path=p,
        )
    summary = summarize_shadow_signals(path=p)
    assert summary["n_total"] == 4
    assert summary["n_bots"] == 2
    m2k = summary["by_bot"]["m2k"]
    assert m2k["n_signals"] == 3
    assert m2k["by_route_reason"]["lifecycle_eval_paper"] == 2
    assert m2k["by_route_reason"]["soft_dd"] == 1


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import read_shadow_signals

    rows = read_shadow_signals(path=tmp_path / "nope.jsonl")
    assert rows == []


def test_read_skips_malformed_lines(tmp_path: Path) -> None:
    from eta_engine.scripts.shadow_signal_logger import read_shadow_signals

    p = tmp_path / "shadow.jsonl"
    p.write_text(
        '{"bot_id": "m2k", "ts": "2026-05-13T10:00:00+00:00"}\n'
        "not-json-at-all\n"
        '{"bot_id": "met", "ts": "2026-05-13T11:00:00+00:00"}\n',
        encoding="utf-8",
    )
    rows = read_shadow_signals(path=p)
    assert len(rows) == 2
    assert {r["bot_id"] for r in rows} == {"m2k", "met"}
