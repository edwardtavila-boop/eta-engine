"""Tests for the diamond promotion gate."""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def test_console_help_description_is_ascii_safe() -> None:
    from eta_engine.scripts import diamond_promotion_gate as gate

    sanitized = gate._console_help_description("Promotion gate \u2014 production \u2265 paper")

    assert sanitized.isascii()
    assert sanitized == "Promotion gate ? production ? paper"


def _ts(days_ago: int, hour: int = 14) -> str:
    """Return an ISO timestamp `days_ago` calendar days back at the given UTC
    hour. Used to scaffold per-day trade distributions in the gate tests."""
    return (
        (datetime.now(UTC) - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()
    )


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            # Wave-25: tag every fixture row as live so the production
            # filter (defaults to live+paper) accepts them. Tests that
            # don't set data_source explicitly intend to exercise the
            # gate's logic on real production-grade records.
            tagged = dict(r)
            tagged.setdefault("data_source", "live")
            tagged.setdefault("realized_pnl", float(tagged.get("realized_r") or 0.0) * 100.0)
            fh.write(json.dumps(tagged) + "\n")


def _run_with_data(
    canonical: list[dict], legacy: list[dict] | None, tmp_path: Path, monkeypatch: object, include_existing: bool = True
) -> dict:
    """Helper: write canonical+legacy jsonl files into tmp_path, point the
    gate at them, then invoke run().  Returns the summary dict."""
    from eta_engine.scripts import diamond_promotion_gate as gate

    can_path = tmp_path / "canonical.jsonl"
    leg_path = tmp_path / "legacy.jsonl"
    _write_jsonl(can_path, canonical)
    _write_jsonl(leg_path, legacy or [])

    monkeypatch.setattr(gate, "TRADE_CLOSES_CANONICAL", can_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(gate, "TRADE_CLOSES_LEGACY", leg_path)  # type: ignore[attr-defined]
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(gate, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    return gate.run(include_existing=include_existing)


# ────────────────────────────────────────────────────────────────────
# Gate verdict semantics
# ────────────────────────────────────────────────────────────────────


def test_passes_all_gates_returns_PROMOTE(tmp_path: Path, monkeypatch: object) -> None:
    """A bot with very strong stats across all gates must verdict PROMOTE.

    The fixture decouples R-sign from session/day so each session genuinely
    samples both winners and losers (otherwise an i%2 R-toggle correlated
    with an i%4 session-rotation would make exactly 2 sessions 100% winning
    and 2 sessions 100% losing — looks 50% overall, but fails the
    sessions-positive count).
    """
    import random

    rng = random.Random(42)  # deterministic
    rows = []
    sessions = ("overnight", "morning", "afternoon", "close")
    for i in range(600):
        # 70% wins of +1.0, 30% losses of -0.5 → avg = 0.7*1.0 + 0.3*(-0.5)
        # = +0.55R, win rate = 70%
        is_win = rng.random() < 0.70
        r = 1.0 if is_win else -0.5
        rows.append(
            {
                "bot_id": "strong_bot",
                "signal_id": f"s{i}",
                "realized_r": r,
                # Spread across 16 days, decoupled from session via independent index
                "ts": _ts(days_ago=rng.randint(0, 15)),
                "session": sessions[rng.randint(0, 3)],
            }
        )
    summary = _run_with_data(rows, [], tmp_path, monkeypatch)
    cards = {c["bot_id"]: c for c in summary["candidates"]}
    assert "strong_bot" in cards
    assert cards["strong_bot"]["verdict"] == "PROMOTE", cards["strong_bot"]


def test_passes_hard_but_fails_temporal_breadth_returns_NEEDS_MORE_DATA(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """H4 passes (5+ days) but S3 fails (<14 days): NEEDS_MORE_DATA."""
    rows = []
    sessions = ("overnight", "morning", "afternoon", "close")
    for i in range(200):
        r = 1.0 if i % 2 == 0 else -0.4
        rows.append(
            {
                "bot_id": "fast_starter",
                "signal_id": f"f{i}",
                "realized_r": r,
                "ts": _ts(days_ago=i % 6),  # 6 days only
                "session": sessions[i % 4],
            }
        )
    summary = _run_with_data(rows, [], tmp_path, monkeypatch)
    cards = {c["bot_id"]: c for c in summary["candidates"]}
    assert cards["fast_starter"]["verdict"] == "NEEDS_MORE_DATA"
    assert "S3_calendar_days_two_weeks" in cards["fast_starter"]["rationale"]


def test_fails_hard_gate_returns_REJECT(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """H4 fails (<5 days) regardless of other strengths: REJECT."""
    rows = []
    for i in range(500):
        r = 1.0 if i % 2 == 0 else -0.4
        rows.append(
            {
                "bot_id": "narrow_window",
                "signal_id": f"n{i}",
                "realized_r": r,
                "ts": _ts(days_ago=i % 2),  # 2 days only
                "session": "overnight" if i % 2 == 0 else "morning",
            }
        )
    summary = _run_with_data(rows, [], tmp_path, monkeypatch)
    cards = {c["bot_id"]: c for c in summary["candidates"]}
    assert cards["narrow_window"]["verdict"] == "REJECT"
    assert "H4_calendar_days" in cards["narrow_window"]["rationale"]


def test_low_avg_r_fails_H2(tmp_path: Path, monkeypatch: object) -> None:
    """avg_r below +0.20R fails H2 even with huge sample + many days."""
    rows = []
    sessions = ("overnight", "morning", "afternoon", "close")
    for i in range(1000):
        # avg +0.05R / 50% wr — large sample, small per-trade edge
        r = 0.5 if i % 2 == 0 else -0.4
        rows.append(
            {
                "bot_id": "noise_bot",
                "signal_id": f"x{i}",
                "realized_r": r,
                "ts": _ts(days_ago=i % 20),
                "session": sessions[i % 4],
            }
        )
    summary = _run_with_data(rows, [], tmp_path, monkeypatch)
    cards = {c["bot_id"]: c for c in summary["candidates"]}
    assert cards["noise_bot"]["verdict"] == "REJECT"
    assert "H2_avg_r" in cards["noise_bot"]["rationale"]


def test_positive_r_but_negative_broker_pnl_fails_hard_gate(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Broker-dollar truth wins over a pretty R curve."""
    rows = []
    sessions = ("overnight", "morning", "afternoon", "close")
    for i in range(300):
        rows.append(
            {
                "bot_id": "phantom_edge",
                "signal_id": f"p{i}",
                "realized_r": 0.5,
                "realized_pnl": -10.0,
                "ts": _ts(days_ago=i % 16),
                "session": sessions[i % 4],
            }
        )
    summary = _run_with_data(rows, [], tmp_path, monkeypatch)
    cards = {c["bot_id"]: c for c in summary["candidates"]}
    assert cards["phantom_edge"]["verdict"] == "REJECT"
    assert cards["phantom_edge"]["total_realized_pnl"] < 0
    assert "H6_total_realized_pnl" in cards["phantom_edge"]["rationale"]


def test_under_min_consideration_sample_is_dropped(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Bots with fewer than MIN_SAMPLE_FOR_CONSIDERATION trades (and not
    already diamonds) don't appear in the report at all."""
    rows = [
        {"bot_id": "tiny_bot", "signal_id": f"t{i}", "realized_r": 0.5, "ts": _ts(i % 5), "session": "overnight"}
        for i in range(10)  # n=10 < 50 threshold
    ]
    summary = _run_with_data(rows, [], tmp_path, monkeypatch, include_existing=False)
    bot_ids = {c["bot_id"] for c in summary["candidates"]}
    assert "tiny_bot" not in bot_ids


# ────────────────────────────────────────────────────────────────────
# Dual-source dedup
# ────────────────────────────────────────────────────────────────────


def test_dual_source_dedupes_on_signal_id_match(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """When the same row appears in canonical AND legacy, it's counted once."""
    shared = [
        {"bot_id": "dup_bot", "signal_id": f"s{i}", "realized_r": 0.5, "ts": _ts(i % 5), "session": "overnight"}
        for i in range(100)
    ]
    summary = _run_with_data(shared, shared, tmp_path, monkeypatch)
    cards = {c["bot_id"]: c for c in summary["candidates"]}
    assert cards["dup_bot"]["n_trades"] == 100  # not 200


# ────────────────────────────────────────────────────────────────────
# Internal bot filter
# ────────────────────────────────────────────────────────────────────


def test_internal_bot_ids_filtered(tmp_path: Path, monkeypatch: object) -> None:
    """t1 / propagate_bot are layer-propagation artifacts, never promoted."""
    rows = [
        {"bot_id": "t1", "signal_id": f"x{i}", "realized_r": 2.5, "ts": _ts(i % 8), "session": "overnight"}
        for i in range(300)
    ] + [
        {"bot_id": "propagate_bot", "signal_id": f"p{i}", "realized_r": 40.0, "ts": _ts(i % 8), "session": "morning"}
        for i in range(200)
    ]
    summary = _run_with_data(rows, [], tmp_path, monkeypatch)
    bot_ids = {c["bot_id"] for c in summary["candidates"]}
    assert "t1" not in bot_ids
    assert "propagate_bot" not in bot_ids


# ────────────────────────────────────────────────────────────────────
# Snapshot file write
# ────────────────────────────────────────────────────────────────────


def test_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    """run() must persist the summary to OUT_LATEST."""
    from eta_engine.scripts import diamond_promotion_gate as gate

    can_path = tmp_path / "canonical.jsonl"
    leg_path = tmp_path / "legacy.jsonl"
    _write_jsonl(
        can_path,
        [
            {"bot_id": "any_bot", "signal_id": "s0", "realized_r": 0.3, "ts": _ts(0), "session": "overnight"},
        ],
    )
    _write_jsonl(leg_path, [])
    monkeypatch.setattr(gate, "TRADE_CLOSES_CANONICAL", can_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(gate, "TRADE_CLOSES_LEGACY", leg_path)  # type: ignore[attr-defined]
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(gate, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    gate.run(include_existing=True)

    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in data
    assert "candidates" in data
    assert "n_promote" in data
    assert "n_needs_more" in data
    assert "n_reject" in data


def test_cli_help_description_is_ascii_safe() -> None:
    from eta_engine.scripts import diamond_promotion_gate as gate

    text = gate._console_help_description(f"hard fail {chr(8594)} reject")

    assert text.isascii()
    assert "?" in text
