"""Tests for attribution_cube — T12 multi-dim performance attribution."""

from __future__ import annotations

import json
from pathlib import Path


def _write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")


def test_query_empty_when_no_trade_closes(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import attribution_cube

    result = attribution_cube.query(
        slice_by=["bot"],
        trade_closes_path=tmp_path / "missing.jsonl",
    )
    assert result.rows == []
    assert result.error is None  # missing file is normal, not an error


def test_query_slices_by_bot(tmp_path: Path) -> None:
    """Group by bot_id → one row per bot with summed R."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
            {"bot_id": "a", "asset_class": "MNQ", "r": -0.5, "ts": "2026-05-12T15:00:00Z"},
            {"bot_id": "b", "asset_class": "BTC", "r": 2.0, "ts": "2026-05-12T16:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["bot"],
        trade_closes_path=trades_path,
    )
    by_key = {tuple(r.key.items()): r for r in result.rows}
    assert (("bot", "a"),) in by_key
    assert (("bot", "b"),) in by_key
    a = by_key[(("bot", "a"),)]
    b = by_key[(("bot", "b"),)]
    assert a.total_r == 0.5
    assert a.n_trades == 2
    assert a.win_rate == 0.5
    assert b.total_r == 2.0
    assert b.n_trades == 1


def test_query_slices_by_asset(tmp_path: Path) -> None:
    """Group by asset_class — different bots in same asset roll up together."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
            {"bot_id": "b", "asset_class": "MNQ", "r": 0.5, "ts": "2026-05-12T15:00:00Z"},
            {"bot_id": "c", "asset_class": "BTC", "r": -2.0, "ts": "2026-05-12T16:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["asset"],
        trade_closes_path=trades_path,
    )
    by_key = {r.key["asset"]: r for r in result.rows}
    assert by_key["MNQ"].total_r == 1.5
    assert by_key["BTC"].total_r == -2.0


def test_query_filters_by_asset(tmp_path: Path) -> None:
    """filter={'asset':'MNQ'} drops non-MNQ trades."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
            {"bot_id": "b", "asset_class": "BTC", "r": 100.0, "ts": "2026-05-12T15:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["bot"],
        filter={"asset": "MNQ"},
        trade_closes_path=trades_path,
    )
    bots = {r.key["bot"] for r in result.rows}
    assert bots == {"a"}


def test_query_filters_by_hour_window(tmp_path: Path) -> None:
    """hour_min/hour_max windows out trades outside the time range (UTC)."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T09:00:00Z"},
            {"bot_id": "a", "asset_class": "MNQ", "r": 2.0, "ts": "2026-05-12T14:00:00Z"},
            {"bot_id": "a", "asset_class": "MNQ", "r": 3.0, "ts": "2026-05-12T20:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["bot"],
        filter={"hour_min": 13, "hour_max": 18},
        trade_closes_path=trades_path,
    )
    # Only the 14:00 trade survives
    assert len(result.rows) == 1
    assert result.rows[0].total_r == 2.0
    assert result.rows[0].n_trades == 1


def test_query_school_expansion(tmp_path: Path) -> None:
    """Slicing by school expands each trade to N school rows."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {
                "bot_id": "a",
                "asset_class": "MNQ",
                "r": 1.0,
                "ts": "2026-05-12T14:00:00Z",
                "schools": {"momentum": {"score": 0.5}, "mean_revert": {"score": -0.2}},
            },
        ],
    )

    result = attribution_cube.query(
        slice_by=["school"],
        trade_closes_path=trades_path,
    )
    schools = {r.key["school"] for r in result.rows}
    assert schools == {"momentum", "mean_revert"}
    # Each school sees the full r=1.0 contribution (attribution overcounts —
    # that's by design for "who participated in winners")
    for r in result.rows:
        assert r.n_trades == 1
        assert r.total_r == 1.0


def test_query_multi_dim_slice(tmp_path: Path) -> None:
    """slice_by=['bot','asset'] produces composite-key rows."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
            {"bot_id": "a", "asset_class": "BTC", "r": 2.0, "ts": "2026-05-12T15:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["bot", "asset"],
        trade_closes_path=trades_path,
    )
    composite_keys = {(r.key["bot"], r.key["asset"]) for r in result.rows}
    assert composite_keys == {("a", "MNQ"), ("a", "BTC")}


