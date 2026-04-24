"""
APEX PREDATOR  //  tests.test_consistency_guard
===============================================
Unit tests for the Apex 30% consistency-rule tracker.

Coverage:
  * init / persistence round-trip
  * threshold + warning validation
  * OK / WARNING / VIOLATION / INSUFFICIENT_DATA statuses
  * largest-day detection across a mixed winning/losing history
  * intraday overwrite of today's entry
  * headroom math:
      - prior total zero -> zero headroom
      - prior total positive, today = 0 -> regime-B cap = t/(1-t) * prior_total
      - today already near largest-day -> headroom drops accordingly
      - VIOLATION state -> headroom clipped to 0
  * reset() preserves thresholds, wipes days
  * ISO-date validation
  * corrupt file fail-closed
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from apex_predator.core.consistency_guard import (
    ConsistencyCorruptError,
    ConsistencyGuard,
    ConsistencyStatus,
    apex_trading_day_iso,
    default_apex_50k_guard,
    utc_today_iso,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def guard_path(tmp_path: Path) -> Path:
    return tmp_path / "consistency.json"


@pytest.fixture
def guard(guard_path: Path) -> ConsistencyGuard:
    return ConsistencyGuard.load_or_init(
        path=guard_path, threshold_pct=0.30, warning_pct=0.25,
    )


# ---------------------------------------------------------------------------
# Init + persistence
# ---------------------------------------------------------------------------

class TestInit:
    def test_fresh_init_writes_empty_days(
        self, guard: ConsistencyGuard,
    ) -> None:
        s = guard.state()
        assert s.threshold_pct == 0.30
        assert s.warning_pct == 0.25
        assert s.days == {}

    def test_fresh_init_persists(
        self, guard: ConsistencyGuard, guard_path: Path,
    ) -> None:
        assert guard_path.exists()
        raw = json.loads(guard_path.read_text())
        assert raw["threshold_pct"] == 0.30
        assert raw["warning_pct"] == 0.25
        assert raw["days"] == {}

    def test_invalid_threshold_raises(self, guard_path: Path) -> None:
        with pytest.raises(ValueError, match="threshold_pct"):
            ConsistencyGuard.load_or_init(
                path=guard_path, threshold_pct=1.5, warning_pct=0.25,
            )
        with pytest.raises(ValueError, match="threshold_pct"):
            ConsistencyGuard.load_or_init(
                path=guard_path, threshold_pct=0.0, warning_pct=0.25,
            )

    def test_invalid_warning_raises(self, guard_path: Path) -> None:
        with pytest.raises(ValueError, match="warning_pct"):
            ConsistencyGuard.load_or_init(
                path=guard_path, threshold_pct=0.30, warning_pct=0.40,
            )
        with pytest.raises(ValueError, match="warning_pct"):
            ConsistencyGuard.load_or_init(
                path=guard_path, threshold_pct=0.30, warning_pct=0.0,
            )

    def test_roundtrip_loads_prior_days(
        self, guard_path: Path,
    ) -> None:
        g1 = ConsistencyGuard.load_or_init(
            path=guard_path, threshold_pct=0.30, warning_pct=0.25,
        )
        g1.record_eod("2026-04-20", 500.0)
        g1.record_eod("2026-04-21", 300.0)
        g2 = ConsistencyGuard.load_or_init(
            path=guard_path, threshold_pct=0.30, warning_pct=0.25,
        )
        assert g2.state().days == {
            "2026-04-20": 500.0,
            "2026-04-21": 300.0,
        }

    def test_default_apex_50k_constructor(self, guard_path: Path) -> None:
        g = default_apex_50k_guard(guard_path)
        assert g.state().threshold_pct == 0.30
        assert g.state().warning_pct == 0.25

    def test_utc_today_iso_has_dashes(self) -> None:
        s = utc_today_iso()
        assert len(s) == 10
        assert s[4] == "-"
        assert s[7] == "-"

    def test_corrupt_file_raises(self, guard_path: Path) -> None:
        guard_path.write_text("not-json-at-all", encoding="utf-8")
        with pytest.raises(ConsistencyCorruptError, match="corrupt"):
            ConsistencyGuard.load_or_init(
                path=guard_path, threshold_pct=0.30, warning_pct=0.25,
            )

    def test_corrupt_date_key_raises(self, guard_path: Path) -> None:
        guard_path.write_text(
            json.dumps({
                "threshold_pct": 0.30, "warning_pct": 0.25,
                "days": {"not-a-date": 500.0},
            }),
            encoding="utf-8",
        )
        with pytest.raises(ConsistencyCorruptError, match="invalid date"):
            ConsistencyGuard.load_or_init(
                path=guard_path, threshold_pct=0.30, warning_pct=0.25,
            )


# ---------------------------------------------------------------------------
# Recording + status
# ---------------------------------------------------------------------------

class TestRecording:
    def test_record_eod_persists_day(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_eod("2026-04-20", 500.0)
        assert guard.state().days == {"2026-04-20": 500.0}

    def test_record_eod_overwrites_same_day(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_eod("2026-04-20", 500.0)
        guard.record_eod("2026-04-20", 620.0)
        assert guard.state().days == {"2026-04-20": 620.0}

    def test_record_intraday_is_same_as_record_eod(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_intraday("2026-04-20", 420.0)
        assert guard.state().days == {"2026-04-20": 420.0}

    def test_invalid_date_format_raises(
        self, guard: ConsistencyGuard,
    ) -> None:
        with pytest.raises(ValueError, match="ISO"):
            guard.record_eod("2026/04/20", 500.0)
        with pytest.raises(ValueError, match="ISO"):
            guard.record_eod("not-a-date", 500.0)


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------

class TestStatus:
    def test_insufficient_data_when_no_days(
        self, guard: ConsistencyGuard,
    ) -> None:
        v = guard.evaluate()
        assert v.status is ConsistencyStatus.INSUFFICIENT_DATA

    def test_insufficient_data_when_net_negative(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_eod("2026-04-20", -500.0)
        v = guard.evaluate()
        assert v.status is ConsistencyStatus.INSUFFICIENT_DATA

    def test_ok_when_largest_below_warning(
        self, guard: ConsistencyGuard,
    ) -> None:
        # 4 winning days of 500 each => largest ratio = 500/2000 = 25% < 25? No, =25%
        # Use 400s instead => 400/1600 = 25%. Let's use 200s + one 200.
        # Need ratio < 0.25. Try 3 winners of 500 each, 1 winner of 500 => 500/2000 = 25%.
        # To stay < 25%, use 4 winners of 500 + one 400 = 2400 total, ratio = 500/2400 ~= 20.8%.
        for d, p in [("2026-04-20", 500.0), ("2026-04-21", 500.0),
                     ("2026-04-22", 500.0), ("2026-04-23", 500.0),
                     ("2026-04-24", 400.0)]:
            guard.record_eod(d, p)
        v = guard.evaluate()
        assert v.status is ConsistencyStatus.OK
        assert v.largest_day_ratio == pytest.approx(500 / 2400, abs=1e-6)

    def test_warning_when_between_warning_and_threshold(
        self, guard: ConsistencyGuard,
    ) -> None:
        # 700/2500 = 28% -> WARNING (25% <= r < 30%)
        guard.record_eod("2026-04-20", 700.0)
        guard.record_eod("2026-04-21", 400.0)
        guard.record_eod("2026-04-22", 500.0)
        guard.record_eod("2026-04-23", 500.0)
        guard.record_eod("2026-04-24", 400.0)
        v = guard.evaluate()
        assert v.status is ConsistencyStatus.WARNING
        assert v.largest_day_usd == 700.0
        assert v.largest_day_date == "2026-04-20"
        assert v.largest_day_ratio == pytest.approx(700 / 2500, abs=1e-6)

    def test_violation_when_ratio_at_or_above_threshold(
        self, guard: ConsistencyGuard,
    ) -> None:
        # 1500 on day 1 out of 2500 total = 60% -> VIOLATION
        guard.record_eod("2026-04-20", 1500.0)
        guard.record_eod("2026-04-21", 250.0)
        guard.record_eod("2026-04-22", 250.0)
        guard.record_eod("2026-04-23", 250.0)
        guard.record_eod("2026-04-24", 250.0)
        v = guard.evaluate()
        assert v.status is ConsistencyStatus.VIOLATION
        assert v.largest_day_ratio == pytest.approx(0.60, abs=1e-6)

    def test_losing_days_reduce_total_and_can_trigger_violation(
        self, guard: ConsistencyGuard,
    ) -> None:
        # $500 win, $500 loss, $300 win, $200 win => total=500, largest=500
        # ratio = 500/500 = 100% -> VIOLATION
        guard.record_eod("2026-04-20", 500.0)
        guard.record_eod("2026-04-21", -500.0)
        guard.record_eod("2026-04-22", 300.0)
        guard.record_eod("2026-04-23", 200.0)
        v = guard.evaluate()
        assert v.status is ConsistencyStatus.VIOLATION
        assert v.largest_day_usd == 500.0
        assert v.total_net_profit_usd == 500.0

    def test_max_allowed_day_scales_with_total(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_eod("2026-04-20", 300.0)
        guard.record_eod("2026-04-21", 400.0)
        guard.record_eod("2026-04-22", 300.0)  # total=1000
        v = guard.evaluate()
        assert v.max_allowed_day_usd == pytest.approx(300.0)  # 0.30 * 1000


# ---------------------------------------------------------------------------
# Headroom math
# ---------------------------------------------------------------------------

class TestHeadroom:
    def test_no_prior_days_zero_headroom(
        self, guard: ConsistencyGuard,
    ) -> None:
        # Prior total = 0, today_pnl = 0 -> regime-B cap = 0
        v = guard.evaluate(today_date="2026-04-24", today_pnl_usd=0.0)
        assert v.headroom_today_usd == pytest.approx(0.0)

    def test_regime_b_cap_exact(
        self, guard: ConsistencyGuard,
    ) -> None:
        # prior days total = 1000, all losers/small-ish so prior_max_win
        # is below regime_b_cap. regime_b_cap = 0.30 * 1000 / 0.70 = 428.57
        guard.record_eod("2026-04-20", 100.0)
        guard.record_eod("2026-04-21", 100.0)
        guard.record_eod("2026-04-22", 100.0)
        guard.record_eod("2026-04-23", 700.0)  # prior_max_win = 700
        # prior_total = 1000 -> regime_b_cap = 0.30*1000/0.70 = 428.57
        # prior_max_win = 700 > 428.57, so we're in the prior_max_win branch.
        # floor_needed = 700/0.30 - 1000 = 2333.33 - 1000 = 1333.33
        # floor_needed=1333 > prior_max_win=700 -> already in violation
        # => headroom = 0
        v = guard.evaluate(today_date="2026-04-24", today_pnl_usd=0.0)
        assert v.headroom_today_usd == pytest.approx(0.0)

    def test_clean_prior_regime_b_cap_applies(
        self, guard: ConsistencyGuard,
    ) -> None:
        # Build a prior where prior_max_win <= regime_b_cap
        # 5 days of 300 each => total=1500, prior_max_win=300
        # regime_b_cap = 0.30*1500/0.70 = 642.857
        # 300 <= 642.857, clean case
        # Today pnl=0 -> headroom = 642.857 - 0 = 642.857
        for d in ["2026-04-20", "2026-04-21", "2026-04-22",
                  "2026-04-23", "2026-04-24"]:
            guard.record_eod(d, 300.0)
        v = guard.evaluate(today_date="2026-04-25", today_pnl_usd=0.0)
        expected_cap = 0.30 * 1500.0 / 0.70
        assert v.headroom_today_usd == pytest.approx(expected_cap, abs=1e-4)

    def test_headroom_shrinks_as_today_pnl_grows(
        self, guard: ConsistencyGuard,
    ) -> None:
        # Same prior as above, but today already made 200
        for d in ["2026-04-20", "2026-04-21", "2026-04-22",
                  "2026-04-23", "2026-04-24"]:
            guard.record_eod(d, 300.0)
        v = guard.evaluate(today_date="2026-04-25", today_pnl_usd=200.0)
        expected_cap = 0.30 * 1500.0 / 0.70
        expected_headroom = expected_cap - 200.0
        assert v.headroom_today_usd == pytest.approx(expected_headroom, abs=1e-4)

    def test_headroom_clipped_to_zero_when_at_cap(
        self, guard: ConsistencyGuard,
    ) -> None:
        for d in ["2026-04-20", "2026-04-21", "2026-04-22",
                  "2026-04-23", "2026-04-24"]:
            guard.record_eod(d, 300.0)
        cap = 0.30 * 1500.0 / 0.70
        v = guard.evaluate(today_date="2026-04-25", today_pnl_usd=cap)
        assert v.headroom_today_usd == pytest.approx(0.0, abs=1e-4)

    def test_headroom_clipped_to_zero_above_cap(
        self, guard: ConsistencyGuard,
    ) -> None:
        for d in ["2026-04-20", "2026-04-21", "2026-04-22",
                  "2026-04-23", "2026-04-24"]:
            guard.record_eod(d, 300.0)
        v = guard.evaluate(today_date="2026-04-25", today_pnl_usd=9_999.0)
        assert v.headroom_today_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Intraday scenarios
# ---------------------------------------------------------------------------

class TestIntradayScenarios:
    def test_intraday_update_progresses_toward_violation(
        self, guard: ConsistencyGuard,
    ) -> None:
        # Build a prior with total=2000 and clean distribution.
        for d, p in [("2026-04-20", 400.0), ("2026-04-21", 400.0),
                     ("2026-04-22", 400.0), ("2026-04-23", 400.0),
                     ("2026-04-24", 400.0)]:
            guard.record_eod(d, p)
        # Today opens flat
        v0 = guard.record_intraday("2026-04-25", 0.0)
        assert v0.status is ConsistencyStatus.OK

        # Today climbs to 300: total=2300, today=300, ratio=300/2300=13%
        v1 = guard.record_intraday("2026-04-25", 300.0)
        assert v1.status is ConsistencyStatus.OK

        # Today climbs to 650: total=2650, today=650, ratio=24.5%
        # prior_max_win=400 -> not largest. But today=650 is largest
        # when > 400 -> check: 650/2650 = 24.5% < 25% -> still OK
        v2 = guard.record_intraday("2026-04-25", 650.0)
        assert v2.largest_day_usd == 650.0
        assert v2.largest_day_date == "2026-04-25"
        assert v2.status is ConsistencyStatus.OK

        # Today climbs to 900: total=2900, today=900, ratio=31% -> VIOLATION
        v3 = guard.record_intraday("2026-04-25", 900.0)
        assert v3.largest_day_ratio > 0.30
        assert v3.status is ConsistencyStatus.VIOLATION

    def test_intraday_entry_does_not_duplicate_day(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_intraday("2026-04-25", 100.0)
        guard.record_intraday("2026-04-25", 300.0)
        guard.record_intraday("2026-04-25", 500.0)
        assert guard.state().days == {"2026-04-25": 500.0}


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_clears_days(self, guard: ConsistencyGuard) -> None:
        guard.record_eod("2026-04-20", 500.0)
        guard.record_eod("2026-04-21", 400.0)
        guard.reset()
        assert guard.state().days == {}

    def test_reset_preserves_thresholds(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_eod("2026-04-20", 500.0)
        guard.reset()
        assert guard.state().threshold_pct == 0.30
        assert guard.state().warning_pct == 0.25

    def test_reset_persists(
        self, guard: ConsistencyGuard, guard_path: Path,
    ) -> None:
        guard.record_eod("2026-04-20", 500.0)
        guard.reset()
        g2 = ConsistencyGuard.load_or_init(
            path=guard_path, threshold_pct=0.30, warning_pct=0.25,
        )
        assert g2.state().days == {}


# ---------------------------------------------------------------------------
# Verdict serialization
# ---------------------------------------------------------------------------

class TestVerdictSerialization:
    def test_as_dict_includes_status_string(
        self, guard: ConsistencyGuard,
    ) -> None:
        guard.record_eod("2026-04-20", 900.0)
        guard.record_eod("2026-04-21", 100.0)
        v = guard.evaluate()
        d = v.as_dict()
        assert d["status"] == ConsistencyStatus.VIOLATION.value
        assert d["largest_day_usd"] == 900.0
        assert d["largest_day_date"] == "2026-04-20"


# ---------------------------------------------------------------------------
# Apex session-day rollover (17:00 CT, DST-aware)
# ---------------------------------------------------------------------------

class TestApexTradingDayIso:
    """Verify ``apex_trading_day_iso`` uses the 17:00 US/Central rollover.

    Apex defines a trading day as the 24-hour window ending at 17:00 CT.
    A UTC-midnight boundary splits a US equity-futures session across two
    day buckets, which understates the real "largest winning day" and
    inflates the denominator in the 30%-rule math. These tests lock in
    the session-day semantics in both DST regimes.
    """

    def test_before_rollover_summer_cdt(self) -> None:
        """21:00 UTC in July = 16:00 CDT → still today's date."""
        from datetime import UTC, datetime
        t = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)  # 16:00 CDT
        assert apex_trading_day_iso(t) == "2026-07-15"

    def test_after_rollover_summer_cdt(self) -> None:
        """22:30 UTC in July = 17:30 CDT → NEXT trading day."""
        from datetime import UTC, datetime
        t = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)  # 17:30 CDT
        assert apex_trading_day_iso(t) == "2026-07-16"

    def test_before_rollover_winter_cst(self) -> None:
        """22:00 UTC in January = 16:00 CST → still today's date."""
        from datetime import UTC, datetime
        t = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)  # 16:00 CST
        assert apex_trading_day_iso(t) == "2026-01-15"

    def test_after_rollover_winter_cst(self) -> None:
        """23:30 UTC in January = 17:30 CST → NEXT trading day."""
        from datetime import UTC, datetime
        t = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)  # 17:30 CST
        assert apex_trading_day_iso(t) == "2026-01-16"

    def test_exactly_at_17_ct_is_next_day(self) -> None:
        """17:00:00 local IS the rollover -- belongs to NEXT day."""
        from datetime import UTC, datetime
        # 17:00 CDT = 22:00 UTC
        t = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
        assert apex_trading_day_iso(t) == "2026-07-16"

    def test_one_second_before_rollover_is_current_day(self) -> None:
        """16:59:59 CDT stays on the current trading day."""
        from datetime import UTC, datetime
        t = datetime(2026, 7, 15, 21, 59, 59, tzinfo=UTC)  # 16:59:59 CDT
        assert apex_trading_day_iso(t) == "2026-07-15"

    def test_overnight_session_charges_to_next_day(self) -> None:
        """A 02:00 UTC timestamp in July (21:00 CDT prior eve) must
        hit the SAME Apex day bucket as a 23:00 UTC timestamp the
        evening before. UTC-midnight would split these into separate
        buckets; the session-day helper keeps them together.
        """
        from datetime import UTC, datetime
        # July 15, 22:30 UTC = 17:30 CDT July 15 → Apex day = July 16
        t_evening = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)
        # July 16, 02:00 UTC = 21:00 CDT July 15 → Apex day = July 16
        t_overnight = datetime(2026, 7, 16, 2, 0, tzinfo=UTC)
        assert apex_trading_day_iso(t_evening) == apex_trading_day_iso(t_overnight)
        assert apex_trading_day_iso(t_evening) == "2026-07-16"

    def test_naive_datetime_treated_as_utc(self) -> None:
        """Input without tzinfo is assumed UTC (not local machine tz)."""
        from datetime import datetime
        t_naive = datetime(2026, 7, 15, 21, 0)
        # Same as tz-aware UTC 21:00 → 16:00 CDT → 2026-07-15
        assert apex_trading_day_iso(t_naive) == "2026-07-15"

    def test_returns_iso_string_today_when_no_arg(self) -> None:
        """With no argument defaults to `datetime.now(UTC)` and returns
        ISO-format string."""
        s = apex_trading_day_iso()
        assert isinstance(s, str)
        assert len(s) == 10
        assert s[4] == "-"
        assert s[7] == "-"

    def test_fallback_when_zoneinfo_missing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If zoneinfo is unavailable the helper uses a fixed 23:00-UTC
        rollover. This is wrong by 1h in summer but never mid-RTH."""
        from datetime import UTC, datetime

        import apex_predator.core.consistency_guard as cg
        monkeypatch.setattr(cg, "ZoneInfo", None)

        # 22:00 UTC → before 23:00 UTC → current date
        assert cg.apex_trading_day_iso(
            datetime(2026, 1, 15, 22, 0, tzinfo=UTC),
        ) == "2026-01-15"
        # 23:30 UTC → past fallback rollover → next date
        assert cg.apex_trading_day_iso(
            datetime(2026, 1, 15, 23, 30, tzinfo=UTC),
        ) == "2026-01-16"

    def test_different_from_utc_today_on_overnight_tick(self) -> None:
        """The two helpers MUST disagree on an evening-session tick.
        This is the bug the session-day helper closes: UTC midnight
        splits the session, so an MNQ overnight tick at 22:30 UTC in
        summer reports different day keys under the two helpers. The
        runtime MUST use apex_trading_day_iso for eval accounting.
        """
        from datetime import UTC, datetime

        import apex_predator.core.consistency_guard as cg

        # 22:30 UTC July 15 → 17:30 CDT → Apex day = July 16
        # But `datetime.now(UTC).date()` on that same clock = July 15
        t = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)
        # We can't monkeypatch datetime.now cleanly, so call the
        # pure function directly and compare to UTC-derived date.
        assert cg.apex_trading_day_iso(t) == "2026-07-16"
        assert t.date().isoformat() == "2026-07-15"
