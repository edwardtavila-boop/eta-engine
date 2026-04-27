"""tests.test_data_cleaning — gaps, outliers, duplicates, bar validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.core.data_pipeline import BarData
from eta_engine.data.cleaning import (
    detect_duplicates,
    detect_gaps,
    fill_gaps,
    remove_outliers_mad,
    validate_bar,
)


def _bar(ts: datetime, px: float = 100.0, sym: str = "T") -> BarData:
    return BarData(
        timestamp=ts,
        symbol=sym,
        open=px,
        high=px + 1.0,
        low=px - 1.0,
        close=px,
        volume=100.0,
    )


class TestGaps:
    def test_no_gap_on_contiguous_stream(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [_bar(start + timedelta(seconds=60 * i)) for i in range(10)]
        assert detect_gaps(bars, expected_freq_s=60) == []

    def test_detects_single_gap(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [_bar(start + timedelta(seconds=60 * i)) for i in range(3)]
        bars.append(_bar(start + timedelta(seconds=60 * 10)))
        gaps = detect_gaps(bars, expected_freq_s=60)
        assert len(gaps) == 1

    def test_fill_forward_closes_gap(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [
            _bar(start, 100.0),
            _bar(start + timedelta(seconds=240), 110.0),  # 4-bar gap @ 60s freq
        ]
        filled = fill_gaps(bars, expected_freq_s=60, method="forward")
        assert len(filled) == 5  # 2 original + 3 inserted
        assert filled[1].close == 100.0  # forward carry

    def test_fill_linear_interpolates(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [_bar(start, 100.0), _bar(start + timedelta(seconds=180), 130.0)]
        filled = fill_gaps(bars, expected_freq_s=60, method="linear")
        # 1 gap filled w/ 2 interpolated points: 110, 120
        closes = [round(b.close, 2) for b in filled]
        assert 110.0 in closes and 120.0 in closes


class TestOutliers:
    def test_mad_drops_spike(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [_bar(start + timedelta(seconds=60 * i), 100.0) for i in range(20)]
        # Inject outlier
        bars.append(_bar(start + timedelta(seconds=60 * 21), 500.0))
        cleaned = remove_outliers_mad(bars, threshold=5.0)
        assert len(cleaned) == 20
        assert all(b.close < 200.0 for b in cleaned)

    def test_mad_passthrough_on_flat(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [_bar(start + timedelta(seconds=60 * i), 100.0) for i in range(10)]
        assert remove_outliers_mad(bars) == bars


class TestDuplicates:
    def test_dedup_keeps_last(self) -> None:
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        bars = [_bar(ts, 100.0), _bar(ts, 101.0), _bar(ts, 102.0)]
        dedup = detect_duplicates(bars)
        assert len(dedup) == 1 and dedup[0].close == 102.0


class TestValidation:
    def test_valid_bar_returns_empty(self) -> None:
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        assert validate_bar(_bar(ts, 100.0)) == []

    def test_high_below_low_caught(self) -> None:
        b = BarData(
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            symbol="T",
            open=100.0,
            high=90.0,
            low=95.0,
            close=92.0,
            volume=1.0,
        )
        errs = validate_bar(b)
        assert any("high" in e for e in errs)

    def test_negative_volume_caught(self) -> None:
        b = BarData(
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            symbol="T",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=-1.0,
        )
        assert any("negative volume" in e for e in validate_bar(b))
