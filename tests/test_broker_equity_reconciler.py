"""
APEX PREDATOR  //  tests.test_broker_equity_reconciler
======================================================
Unit tests for the R1 drift detector.

Covers:
  * no_broker_data path (source returns None -> in_tolerance=True)
  * within_tolerance path
  * broker_below_logical (our cushion is OVER-stated -- the dangerous case)
  * broker_above_logical (our cushion is UNDER-stated -- fine, but logged)
  * tolerance_usd boundary
  * tolerance_pct boundary
  * zero logical equity
  * source callable raising -> treated as no_data (never crash the runtime)
  * running stats counters
"""
from __future__ import annotations

import pytest

from apex_predator.core.broker_equity_reconciler import (
    BrokerEquityReconciler,
    ReconcileResult,
    ReconcileStats,
)


class TestNoBrokerData:
    """Source returns None -- reconciler must stay silent and in_tolerance."""

    def test_source_returns_none_is_no_data(self):
        rec = BrokerEquityReconciler(broker_equity_source=lambda: None)
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "no_broker_data"
        assert result.in_tolerance is True
        assert result.broker_equity_usd is None
        assert result.drift_usd is None
        assert result.drift_pct_of_logical is None

    def test_no_data_counter_increments(self):
        rec = BrokerEquityReconciler(broker_equity_source=lambda: None)
        rec.reconcile(logical_equity_usd=50_000.0)
        rec.reconcile(logical_equity_usd=50_100.0)
        assert rec.stats.checks_no_data == 2
        assert rec.stats.checks_total == 2
        assert rec.stats.checks_in_tolerance == 0
        assert rec.stats.checks_out_of_tolerance == 0


class TestWithinTolerance:

    def test_small_drift_usd_in_tolerance(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 50_025.0,
            tolerance_usd=50.0,
            tolerance_pct=0.01,
        )
        # drift = 50_000 - 50_025 = -25  (broker above logical by $25)
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.in_tolerance is True
        assert result.reason == "within_tolerance"
        assert result.drift_usd == pytest.approx(-25.0)

    def test_zero_drift_exact_is_in_tolerance(self):
        rec = BrokerEquityReconciler(broker_equity_source=lambda: 50_000.0)
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.in_tolerance is True
        assert result.drift_usd == pytest.approx(0.0)

    def test_within_counter_increments(self):
        rec = BrokerEquityReconciler(broker_equity_source=lambda: 50_010.0)
        rec.reconcile(logical_equity_usd=50_000.0)
        rec.reconcile(logical_equity_usd=50_005.0)
        assert rec.stats.checks_in_tolerance == 2


class TestBrokerBelowLogical:
    """The dangerous case: broker reports LESS than our logical equity,
    so our computed cushion over-states the real cushion."""

    def test_broker_below_logical_is_out_of_tolerance(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_800.0,
            tolerance_usd=50.0,
            tolerance_pct=0.01,
        )
        # drift = 50_000 - 49_800 = +200 (logical above broker)
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.in_tolerance is False
        assert result.reason == "broker_below_logical"
        assert result.drift_usd == pytest.approx(200.0)

    def test_broker_below_logical_increments_out_of_tolerance(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_800.0,
            tolerance_usd=50.0,
        )
        rec.reconcile(logical_equity_usd=50_000.0)
        assert rec.stats.checks_out_of_tolerance == 1
        assert rec.stats.checks_in_tolerance == 0


class TestBrokerAboveLogical:
    """Less-dangerous inverse: broker reports MORE than logical.
    Cushion is under-stated -- we'll trigger FLATTEN_TIER_A_PREEMPTIVE
    earlier than necessary, but the eval won't bust. Still flagged."""

    def test_broker_above_logical_is_out_of_tolerance(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 50_250.0,
            tolerance_usd=50.0,
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.in_tolerance is False
        assert result.reason == "broker_above_logical"
        assert result.drift_usd == pytest.approx(-250.0)


class TestToleranceBoundaries:

    def test_drift_exactly_at_usd_tolerance_is_in_tolerance(self):
        """Tolerance is strict: drift == tolerance is still in tolerance.
        Out-of-tolerance requires abs(drift) > tolerance."""
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_950.0,
            tolerance_usd=50.0,
            tolerance_pct=1.0,   # set high so USD threshold decides
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.drift_usd == pytest.approx(50.0)
        assert result.in_tolerance is True

    def test_drift_one_cent_over_usd_tolerance_out(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_949.99,
            tolerance_usd=50.0,
            tolerance_pct=1.0,
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.drift_usd == pytest.approx(50.01)
        assert result.in_tolerance is False

    def test_pct_tolerance_enforces_small_drift_on_large_accounts(self):
        # tolerance_pct=0.1% of $150K = $150 -- drift of $200 is out,
        # even if drift < tolerance_usd=50 is impossible here anyway.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 149_800.0,
            tolerance_usd=10_000.0,   # set huge so pct decides
            tolerance_pct=0.001,
        )
        result = rec.reconcile(logical_equity_usd=150_000.0)
        assert result.drift_usd == pytest.approx(200.0)
        # pct = 200/150000 = 0.00133 > 0.001 -> out
        assert result.in_tolerance is False
        assert result.reason == "broker_below_logical"

    def test_pct_tolerance_permits_small_absolute_on_large_accounts(self):
        # 0.1% of $150K = $150 tolerance; drift of $100 is in.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 149_900.0,
            tolerance_usd=10_000.0,
            tolerance_pct=0.001,
        )
        result = rec.reconcile(logical_equity_usd=150_000.0)
        assert result.drift_usd == pytest.approx(100.0)
        assert result.in_tolerance is True


