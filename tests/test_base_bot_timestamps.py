from __future__ import annotations

from datetime import UTC

from eta_engine.bots.base_bot import Fill, Position, Signal, SignalType


def test_shared_model_timestamps_default_to_timezone_aware_utc() -> None:
    signal = Signal(type=SignalType.LONG, symbol="MNQ", price=1.0)
    position = Position(symbol="MNQ", side="LONG", entry_price=1.0, size=1.0)
    fill = Fill(symbol="MNQ", side="BUY", price=1.0, size=1.0)

    assert signal.ts.tzinfo is UTC
    assert position.opened_at.tzinfo is UTC
    assert fill.ts.tzinfo is UTC