def test_query_rejects_unknown_slice_dims(tmp_path: Path) -> None:
    """Unknown dim is silently dropped from slice_by."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "r": 1.0, "ts": "2026-05-12T14:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["bot", "not_a_real_dim"],
        trade_closes_path=trades_path,
    )
    assert result.slice_by == ["bot"]


def test_query_rows_sorted_descending_by_total_r(tmp_path: Path) -> None:
    """Operator sees winners first."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    trades_path = tmp_path / "trade_closes.jsonl"
    _write_trades(
        trades_path,
        [
            {"bot_id": "loser", "asset_class": "MNQ", "r": -5.0, "ts": "2026-05-12T14:00:00Z"},
            {"bot_id": "winner", "asset_class": "MNQ", "r": 10.0, "ts": "2026-05-12T15:00:00Z"},
            {"bot_id": "meh", "asset_class": "MNQ", "r": 0.0, "ts": "2026-05-12T16:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["bot"],
        trade_closes_path=trades_path,
    )
    bots_in_order = [r.key["bot"] for r in result.rows]
    assert bots_in_order == ["winner", "meh", "loser"]


def test_query_to_dict_serializable() -> None:
    """to_dict() returns pure-dict structure (for MCP envelope)."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    result = attribution_cube.query(slice_by=["bot"], trade_closes_path=Path("nope"))
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "rows" in d
    assert "slice_by" in d
    assert "asof" in d
    # JSON round-trip
    json.dumps(d)


# ────────────────────────────────────────────────────────────────────
# Wave-11: direction as a slice dimension
# ────────────────────────────────────────────────────────────────────


def test_query_slices_by_direction_from_extra_side(tmp_path: Path) -> None:
    """direction dim derives from extra.side (BUY -> long, SELL -> short)
    — NOT from the historically-broken `direction` field that was always
    "long" on pre-wave-10 records."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    path = tmp_path / "tc.jsonl"
    trades = [
        # 3 BUY trades = long
        {
            "bot_id": "x",
            "ts": "2026-05-12T14:00:00+00:00",
            "realized_r": 1.0,
            "direction": "long",
            "extra": {"side": "BUY"},
        },
        {
            "bot_id": "x",
            "ts": "2026-05-12T15:00:00+00:00",
            "realized_r": 0.5,
            "direction": "long",
            "extra": {"side": "BUY"},
        },
        {
            "bot_id": "x",
            "ts": "2026-05-12T16:00:00+00:00",
            "realized_r": -0.3,
            "direction": "long",
            "extra": {"side": "BUY"},
        },
        # 2 SELL trades = short (note: direction field STILL says "long" —
        # the wave-10 writer bug — but the cube must override)
        {
            "bot_id": "x",
            "ts": "2026-05-12T17:00:00+00:00",
            "realized_r": 0.7,
            "direction": "long",
            "extra": {"side": "SELL"},
        },
        {
            "bot_id": "x",
            "ts": "2026-05-12T18:00:00+00:00",
            "realized_r": -0.2,
            "direction": "long",
            "extra": {"side": "SELL"},
        },
    ]
    _write_trades(path, trades)

    result = attribution_cube.query(
        slice_by=["direction"],
        trade_closes_path=path,
    )
    rows_by_dir = {r.key["direction"]: r for r in result.rows}
    assert "long" in rows_by_dir, (
        "BUY records did not map to 'long' — wave-11 direction derivation broken in attribution_cube._key_for"
    )
    assert "short" in rows_by_dir, (
        "SELL records did not map to 'short' — pre-wave-11 attribution "
        "would have lumped them under 'long' from the broken direction field"
    )
    assert rows_by_dir["long"].n_trades == 3
    assert rows_by_dir["short"].n_trades == 2
    # +1.0 + 0.5 - 0.3 = +1.2
    assert abs(rows_by_dir["long"].total_r - 1.2) < 0.001
    # +0.7 - 0.2 = +0.5
    assert abs(rows_by_dir["short"].total_r - 0.5) < 0.001


def test_query_direction_falls_back_to_direction_field_when_no_side(
    tmp_path: Path,
) -> None:
    """Post-wave-10 records may have no extra.side but a correct
    direction field. The cube must honour it."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    path = tmp_path / "tc.jsonl"
    trades = [
        {"bot_id": "x", "ts": "2026-05-12T14:00:00+00:00", "realized_r": 1.0, "direction": "short", "extra": {}},
        {"bot_id": "x", "ts": "2026-05-12T15:00:00+00:00", "realized_r": 0.5, "direction": "short", "extra": {}},
    ]
    _write_trades(path, trades)

    result = attribution_cube.query(
        slice_by=["direction"],
        trade_closes_path=path,
    )
    rows_by_dir = {r.key["direction"]: r for r in result.rows}
    assert "short" in rows_by_dir
    assert rows_by_dir["short"].n_trades == 2


def test_query_direction_in_valid_slice_dims() -> None:
    """direction is in VALID_SLICE_DIMS (smoke test guarding against
    accidental removal during refactors)."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    assert "direction" in attribution_cube.VALID_SLICE_DIMS


def test_query_multi_dim_with_direction(tmp_path: Path) -> None:
    """direction composes with other dims: e.g. bot × direction
    surfaces per-bot per-side R-attribution."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    path = tmp_path / "tc.jsonl"
    trades = [
        # m2k longs
        {"bot_id": "m2k", "ts": "2026-05-12T14:00:00+00:00", "realized_r": 0.5, "extra": {"side": "BUY"}},
        {"bot_id": "m2k", "ts": "2026-05-12T15:00:00+00:00", "realized_r": 0.5, "extra": {"side": "BUY"}},
        # m2k shorts
        {"bot_id": "m2k", "ts": "2026-05-12T16:00:00+00:00", "realized_r": 0.4, "extra": {"side": "SELL"}},
        # eur longs
        {"bot_id": "eur", "ts": "2026-05-12T17:00:00+00:00", "realized_r": 0.3, "extra": {"side": "BUY"}},
    ]
    _write_trades(path, trades)

    result = attribution_cube.query(
        slice_by=["bot", "direction"],
        trade_closes_path=path,
    )
    rows_by_key = {(r.key["bot"], r.key["direction"]): r for r in result.rows}
    assert ("m2k", "long") in rows_by_key
    assert ("m2k", "short") in rows_by_key
    assert ("eur", "long") in rows_by_key
    assert rows_by_key[("m2k", "long")].n_trades == 2
    assert rows_by_key[("m2k", "short")].n_trades == 1
    assert rows_by_key[("eur", "long")].n_trades == 1


# ────────────────────────────────────────────────────────────────────
# 2026-05-13: tick-leak sanitizer integration
#
# Before this hookup, the attribution cube would happily sum +32,661R
# from a single MNQ trade where the writer recorded ticks-traveled
# instead of R-multiple. The trade_close_sanitizer guards against
# that by capping anything beyond R_SANITY_CEILING (currently 20R)
# and recovering when extra.realized_pnl + symbol-root tick value
# allow reconstruction.
# ────────────────────────────────────────────────────────────────────


def test_attribution_drops_tick_leak_r(tmp_path: Path) -> None:
    """A trade with realized_r=69 (ticks, not R) and NO recovery
    fields must be dropped by the sanitizer — total_r stays clean."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            # Clean trade: 1R
            {"bot_id": "x", "asset_class": "MNQ", "realized_r": 1.0, "ts": "2026-05-12T14:00:00Z"},
            # Tick-leak: 69R with no extra.realized_pnl — sanitizer should drop
            {"bot_id": "x", "asset_class": "MNQ", "realized_r": 69.0, "ts": "2026-05-12T15:00:00Z"},
        ],
    )

    result = attribution_cube.query(
        slice_by=["bot"],
        trade_closes_path=path,
    )
    by_key = {r.key["bot"]: r for r in result.rows}
    assert "x" in by_key
    # Only the clean 1.0R survived — the 69 was dropped entirely
    # (suspect rows do not contribute to total_r AND do not bump n_trades,
    # because counting them would let a single bug-event poison the
    # per-bot trade count too).
    assert by_key["x"].total_r == 1.0
    assert by_key["x"].n_trades == 1


