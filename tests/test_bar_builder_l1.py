"""Tests for bar_builder_l1 — Phase-2 buy/sell-split bar reconstruction."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from eta_engine.feeds import bar_builder_l1 as bb

# ── _classify_tick (tick rule) ────────────────────────────────────


def test_classify_first_tick_unknown() -> None:
    assert bb._classify_tick(100.0, None) == "UNKNOWN"


def test_classify_uptick_buy() -> None:
    assert bb._classify_tick(101.0, 100.0) == "BUY"


def test_classify_downtick_sell() -> None:
    assert bb._classify_tick(99.0, 100.0) == "SELL"


def test_classify_zero_tick_inherits() -> None:
    assert bb._classify_tick(100.0, 100.0, prev_side="BUY") == "BUY"
    assert bb._classify_tick(100.0, 100.0, prev_side="SELL") == "SELL"
    assert bb._classify_tick(100.0, 100.0, prev_side="UNKNOWN") == "UNKNOWN"


# ── _bucket_start ─────────────────────────────────────────────────


def test_bucket_start_aligns_to_minute() -> None:
    # epoch_s 100.5 with tf=60 → bucket 60 (the minute starting at 60)
    assert bb._bucket_start(100.5, 60) == 60
    # epoch_s 119.99 → still bucket 60
    assert bb._bucket_start(119.99, 60) == 60
    # epoch_s 120.0 → bucket 120
    assert bb._bucket_start(120.0, 60) == 120


def test_bucket_start_aligns_to_5m() -> None:
    # epoch_s 1000 with tf=300 → bucket 900
    assert bb._bucket_start(1000, 300) == 900


# ── BarAccum ──────────────────────────────────────────────────────


def test_bar_accum_from_first_tick_counts_volume() -> None:
    """Bug fix 2026-05-11: the first tick's volume must be counted."""
    t = bb.TickRecord(epoch_s=0, price=100.0, size=5.0, side="BUY")
    bar = bb.BarAccum.from_first_tick(t)
    assert bar.open == bar.high == bar.low == bar.close == 100.0
    assert bar.volume_total == 5.0
    assert bar.volume_buy == 5.0
    assert bar.n_trades == 1


def test_bar_accum_from_first_tick_unknown_side() -> None:
    t = bb.TickRecord(epoch_s=0, price=100.0, size=5.0, side="UNKNOWN")
    bar = bb.BarAccum.from_first_tick(t)
    assert bar.volume_total == 5.0
    assert bar.volume_buy == 0.0
    assert bar.volume_sell == 0.0
    assert bar.n_trades == 1


def test_bar_accum_absorb_buy_sell() -> None:
    bar = bb.BarAccum.from_first_tick(
        bb.TickRecord(epoch_s=0, price=100.0, size=0.0, side="UNKNOWN"))
    bar.absorb(bb.TickRecord(epoch_s=10, price=101.0, size=3.0, side="BUY"))
    bar.absorb(bb.TickRecord(epoch_s=20, price=99.0, size=2.0, side="SELL"))
    bar.absorb(bb.TickRecord(epoch_s=30, price=100.0, size=1.0, side="UNKNOWN"))
    assert bar.high == 101.0
    assert bar.low == 99.0
    assert bar.close == 100.0
    assert bar.volume_total == 6.0  # opener size=0 + 3+2+1
    assert bar.volume_buy == 3.0
    assert bar.volume_sell == 2.0
    assert bar.n_trades == 4  # opener + 3 absorbed


# ── build_bars (end-to-end with synthetic ticks) ──────────────────


def _ticks_from_sequence(prices_sizes: list[tuple[float, float]],
                          start_epoch: float = 1700000000.0,
                          step: float = 1.0) -> list[bb.TickRecord]:
    """Convert (price, size) sequence to TickRecord with tick-rule
    classification."""
    out: list[bb.TickRecord] = []
    prev_price: float | None = None
    prev_side = "UNKNOWN"
    for i, (p, s) in enumerate(prices_sizes):
        side = bb._classify_tick(p, prev_price, prev_side)
        out.append(bb.TickRecord(epoch_s=start_epoch + i * step,
                                  price=p, size=s, side=side))
        prev_price = p
        prev_side = side
    return out


def test_build_bars_single_bucket() -> None:
    # All ticks fall within one 5m bucket
    ticks = _ticks_from_sequence([
        (100.0, 1), (101.0, 2), (99.0, 3), (100.5, 1),
    ])
    bars = bb.build_bars(ticks, "5m")
    assert len(bars) == 1
    b = bars[0]
    assert b["open"] == 100.0
    assert b["high"] == 101.0
    assert b["low"] == 99.0
    assert b["close"] == 100.5
    # All 4 sizes counted (opener + 3 absorbed)
    assert b["volume_total"] == 7.0  # 1 + 2 + 3 + 1
    # Tick 1 (100.0, size=1): UNKNOWN (no prior price)
    # Tick 2 (101.0, size=2): uptick → BUY
    # Tick 3 (99.0, size=3): downtick → SELL
    # Tick 4 (100.5, size=1): uptick → BUY
    assert b["volume_buy"] == 3.0  # 2 + 1
    assert b["volume_sell"] == 3.0  # 3 alone
    assert b["n_trades"] == 4


