from __future__ import annotations

import numpy as np

from eta_engine.feeds.strategy_lab import engine


def _bars(n: int = 40) -> dict[str, np.ndarray]:
    close = np.linspace(100.0, 120.0, n)
    return {
        "time": np.arange(n, dtype=float),
        "open": close - 0.25,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": np.linspace(1_000.0, 2_000.0, n),
    }


def test_scorecard_sub_strategy_dispatch_filters_registered_sub_signals(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def sub_generator(bars: dict[str, np.ndarray], spec: dict[str, object]):
        calls.append(spec)
        return [
            (25, "long", 1.25, 2.5),
            (26, "short", 1.50, 3.0),
        ]

    def fake_scorecard_score_at(
        bars: dict[str, np.ndarray],
        spec: dict[str, object],
        idx: int,
        side: str,
    ) -> int:
        return 3 if idx == 25 else 1

    monkeypatch.setitem(engine.SIGNAL_GENERATORS, "unit_sub", sub_generator)
    monkeypatch.setattr(engine, "scorecard_score_at", fake_scorecard_score_at)

    signals = engine.signals_confluence_scorecard(
        _bars(),
        {
            "sub_strategy_kind": "unit_sub",
            "sub_strategy_extras": {"stop_atr": 9.0},
            "min_score": 2,
        },
    )

    assert signals == [(25, "long", 1.25, 2.5)]
    assert calls[0]["stop_atr"] == 9.0


def test_scorecard_countertrend_alias_dispatch_bypasses_trend_filter(monkeypatch) -> None:
    def vwap_generator(bars: dict[str, np.ndarray], spec: dict[str, object]):
        return [(30, "long", 1.0, 2.0)]

    def rejecting_scorecard(
        bars: dict[str, np.ndarray],
        spec: dict[str, object],
        idx: int,
        side: str,
    ) -> int:
        return 0

    monkeypatch.setitem(engine.SIGNAL_GENERATORS, "vwap_mr", vwap_generator)
    monkeypatch.setattr(engine, "scorecard_score_at", rejecting_scorecard)

    signals = engine.signals_confluence_scorecard(
        _bars(),
        {
            "sub_strategy_kind": "vwap_mean_reversion",
            "min_score": 5,
        },
    )

    assert signals == [(30, "long", 1.0, 2.0)]
