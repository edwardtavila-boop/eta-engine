from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.scripts import tick_stream_consumer as consumer


def _tick(price: float = 100.0, size: float = 2.0) -> dict:
    ts = datetime(2026, 5, 11, 17, tzinfo=UTC)
    return {
        "ts": ts.isoformat(),
        "epoch_s": ts.timestamp(),
        "symbol": "MNQ1",
        "price": price,
        "size": size,
        "exchange": "CME",
    }


def test_parse_line_normalizes_tick_record() -> None:
    tick = consumer._parse_line(json.dumps(_tick(price=101.25)))

    assert tick is not None
    assert tick.symbol == "MNQ1"
    assert tick.price == 101.25
    assert tick.exchange == "CME"


def test_iter_ticks_from_file_skips_malformed_lines(tmp_path) -> None:
    path = tmp_path / "ticks.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_tick(price=100.0)),
                "{bad json",
                json.dumps({"price": "not-a-number"}),
                json.dumps(_tick(price=100.25)),
            ]
        ),
        encoding="utf-8",
    )

    ticks = list(consumer.iter_ticks_from_file(path))

    assert [tick.price for tick in ticks] == [100.0, 100.25]


def test_feed_strategy_microprice_delivers_ticks(monkeypatch, tmp_path) -> None:
    ticks_dir = tmp_path / "ticks"
    ticks_dir.mkdir()
    tick_file = ticks_dir / "MNQ_20260511.jsonl"
    tick_file.write_text(
        "\n".join(json.dumps(_tick(price=price)) for price in (100.0, 100.25, 100.5)),
        encoding="utf-8",
    )
    monkeypatch.setattr(consumer, "TICKS_DIR", ticks_dir)

    delivered: list[tuple[float, datetime]] = []

    class _Strategy:
        def update_trade(self, price: float, ts: datetime | None = None) -> None:
            assert ts is not None
            delivered.append((price, ts))

    count = consumer.feed_strategy_microprice("MNQ", "20260511", _Strategy())

    assert count == 3
    assert [price for price, _ts in delivered] == [100.0, 100.25, 100.5]
