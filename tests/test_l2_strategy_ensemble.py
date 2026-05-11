from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from eta_engine.strategies import l2_strategy_ensemble as ensemble


@dataclass
class _Signal:
    strategy_id: str
    side: str
    confidence: float
    signal_id: str


def test_compute_weights_from_history_mutes_negative_sharpe(tmp_path) -> None:
    path = tmp_path / "l2_backtest_runs.jsonl"
    now = datetime.now(UTC).isoformat()
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "ts": now,
                    "strategy": "book_imbalance",
                    "sharpe_proxy_valid": True,
                    "sharpe_proxy": 1.2,
                },
                {
                    "ts": now,
                    "strategy": "microprice_drift",
                    "sharpe_proxy_valid": True,
                    "sharpe_proxy": -0.4,
                },
            ]
        ),
        encoding="utf-8",
    )

    weights = ensemble.compute_weights_from_history(_path=path)

    assert weights.weights["book_imbalance"] == 1.2
    assert weights.weights["microprice_drift"] == 0.0


def test_vote_emits_weighted_long_signal() -> None:
    signal = ensemble.vote(
        [
            _Signal("book_imbalance", "LONG", 0.8, "sig-a"),
            _Signal("microprice_drift", "SHORT", 0.4, "sig-b"),
        ],
        {"book_imbalance": 1.0, "microprice_drift": 0.25},
        ensemble_threshold=0.5,
    )

    assert signal is not None
    assert signal.side == "LONG"
    assert signal.signal_id.startswith("ENSEMBLE-LONG")
    assert len(signal.constituent_signals) == 2


def test_vote_returns_none_without_positive_weights() -> None:
    signal = ensemble.vote(
        [_Signal("book_imbalance", "LONG", 0.8, "sig-a")],
        {"book_imbalance": 0.0},
    )

    assert signal is None
