"""Tests for the trade_close defensive sanitizer.

Real-world cases this module guards against:
  * mnq_futures_sage writes tick count to realized_r (r=69 for $17.25 PnL)
  * legacy bots write raw USD PnL into realized_r (r=-65 for a small loss)
  * malformed strings, missing fields, unknown symbols
"""

from __future__ import annotations

from typing import Any

from eta_engine.brain.jarvis_v3.trade_close_sanitizer import (
    R_SANITY_CEILING,
    classify,
    sanitize_r,
)


def _rec(
    *,
    realized_r: Any = None,
    realized_pnl: Any = None,
    symbol: str | None = None,
    legacy_r: Any = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {}
    if realized_r is not None:
        rec["realized_r"] = realized_r
    if legacy_r is not None:
        rec["r"] = legacy_r
    extra: dict[str, Any] = {}
    if realized_pnl is not None:
        extra["realized_pnl"] = realized_pnl
    if symbol is not None:
        extra["symbol"] = symbol
    if extra:
        rec["extra"] = extra
    return rec


# ---------------------------------------------------------------------------
# classify() returns one of {"clean", "recovered", "suspect", "none"}
# ---------------------------------------------------------------------------


def test_classify_clean_normal_win() -> None:
    """A legitimate 1.5R win is clean and trusted as-is."""
    status, r = classify(_rec(realized_r=1.5))
    assert status == "clean"
    assert r == 1.5


def test_classify_clean_normal_loss() -> None:
    status, r = classify(_rec(realized_r=-0.85))
    assert status == "clean"
    assert r == -0.85


def test_classify_clean_at_ceiling() -> None:
    """A 20R trade is the absolute upper edge — still trusted."""
    status, r = classify(_rec(realized_r=20.0))
    assert status == "clean"
    assert r == 20.0


def test_classify_clean_high_but_realistic() -> None:
    """An 8R win on a strong day is realistic and not flagged."""
    status, r = classify(_rec(realized_r=8.2))
    assert status == "clean"
    assert r == 8.2


def test_classify_recovers_mnq_r69_bug() -> None:
    """REGRESSION: mnq_futures_sage's r=69 (tick count) for $17.25 PnL.

    MNQ is $20/R. $17.25 / $20 = 0.8625R. So the recovered value
    should match.
    """
    status, r = classify(
        _rec(realized_r=69.0, realized_pnl=17.25, symbol="MNQ1"),
    )
    assert status == "recovered"
    assert abs(r - (17.25 / 20.0)) < 1e-6
    assert abs(r) <= R_SANITY_CEILING


def test_classify_recovers_dollar_leak_legacy() -> None:
    """eur_sweep_reclaim wrote -65.88 (USD) into realized_r for an FX trade."""
    status, r = classify(
        _rec(realized_r=-65.88, realized_pnl=-65.88, symbol="6E"),
    )
    # 6E is $62.50/R, so -65.88 / 62.50 = -1.054R
    assert status == "recovered"
    assert abs(r + 1.054) < 0.01


def test_classify_suspect_when_no_recovery_path() -> None:
    """A 100R value with no extra.realized_pnl can't be recovered."""
    status, r = classify(_rec(realized_r=100.0))
    assert status == "suspect"
    # Suspect returns the raw value for diagnostics, but sanitize_r drops it
    assert r == 100.0


def test_classify_suspect_when_unknown_symbol() -> None:
    """Unknown symbol (e.g. ETH/crypto) can't be recovered from USD PnL."""
    status, _ = classify(
        _rec(realized_r=999.0, realized_pnl=200.0, symbol="ETH"),
    )
    assert status == "suspect"


def test_classify_suspect_when_recovery_still_huge() -> None:
    """If recovery yields >20R too, the trade is genuinely suspect."""
    # $5000 PnL on MNQ ($20/R) = 250R, still off the cliff
    status, _ = classify(
        _rec(realized_r=99.0, realized_pnl=5000.0, symbol="MNQ"),
    )
    assert status == "suspect"


def test_classify_none_when_missing() -> None:
    """No realized_r and no legacy alias = no usable r."""
    status, r = classify({})
    assert status == "none"
    assert r is None


def test_classify_none_when_non_numeric() -> None:
    status, r = classify(_rec(realized_r="garbage"))
    assert status == "none"
    assert r is None


def test_classify_falls_back_to_legacy_r_field() -> None:
    """Older records use `r` instead of `realized_r`."""
    status, val = classify(_rec(legacy_r=1.2))
    assert status == "clean"
    assert val == 1.2


# ---------------------------------------------------------------------------
# sanitize_r() — the production interface that returns float|None
# ---------------------------------------------------------------------------


def test_sanitize_returns_clean_value_as_is() -> None:
    assert sanitize_r(_rec(realized_r=1.5)) == 1.5


def test_sanitize_returns_recovered_value() -> None:
    val = sanitize_r(_rec(realized_r=69.0, realized_pnl=17.25, symbol="MNQ1"))
    assert val is not None
    assert abs(val - 0.8625) < 1e-6


def test_sanitize_returns_none_for_suspect() -> None:
    """When recovery fails, downstream gets None so the row is SKIPPED."""
    assert sanitize_r(_rec(realized_r=999.0)) is None


def test_sanitize_returns_none_for_missing() -> None:
    assert sanitize_r({}) is None


def test_sanitize_handles_symbol_with_suffix() -> None:
    """MNQ1, MNQM6, MNQH7 should all match the MNQ root."""
    for sym in ("MNQ1", "MNQM6", "MNQH7", "MNQ"):
        val = sanitize_r(_rec(realized_r=99.0, realized_pnl=20.0, symbol=sym))
        assert val is not None, f"failed for symbol {sym}"
        assert abs(val - 1.0) < 1e-6, f"wrong recovery for {sym}"


def test_sanitize_handles_m6e_root() -> None:
    """M6E is the longest known root; must not be greedy-matched by 'M'."""
    val = sanitize_r(_rec(realized_r=99.0, realized_pnl=12.5, symbol="M6E1"))
    # M6E is $6.25/R, so 12.5 / 6.25 = 2.0R
    assert val is not None
    assert abs(val - 2.0) < 1e-6


# ---------------------------------------------------------------------------
# Integration: anomaly_watcher + pnl_summary now consume the sanitizer
# ---------------------------------------------------------------------------


def test_anomaly_watcher_extract_r_uses_sanitizer() -> None:
    """Wired through: anomaly_watcher._extract_r returns None for r=999 suspect."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    assert anomaly_watcher._extract_r(_rec(realized_r=999.0)) is None


def test_anomaly_watcher_extract_r_recovers_bad_mnq() -> None:
    """r=69 with recovery info returns ~0.86R, not 69R."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    val = anomaly_watcher._extract_r(
        _rec(realized_r=69.0, realized_pnl=17.25, symbol="MNQ1"),
    )
    assert val is not None
    assert abs(val - 0.8625) < 1e-6


def test_pnl_summary_extract_r_uses_sanitizer() -> None:
    """pnl_summary won't pollute MTD with the r=69 bug anymore."""
    from eta_engine.brain.jarvis_v3 import pnl_summary

    assert pnl_summary._extract_r(_rec(realized_r=999.0)) is None
    recovered = pnl_summary._extract_r(
        _rec(realized_r=69.0, realized_pnl=17.25, symbol="MNQ1"),
    )
    assert recovered is not None
    assert abs(recovered - 0.8625) < 1e-6
