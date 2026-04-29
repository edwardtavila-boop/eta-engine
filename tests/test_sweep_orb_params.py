from __future__ import annotations

from eta_engine.data import library as data_library
from eta_engine.scripts import sweep_orb_params


def test_sweep_orb_run_one_returns_zero_result_when_positive_price_filter_empties_dataset(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeDataset:
        symbol = "MNQ1"

    class FakeLibrary:
        def get(self, *, symbol: str, timeframe: str) -> FakeDataset:
            return FakeDataset()

        def load_bars(self, dataset: FakeDataset, **kwargs: object) -> list[object]:
            calls.append(kwargs)
            return []

    monkeypatch.setattr(data_library, "default_library", lambda: FakeLibrary())

    result = sweep_orb_params.run_one(
        sweep_orb_params.SweepCell(
            range_minutes=15,
            rr_target=2.0,
            atr_stop_mult=1.5,
            ema_bias_period=200,
        ),
        symbol="MNQ1",
        timeframe="5m",
        window_days=60,
        step_days=30,
        max_bars=100,
        bar_slice="tail",
    )

    assert calls == [
        {
            "limit": 100,
            "limit_from": "tail",
            "require_positive_prices": True,
        }
    ]
    assert result.n_windows == 0
    assert result.pass_gate is False


def test_sweep_orb_run_one_returns_zero_result_when_dataset_is_missing(monkeypatch) -> None:
    class FakeLibrary:
        def get(self, *, symbol: str, timeframe: str) -> None:
            return None

    monkeypatch.setattr(data_library, "default_library", lambda: FakeLibrary())

    result = sweep_orb_params.run_one(
        sweep_orb_params.SweepCell(
            range_minutes=15,
            rr_target=2.0,
            atr_stop_mult=1.5,
            ema_bias_period=200,
        ),
        symbol="MNQ1",
        timeframe="5m",
        window_days=60,
        step_days=30,
    )

    assert result.n_windows == 0
    assert result.pass_gate is False
