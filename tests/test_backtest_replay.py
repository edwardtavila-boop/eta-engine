"""Tests for ``eta_engine.backtest.replay``.

Auto-scaffolded by scripts/_test_scaffold.py -- the import smoke and
the per-symbol smoke tests are boilerplate. Edit freely; the
operator-specific edge cases belong here.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def test_import_smoke() -> None:
    """Module imports without raising."""
    importlib.import_module("eta_engine.backtest.replay")


def test_bar_replay_smoke() -> None:
    """``BarReplay`` instantiates with no args (or skips if it requires args)."""
    from eta_engine.backtest.replay import BarReplay

    try:
        obj = BarReplay()  # type: ignore[call-arg]
    except TypeError as e:
        pytest.skip(f"BarReplay requires args: {e}")
    else:
        assert obj is not None
        # TODO: real assertions about default state


def test_bar_replay_from_parquet_streams_symbol_filtered_cache(tmp_path: Path) -> None:
    """Cached parquet replay should use the real loader, not a stale scaffold."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from eta_engine.backtest.replay import BarReplay

    start_ns = int(datetime(2026, 4, 29, tzinfo=UTC).timestamp() * 1_000_000_000)
    path = tmp_path / "mixed_symbols.parquet"
    table = pa.table(
        {
            "timestamp": [
                start_ns,
                start_ns + 60_000_000_000,
                start_ns + 120_000_000_000,
            ],
            "symbol": ["MNQ", "ES", "MNQ"],
            "open": [19000.0, 5200.0, 19010.0],
            "high": [19012.0, 5204.0, 19025.0],
            "low": [18990.0, 5195.0, 19002.0],
            "close": [19008.0, 5201.0, 19018.0],
            "volume": [120.0, 80.0, 135.0],
        }
    )
    pq.write_table(table, path)

    bars = list(BarReplay.from_parquet(path, symbol="MNQ"))

    assert [bar.symbol for bar in bars] == ["MNQ", "MNQ"]
    assert [bar.close for bar in bars] == [19008.0, 19018.0]
    assert bars[0].timestamp == datetime(2026, 4, 29, tzinfo=UTC)
