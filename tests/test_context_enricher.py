"""Tests for eta_engine.brain.jarvis_v3.context_enricher — multi-TF + event-aware context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_v3 import context_enricher as cx


def test_enrich_returns_EnrichedContext() -> None:  # noqa: N802 — name fixed by plan brief
    """A basic call returns an EnrichedContext frozen dataclass with all fields set."""
    now = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    out = cx.enrich(symbol="BTC", asset_class="crypto", now=now)
    assert isinstance(out, cx.EnrichedContext)
    # Frozen dataclass: assignment must fail.
    import dataclasses

    assert dataclasses.is_dataclass(out)
    # Required fields present.
    assert isinstance(out.multi_tf, dict)
    assert isinstance(out.session, str)
    assert isinstance(out.time_of_day_risk, float)
    assert isinstance(out.multi_tf_agreement, float)


def test_enrich_handles_missing_library(monkeypatch) -> None:
    """If data.library.default_library raises, enrich still returns a valid context with empty multi_tf."""

    def _boom():
        raise RuntimeError("library is dormant")

    monkeypatch.setattr("eta_engine.data.library.default_library", _boom)
    now = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    out = cx.enrich(symbol="MNQ", asset_class="futures", now=now)
    assert isinstance(out, cx.EnrichedContext)
    assert out.multi_tf == {}
    assert out.multi_tf_agreement == 0.0


def test_session_detection_at_2030_utc_is_ny_pm() -> None:
    """20:30 UTC falls in the NY_PM band (17..21)."""
    now = datetime(2026, 5, 11, 20, 30, tzinfo=UTC)
    out = cx.enrich(symbol="BTC", asset_class="crypto", now=now)
    assert out.session == "NY_PM"


def test_session_detection_at_0500_utc_is_asia() -> None:
    """05:00 UTC is in the ASIA band (02..08)."""
    now = datetime(2026, 5, 11, 5, 0, tzinfo=UTC)
    out = cx.enrich(symbol="BTC", asset_class="crypto", now=now)
    assert out.session == "ASIA"


def test_time_of_day_risk_in_overnight_is_high() -> None:
    """OVERNIGHT (21..22 UTC) is high risk (> 0.7)."""
    now = datetime(2026, 5, 11, 21, 30, tzinfo=UTC)
    out = cx.enrich(symbol="BTC", asset_class="crypto", now=now)
    assert out.session == "OVERNIGHT"
    assert out.time_of_day_risk > 0.7


def test_nearby_events_populated_pre_fomc(tmp_path, monkeypatch) -> None:
    """A FOMC event 30 minutes in the future surfaces in nearby_events."""
    now = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    future_ts = now + timedelta(minutes=30)
    yaml_text = (
        "events:\n"
        f'  - ts_utc: "{future_ts.strftime("%Y-%m-%dT%H:%M:%SZ")}"\n'
        "    kind: FOMC\n"
        "    symbol: null\n"
        "    severity: 3\n"
    )
    cal_path = tmp_path / "cal.yaml"
    cal_path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr("eta_engine.data.event_calendar.DEFAULT_YAML_PATH", cal_path)

    out = cx.enrich(symbol="BTC", asset_class="crypto", now=now)
    assert len(out.nearby_events) == 1
    assert out.nearby_events[0].kind == "FOMC"


def test_multi_tf_agreement_all_bull(monkeypatch) -> None:
    """When library returns bullish trend on every TF, agreement is positive and > 0.5."""

    class _FakeDataset:
        def __init__(self) -> None:
            self.symbol = "BTC"
            self.timeframe = "5m"

    class _FakeLib:
        def get(self, *, symbol: str, timeframe: str, schema_kind: str | None = None):
            return _FakeDataset()

        def load_bars(self, dataset, *, limit=None, limit_from="head", require_positive_prices=False):
            # Return a small list of bars with rising closes => bullish trend
            class _Bar:
                def __init__(self, close: float) -> None:
                    self.close = close

            return [_Bar(100.0 + i) for i in range(25)]

    monkeypatch.setattr("eta_engine.data.library.default_library", lambda: _FakeLib())

    now = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    out = cx.enrich(symbol="BTC", asset_class="crypto", now=now)
    assert out.multi_tf_agreement > 0.5
