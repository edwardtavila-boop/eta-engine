from __future__ import annotations

from datetime import UTC, datetime, time

from eta_engine.data import library as data_library
from eta_engine.scripts import run_orb_walk_forward


def test_orb_config_from_env_uses_mnq_defaults(monkeypatch) -> None:
    for key in (
        "ORB_RANGE_MINUTES",
        "ORB_RTH_OPEN",
        "ORB_RTH_CLOSE",
        "ORB_MAX_ENTRY",
        "ORB_FLATTEN_AT",
        "ORB_TIMEZONE",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = run_orb_walk_forward._orb_config_from_env()

    assert cfg.range_minutes == 15
    assert cfg.rth_open_local == time(9, 30)
    assert cfg.rth_close_local == time(16, 0)
    assert cfg.timezone_name == "America/New_York"


def test_orb_config_from_env_accepts_crypto_session_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ORB_RANGE_MINUTES", "240")
    monkeypatch.setenv("ORB_RTH_OPEN", "00:00")
    monkeypatch.setenv("ORB_MAX_ENTRY", "23:00")
    monkeypatch.setenv("ORB_TIMEZONE", "UTC")

    cfg = run_orb_walk_forward._orb_config_from_env()

    assert cfg.range_minutes == 240
    assert cfg.rth_open_local == time(0, 0)
    assert cfg.max_entry_local == time(23, 0)
    assert cfg.timezone_name == "UTC"


def test_run_orb_walk_forward_aborts_when_positive_price_filter_empties_dataset(
    monkeypatch,
    capsys,
) -> None:
    load_calls: list[dict[str, object]] = []

    class FakeDataset:
        symbol = "MNQ1"
        timeframe = "5m"
        schema_kind = "history"
        row_count = 3
        start_ts = datetime(2026, 1, 1, tzinfo=UTC)
        end_ts = datetime(2026, 1, 2, tzinfo=UTC)

        def days_span(self) -> float:
            return 1.0

    class FakeLibrary:
        def get(self, *, symbol: str, timeframe: str) -> FakeDataset:
            return FakeDataset()

        def load_bars(self, dataset: FakeDataset, **kwargs: object) -> list[object]:
            load_calls.append(kwargs)
            return []

    monkeypatch.setattr(data_library, "default_library", lambda: FakeLibrary())
    monkeypatch.setenv("MNQ_SYMBOL", "MNQ1")
    monkeypatch.setenv("MNQ_TIMEFRAME", "5m")

    assert run_orb_walk_forward.main() == 1
    assert load_calls == [{"require_positive_prices": True}]
    assert "no positive-price tradable bars" in capsys.readouterr().out
