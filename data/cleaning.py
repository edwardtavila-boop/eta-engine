"""
EVOLUTIONARY TRADING ALGO  //  data.cleaning
================================
Gap detection, outlier removal (MAD), duplicate drop, bar validation.
All functions pure — no pandas.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from eta_engine.core.data_pipeline import BarData

# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


def detect_gaps(
    bars: list[BarData],
    expected_freq_s: int,
) -> list[tuple[datetime, datetime]]:
    """Return a list of (gap_start, gap_end) ranges where bars are missing.

    Gap is detected when delta > 1.5 * expected_freq_s.
    """
    if len(bars) < 2 or expected_freq_s <= 0:
        return []
    threshold = expected_freq_s * 1.5
    gaps: list[tuple[datetime, datetime]] = []
    for a, b in zip(bars, bars[1:], strict=False):
        dt_s = (b.timestamp - a.timestamp).total_seconds()
        if dt_s > threshold:
            gaps.append((a.timestamp, b.timestamp))
    return gaps


def fill_gaps(
    bars: list[BarData],
    expected_freq_s: int,
    method: str = "forward",
) -> list[BarData]:
    """Insert synthetic bars to close any gaps.

    method: 'forward' (carry prev close), 'linear' (interpolate close), 'drop' (return unchanged)
    """
    if method == "drop" or len(bars) < 2 or expected_freq_s <= 0:
        return list(bars)
    step = timedelta(seconds=expected_freq_s)
    out: list[BarData] = []
    for i, b in enumerate(bars):
        out.append(b)
        if i == len(bars) - 1:
            break
        nxt = bars[i + 1]
        dt_s = (nxt.timestamp - b.timestamp).total_seconds()
        if dt_s <= expected_freq_s * 1.5:
            continue
        n_missing = int(round(dt_s / expected_freq_s)) - 1
        for k in range(1, n_missing + 1):
            ts = b.timestamp + step * k
            if method == "forward":
                price = b.close
            else:  # linear
                frac = k / (n_missing + 1)
                price = b.close + frac * (nxt.close - b.close)
            out.append(
                BarData(
                    timestamp=ts,
                    symbol=b.symbol,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=0.0,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Outliers (Median Absolute Deviation)
# ---------------------------------------------------------------------------


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def remove_outliers_mad(
    bars: list[BarData],
    threshold: float = 5.0,
) -> list[BarData]:
    """Drop bars whose |close - median| / MAD > threshold.

    MAD uses the robust scaling factor 1.4826 to approximate stdev under normal.
    If the MAD collapses to zero (e.g. a constant series with a single spike),
    we fall back to mean absolute deviation so a lone outlier is still caught.
    """
    if len(bars) < 3:
        return list(bars)
    closes = [b.close for b in bars]
    med = _median(closes)
    abs_dev = [abs(c - med) for c in closes]
    scale = _median(abs_dev) * 1.4826
    if scale <= 0.0:
        mean_abs = sum(abs_dev) / len(abs_dev)
        if mean_abs <= 0.0:
            return list(bars)
        scale = mean_abs
    return [b for b in bars if abs(b.close - med) / scale <= threshold]


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def detect_duplicates(bars: list[BarData]) -> list[BarData]:
    """Drop rows sharing the same timestamp — keep the LAST one."""
    seen: dict[datetime, BarData] = {}
    for b in bars:
        seen[b.timestamp] = b
    return sorted(seen.values(), key=lambda x: x.timestamp)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_bar(bar: BarData) -> list[str]:
    """Return list of validation errors. Empty means clean."""
    errs: list[str] = []
    if bar.high < bar.low:
        errs.append("high < low")
    if bar.close > bar.high or bar.close < bar.low:
        errs.append("close outside [low, high]")
    if bar.open > bar.high or bar.open < bar.low:
        errs.append("open outside [low, high]")
    if bar.volume < 0.0:
        errs.append("negative volume")
    if bar.open <= 0.0 or bar.close <= 0.0:
        errs.append("non-positive price")
    return errs