def test_attribution_recovers_tick_leak_when_pnl_available(tmp_path: Path) -> None:
    """A trade with bogus realized_r=32661 BUT a clean
    extra.realized_pnl + extra.symbol on a known futures root must have
    its R recovered (pnl_usd / dollar_per_R from the sanitizer table)
    rather than dropped.

    For MNQ the sanitizer uses dollar_per_R = $20, so $10 PnL recovers
    to 0.5R.
    """
    from eta_engine.brain.jarvis_v3 import attribution_cube

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {
                "bot_id": "x",
                "asset_class": "MNQ",
                "realized_r": 32661.0,
                # Sanitizer reads extra.symbol + extra.realized_pnl
                "extra": {"realized_pnl": 10.0, "symbol": "MNQ1"},
                "ts": "2026-05-12T14:00:00Z",
            },
        ],
    )
    result = attribution_cube.query(
        slice_by=["bot"],
        trade_closes_path=path,
    )
    by_key = {r.key["bot"]: r for r in result.rows}
    assert "x" in by_key, "recovered record was dropped — sanitizer integration broken"
    # Recovered r = 10/20 = 0.5, NOT 32661
    assert abs(by_key["x"].total_r - 0.5) < 1e-6
    assert by_key["x"].n_trades == 1


def test_attribution_back_compat_legacy_r_field(tmp_path: Path) -> None:
    """Legacy trade closes that used `r` (not `realized_r`) still
    work — the sanitizer fall-through reads them."""
    from eta_engine.brain.jarvis_v3 import attribution_cube

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "asset_class": "MNQ", "r": 1.5, "ts": "2026-05-12T14:00:00Z"},
            {"bot_id": "a", "asset_class": "MNQ", "r": -0.5, "ts": "2026-05-12T15:00:00Z"},
        ],
    )
    result = attribution_cube.query(slice_by=["bot"], trade_closes_path=path)
    by_key = {r.key["bot"]: r for r in result.rows}
    assert by_key["a"].total_r == 1.0
    assert by_key["a"].n_trades == 2
