"""
EVOLUTIONARY TRADING ALGO  //  tests.test_broker_equity_reconciler
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

from eta_engine.core.broker_equity_reconciler import (
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
            tolerance_pct=1.0,  # set high so USD threshold decides
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
            tolerance_usd=10_000.0,  # set huge so pct decides
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
        # The schema includes the original 7 fields plus the H3
        # closure fields (is_in_drift_state + transition) that v0.1.66
        # adds to track the latched drift state across ticks.
        assert set(d.keys()) >= {
            "ts",
            "logical_equity_usd",
            "broker_equity_usd",
            "drift_usd",
            "drift_pct_of_logical",
            "in_tolerance",
            "reason",
        }
        assert d["reason"] == "broker_below_logical"
        # Spot-check the H3 fields exist with their default values
        # (manually-constructed ReconcileResult uses the dataclass
        # defaults: is_in_drift_state=False, transition="stable").
        assert d.get("is_in_drift_state") is False
        assert d.get("transition") == "stable"


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


class TestAsymmetricTolerances:
    """H2 closure (Red Team v0.1.64 review): per-direction tolerances.

    The Red Team finding was: ``broker_below_logical`` (cushion
    over-stated, eval-bust risk) and ``broker_above_logical`` (cushion
    under-stated, MTM-lag / rebate, harmless) used the SAME absolute
    thresholds. To keep below tight at $20, you also had to keep above
    tight at $20, which made benign IBKR-MTM-overshoot moves trip
    drift events. Asymmetric thresholds let the operator set
    ``tolerance_below_*`` strict (eval-protective) and
    ``tolerance_above_*`` loose (anti-spam).
    """

    def test_below_tighter_than_above_drift_below_threshold_trips(self):
        # Below threshold $20, above threshold $200. Drift = +$50
        # (broker BELOW logical). Should trip via the tight below.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_950.0,  # broker below
            tolerance_below_usd=20.0,
            tolerance_below_pct=10.0,  # disable pct check for this case
            tolerance_above_usd=200.0,
            tolerance_above_pct=10.0,
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "broker_below_logical"
        assert result.in_tolerance is False
        assert result.drift_usd == pytest.approx(50.0)

    def test_below_tighter_than_above_drift_above_threshold_passes(self):
        # Below $20, above $200. Drift = -$50 (broker ABOVE logical).
        # Above's threshold is $200, so $50 is within tolerance. The
        # SAME drift magnitude that tripped below would NOT trip above.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 50_050.0,  # broker above
            tolerance_below_usd=20.0,
            tolerance_below_pct=10.0,
            tolerance_above_usd=200.0,
            tolerance_above_pct=10.0,
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "within_tolerance"
        assert result.in_tolerance is True
        assert result.drift_usd == pytest.approx(-50.0)

    def test_above_threshold_only_trip_does_not_affect_below_classification(self):
        # Below $20, above $200. Drift = -$300 (broker WAY above
        # logical). Above $200 threshold trips; classification is
        # broker_above_logical, NOT broker_below_logical.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 50_300.0,
            tolerance_below_usd=20.0,
            tolerance_below_pct=10.0,
            tolerance_above_usd=200.0,
            tolerance_above_pct=10.0,
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "broker_above_logical"
        assert result.in_tolerance is False
        assert result.drift_usd == pytest.approx(-300.0)

    def test_only_below_overridden_above_falls_back_to_symmetric(self):
        # Caller sets tolerance_below_usd=20 and leaves
        # tolerance_above_usd=None -- above MUST fall back to
        # tolerance_usd=100 (the symmetric default), NOT to 0 or
        # to tolerance_below_usd.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 50_050.0,  # broker above by 50
            tolerance_usd=100.0,
            tolerance_pct=10.0,
            tolerance_below_usd=20.0,
            # tolerance_above_usd intentionally omitted
        )
        # Drift = -50, tol_above falls back to tolerance_usd=100, in tol
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.in_tolerance is True
        assert rec.tolerance_above_usd == pytest.approx(100.0)
        assert rec.tolerance_below_usd == pytest.approx(20.0)

    def test_symmetric_tolerance_usd_still_works_unchanged(self):
        # Backwards-compat pin: a caller using ONLY tolerance_usd
        # (the v0.1.65 API) gets identical behaviour. Both directions
        # use the symmetric value.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_950.0,
            tolerance_usd=100.0,
            tolerance_pct=10.0,
        )
        # Drift = +50, tol below = symmetric 100, in tol
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.in_tolerance is True
        assert rec.tolerance_below_usd == pytest.approx(100.0)
        assert rec.tolerance_above_usd == pytest.approx(100.0)

    def test_pct_threshold_split_per_direction(self):
        # Below pct = 0.0001 (0.01%), above pct = 0.01 (1%). Drift =
        # +$50 = 0.1% of $50K. Below pct $50/$50K = 0.001 > 0.0001 trips.
        # Same magnitude on the above side: 0.001 < 0.01 passes.
        # Use very-large USD thresholds so only the pct fires.
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_950.0,
            tolerance_below_usd=1_000_000.0,
            tolerance_below_pct=0.0001,
            tolerance_above_usd=1_000_000.0,
            tolerance_above_pct=0.01,
        )
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "broker_below_logical"
        assert result.in_tolerance is False

        rec2 = BrokerEquityReconciler(
            broker_equity_source=lambda: 50_050.0,
            tolerance_below_usd=1_000_000.0,
            tolerance_below_pct=0.0001,
            tolerance_above_usd=1_000_000.0,
            tolerance_above_pct=0.01,
        )
        result2 = rec2.reconcile(logical_equity_usd=50_000.0)
        assert result2.reason == "within_tolerance"
        assert result2.in_tolerance is True

    def test_negative_per_direction_tolerance_rejected(self):
        with pytest.raises(ValueError, match="tolerance_below_usd"):
            BrokerEquityReconciler(
                broker_equity_source=lambda: None,
                tolerance_below_usd=-1.0,
            )
        with pytest.raises(ValueError, match="tolerance_above_pct"):
            BrokerEquityReconciler(
                broker_equity_source=lambda: None,
                tolerance_above_pct=-0.001,
            )


# ---------------------------------------------------------------------------
# v0.1.66 H3 -- hysteresis clear-band + drift-state machine
# ---------------------------------------------------------------------------


class TestHysteresisClearBand:
    """v0.1.66 H3 -- the latched drift state with a tighter clear band."""

    def test_default_clear_band_is_70pct_of_trigger(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: None,
            tolerance_usd=100.0,
            tolerance_pct=0.01,
        )
        assert rec.clear_tolerance_below_usd == pytest.approx(70.0)
        assert rec.clear_tolerance_below_pct == pytest.approx(0.007)

    def test_custom_clear_band_overrides_default(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: None,
            tolerance_usd=100.0,
            tolerance_pct=0.01,
            clear_tolerance_below_usd=25.0,
            clear_tolerance_below_pct=0.001,
        )
        assert rec.clear_tolerance_below_usd == pytest.approx(25.0)
        assert rec.clear_tolerance_below_pct == pytest.approx(0.001)

    def test_clear_band_wider_than_trigger_rejected(self):
        with pytest.raises(ValueError, match="clear_tolerance_below_usd"):
            BrokerEquityReconciler(
                broker_equity_source=lambda: None,
                tolerance_usd=50.0,
                clear_tolerance_below_usd=100.0,  # > trigger -- invalid
            )

    def test_negative_clear_band_rejected(self):
        with pytest.raises(ValueError, match="clear_tolerance_below_pct"):
            BrokerEquityReconciler(
                broker_equity_source=lambda: None,
                clear_tolerance_below_pct=-0.001,
            )

    def test_entry_into_drift_flips_state_and_emits_transition(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_900.0,  # drift=$100
            tolerance_usd=50.0,
            tolerance_pct=0.01,
        )
        assert rec._in_drift_state is False  # noqa: SLF001
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "broker_below_logical"
        assert result.is_in_drift_state is True
        assert result.transition == "entered_drift"
        assert rec.stats.drift_state_entries == 1

    def test_steady_drift_after_entry_is_stable_transition(self):
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 49_900.0,
            tolerance_usd=50.0,
        )
        first = rec.reconcile(logical_equity_usd=50_000.0)
        assert first.transition == "entered_drift"
        second = rec.reconcile(logical_equity_usd=50_000.0)
        assert second.transition == "stable"
        assert second.is_in_drift_state is True

    def test_drift_inside_clear_band_exits_state(self):
        # Default clear_tolerance_below_usd = 50 * 0.7 = 35.
        # Use a value where drift = 30 (inside clear band) to verify exit.
        broker = [49_900.0]  # drift=$100 (out of trigger)

        def _src():
            return broker[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
            tolerance_pct=10.0,  # huge so only USD path matters
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # entered_drift
        broker[0] = 49_970.0  # drift=$30 -- inside clear band ($35)
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.transition == "exited_drift"
        assert result.is_in_drift_state is False
        assert rec.stats.drift_state_exits == 1

    def test_drift_in_jitter_zone_stays_latched(self):
        # Trigger=$50, clear=$35. Drift bouncing at $40 stays latched.
        broker = [49_900.0]  # drift=$100

        def _src():
            return broker[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
            tolerance_pct=10.0,
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # entered_drift
        # Drift drops to $40 -- still > clear band $35.
        broker[0] = 49_960.0
        result = rec.reconcile(logical_equity_usd=50_000.0)
        # In tolerance per the trigger ($40 < $50) but still latched
        # because it has not crossed the (tighter) clear band.
        assert result.in_tolerance is True
        assert result.is_in_drift_state is True
        assert result.transition == "stable"

    def test_no_broker_data_preserves_drift_state(self):
        """A no_broker_data tick must not clear the latched drift state."""
        broker: list[float | None] = [49_900.0]

        def _src():
            return broker[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # entered_drift
        broker[0] = None
        result = rec.reconcile(logical_equity_usd=50_000.0)
        assert result.reason == "no_broker_data"
        # Drift state survives the blink.
        assert result.is_in_drift_state is True
        assert result.transition == "stable"

    def test_broker_above_logical_clears_drift_latch(self):
        """If broker overshoots logical, we are no longer in cushion-overstated."""
        broker = [49_900.0]  # drift=$100 (broker_below_logical)

        def _src():
            return broker[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # entered_drift
        broker[0] = 50_500.0  # drift=-$500 (broker_above_logical)
        result = rec.reconcile(logical_equity_usd=50_000.0)
        # Crossed past logical. Latch clears -- the dangerous direction
        # is no longer active.
        assert result.transition == "exited_drift"
        assert result.is_in_drift_state is False

    def test_below_logical_at_min_logical_floor_does_not_crash(self):
        """No-broker-data path on a sub-floor logical preserves state."""
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: 0.5,
            tolerance_usd=50.0,
            min_logical_usd=1.0,
        )
        result = rec.reconcile(logical_equity_usd=0.5)
        assert result.reason == "no_broker_data"
        assert result.is_in_drift_state is False
        assert result.transition == "stable"


# ---------------------------------------------------------------------------
# v0.1.67 L2 -- windowed max drift over the last N reconcile ticks
# ---------------------------------------------------------------------------


class TestWindowedMaxDrift:
    """v0.1.67 L2 -- bounded sliding window over drift_abs."""

    def test_default_window_size_is_1000(self):
        rec = BrokerEquityReconciler(broker_equity_source=lambda: None)
        assert rec.drift_window_size == 1000
        assert rec.stats.drift_window_size == 1000
        assert rec.stats.windowed_max_drift_usd_abs == 0.0

    def test_negative_window_size_rejected(self):
        with pytest.raises(ValueError, match="drift_window_size"):
            BrokerEquityReconciler(
                broker_equity_source=lambda: None,
                drift_window_size=-5,
            )

    def test_window_size_zero_disables_windowing(self):
        """drift_window_size=0 means windowed_max stays at 0.0."""
        broker = [49_900.0]  # drift=$100

        def _src():
            return broker[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=50.0,
            drift_window_size=0,
        )
        rec.reconcile(logical_equity_usd=50_000.0)
        # Lifetime max still tracks; window stays disabled.
        assert rec.stats.max_drift_usd_abs == pytest.approx(100.0)
        assert rec.stats.windowed_max_drift_usd_abs == 0.0

    def test_window_grows_then_caps(self):
        """Drift series 100, 200, 50 with window=2: max sees 100,200; 200,50."""
        equities = iter([49_900.0, 49_800.0, 49_950.0])
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: next(equities),
            tolerance_usd=10.0,
            drift_window_size=2,
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=100
        assert rec.stats.windowed_max_drift_usd_abs == pytest.approx(100.0)
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=200
        assert rec.stats.windowed_max_drift_usd_abs == pytest.approx(200.0)
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=50
        # Window now [200, 50] -- the 100 has aged out, max is 200.
        assert rec.stats.windowed_max_drift_usd_abs == pytest.approx(200.0)

    def test_window_eventually_ages_out_old_spike(self):
        """Spike + sustained quiet: windowed max eventually drops to quiet level."""
        equities = iter([49_000.0] + [49_990.0] * 5)  # 1000 spike, then 10x5
        rec = BrokerEquityReconciler(
            broker_equity_source=lambda: next(equities),
            tolerance_usd=10.0,
            drift_window_size=3,
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # 1000
        for _ in range(5):
            rec.reconcile(logical_equity_usd=50_000.0)  # 10
        # Window holds last 3 = [10, 10, 10]; max is 10.
        assert rec.stats.windowed_max_drift_usd_abs == pytest.approx(10.0)
        # Lifetime max still records the original spike.
        assert rec.stats.max_drift_usd_abs == pytest.approx(1000.0)

    def test_no_broker_data_does_not_pollute_window(self):
        """A no_broker_data tick must not append to the window."""
        broker: list[float | None] = [49_900.0]

        def _src():
            return broker[0]

        rec = BrokerEquityReconciler(
            broker_equity_source=_src,
            tolerance_usd=10.0,
            drift_window_size=10,
        )
        rec.reconcile(logical_equity_usd=50_000.0)  # drift=100
        # Source returns None next; that tick should not be windowed.
        broker[0] = None
        rec.reconcile(logical_equity_usd=50_000.0)
        # Window still has just the single $100 entry.
        assert rec.stats.windowed_max_drift_usd_abs == pytest.approx(100.0)
        assert rec.stats.checks_no_data == 1