class TestEdgeCases:

    def test_zero_logical_equity_short_circuits_to_no_data(self):
        # H5 closure (Red Team v0.1.64 review): when logical equity is
        # below the min_logical_usd floor (default 1.0), reconcile must
        # NOT compute a drift percentage (was producing float('inf') and
        # corrupting runtime_log.jsonl per RFC 8259). It now classifies
        # as no_broker_data so the runtime path stays uniform.
        rec = BrokerEquityReconciler(broker_equity_source=lambda: 0.0)
        result = rec.reconcile(logical_equity_usd=0.0)
        assert result.reason == "no_broker_data"
        assert result.drift_usd is None
        assert result.drift_pct_of_logical is None
        assert result.in_tolerance is True

    def test_zero_logical_with_nonzero_broker_short_circuits_to_no_data(self):
        # H5 closure: same guard fires regardless of broker value when
        # logical is below the floor. The asymmetric drift detection
        # (logical_zero, broker=$10K = "broker says you have $10K but
        # the bot books say zero") is real but cannot be expressed as a
        # percentage, so we degrade to no_broker_data and let the
        # operator surface the discrepancy via boot-time bot-state
        # inspection rather than letting an inf escape into the JSON
        # tick log.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 10.0,
            tolerance_usd=1.0,
        )
        result = rec.reconcile(logical_equity_usd=0.0)
        assert result.reason == "no_broker_data"
        assert result.drift_pct_of_logical is None
        assert result.in_tolerance is True

    def test_below_floor_logical_does_not_emit_inf_in_serialized_json(self):
        # H5 regression pin: the whole point of the guard is to keep
        # docs/runtime_log.jsonl strict-RFC-8259-parseable. Round-trip
        # the result through json.dumps with allow_nan=False (which
        # raises on inf/nan if they leak through) and confirm no
        # "Infinity" / "NaN" tokens appear.
        import json as _json
        rec = BrokerEquityReconciler(broker_equity_source=lambda: 50_000.0)
        result = rec.reconcile(logical_equity_usd=0.0)
        # allow_nan=False makes json.dumps raise on inf/nan rather than
        # silently producing the non-RFC-8259 "Infinity" string.
        encoded = _json.dumps(result.as_dict(), allow_nan=False)
        assert "Infinity" not in encoded
        assert "NaN" not in encoded

    def test_source_raising_is_treated_as_no_data(self):
        def _broken():
            raise RuntimeError("broker adapter disconnected")

        rec = BrokerEquityReconciler(broker_equity_source=_broken)
        # Must not re-raise; must classify as no_broker_data.
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "no_broker_data"
        assert result.in_tolerance is True
        assert rec.stats.checks_no_data == 1

    def test_negative_tolerance_usd_rejected(self):
        with pytest.raises(ValueError, match="tolerance_usd"):
            BrokerEquityReconciler(
                broker_equity_source=lambda: None,
                tolerance_usd=-1.0,
            )

    def test_negative_tolerance_pct_rejected(self):
        with pytest.raises(ValueError, match="tolerance_pct"):
            BrokerEquityReconciler(
                broker_equity_source=lambda: None,
                tolerance_pct=-0.01,
            )


class TestReconcileResultShape:

    def test_as_dict_contains_all_fields(self):
        result = ReconcileResult(
            ts="2026-04-24T12:00:00+00:00",
            logical_equity_usd=50_000.0,
            broker_equity_usd=49_900.0,
            drift_usd=100.0,
            drift_pct_of_logical=0.002,
            in_tolerance=False,
            reason="broker_below_logical",
        )
        d = result.as_dict()
        assert set(d.keys()) == {
            "ts", "logical_equity_usd", "broker_equity_usd",
            "drift_usd", "drift_pct_of_logical", "in_tolerance", "reason",
        }
        assert d["reason"] == "broker_below_logical"


class TestStats:

    def test_max_drift_tracks_running_maximum(self):
        equities = iter([49_900.0, 50_100.0, 49_850.0, 50_000.0])
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: next(equities),
            tolerance_usd=10.0,
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=100
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=-100
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=150
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=0
        # max_drift is the largest ABS drift observed
        assert rec.stats.max_drift_usd_abs == pytest.approx(150.0)
        assert rec.stats.checks_total == 4

    def test_last_result_is_most_recent(self):
        equities = iter([49_900.0, 50_000.0])
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: next(equities),
            tolerance_usd=50.0,
        )
        r1 = rec.reconcile(logical_equity_usd=50_000.0)
        r2 = rec.reconcile(logical_equity_usd=50_000.0)
        assert rec.stats.last_result is r2
        assert rec.stats.last_result is not r1

    def test_stats_shape(self):
        s = ReconcileStats()
        assert s.checks_total == 0
        assert s.checks_no_data == 0
        assert s.checks_in_tolerance == 0
        assert s.checks_out_of_tolerance == 0
        assert s.max_drift_usd_abs == 0.0
        assert s.last_result is None