def test_build_bars_multi_bucket() -> None:
    # 2 ticks at t=0, 1; then 2 ticks at t=300, 301 (next 5m bucket)
    ticks = [
        bb.TickRecord(epoch_s=1700000000, price=100.0, size=1.0, side="UNKNOWN"),
        bb.TickRecord(epoch_s=1700000001, price=101.0, size=2.0, side="BUY"),
        bb.TickRecord(epoch_s=1700000300, price=102.0, size=3.0, side="BUY"),
        bb.TickRecord(epoch_s=1700000301, price=100.0, size=4.0, side="SELL"),
    ]
    bars = bb.build_bars(ticks, "5m")
    assert len(bars) == 2
    # Bucket 0: tick 1 (size=1) + tick 2 (size=2) = 3
    assert bars[0]["volume_total"] == 3.0
    # Bucket 1: tick 3 (size=3) + tick 4 (size=4) = 7
    assert bars[1]["volume_total"] == 7.0


def test_build_bars_unknown_timeframe() -> None:
    with pytest.raises(ValueError, match="unknown timeframe"):
        bb.build_bars([], "13s")


# ── _read_ticks (file IO + malformed-line tolerance) ──────────────


@pytest.fixture()
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    ticks = tmp_path / "ticks"
    out = tmp_path / "out"
    ticks.mkdir()
    out.mkdir()
    monkeypatch.setattr(bb, "TICKS_DIR", ticks)
    monkeypatch.setattr(bb, "OUT_DIR", out)
    return {"ticks": ticks, "out": out}


def test_read_ticks_handles_missing_file(isolated_dirs: dict) -> None:
    out = bb._read_ticks(isolated_dirs["ticks"] / "MNQ_20260101.jsonl")
    assert out == []


def test_read_ticks_skips_malformed_lines(isolated_dirs: dict) -> None:
    p = isolated_dirs["ticks"] / "MNQ_20260101.jsonl"
    p.write_text("\n".join([
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "epoch_s": 1, "price": 100.0, "size": 1}),
        "garbage line",
        json.dumps({"price": "not-a-number"}),
        json.dumps({"ts": "2026-01-01T00:00:01+00:00", "epoch_s": 2, "price": 101.0, "size": 2}),
        "",  # blank
    ]) + "\n", encoding="utf-8")
    ticks = bb._read_ticks(p)
    assert len(ticks) == 2
    assert ticks[0].price == 100.0
    assert ticks[1].price == 101.0


# ── write_bars_csv ────────────────────────────────────────────────


def test_write_bars_csv_atomic(isolated_dirs: dict) -> None:
    bars = [{
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "epoch_s": 0, "open": 1.0, "high": 1.5, "low": 0.9, "close": 1.2,
        "volume_total": 10.0, "volume_buy": 6.0, "volume_sell": 4.0, "n_trades": 5,
    }]
    out_path = isolated_dirs["out"] / "MNQ_5m_l1.csv"
    bb.write_bars_csv(out_path, bars)
    assert out_path.exists()
    with out_path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["volume_buy"] == "6.0"


def test_write_bars_csv_empty_noop(isolated_dirs: dict) -> None:
    out_path = isolated_dirs["out"] / "EMPTY_5m_l1.csv"
    bb.write_bars_csv(out_path, [])
    assert not out_path.exists()


# ── rebuild_one_symbol (integration) ──────────────────────────────


def test_rebuild_one_symbol_full_path(isolated_dirs: dict) -> None:
    # Write a tick file with 4 ticks in the same 5m bucket
    p = isolated_dirs["ticks"] / "MNQ_20260101.jsonl"
    base = 1700000000
    p.write_text("\n".join([
        json.dumps({"ts": f"t{i}", "epoch_s": base + i, "price": 100.0 + i, "size": 1.0})
        for i in range(4)
    ]) + "\n", encoding="utf-8")
    result = bb.rebuild_one_symbol("MNQ", "5m")
    assert result["n_ticks"] == 4
    assert result["n_bars"] == 1
    out = isolated_dirs["out"] / "MNQ_5m_l1.csv"
    assert out.exists()


def test_rebuild_one_symbol_no_files(isolated_dirs: dict) -> None:
    result = bb.rebuild_one_symbol("NONEXISTENT", "5m")
    assert result["n_ticks"] == 0
    assert result["note"] == "no tick files"
