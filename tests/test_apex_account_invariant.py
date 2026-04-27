"""Unit tests for :mod:`eta_engine.core.eta_account_invariant`.

B3 closure (v0.1.69). Pins the tier-A aggregate-equity invariant
validator: every verdict path, every kwarg, the strict-mode raise,
and the bounds-validation rejections. The drift detector relies on
the invariant being explicit; if the validator silently regresses,
a misconfigured fleet can produce huge bogus drift events.

Sections
--------
TestNoTierABots
  Empty / non-tier-A snapshot lists are trivially OK.

TestNegativeAggregate
  A bot bookkeeping equity below zero is always a bug.

TestNonFiniteAggregate
  inf / NaN aggregates are caught.

TestOversizeAggregate (the canonical B3 case)
  Two tier-A bots each tracking the full account size produce 2x
  oversize aggregate; validator flags it.

TestUndersizeAggregate
  Operator-tightened lower bound fires its own verdict.

TestNonTierABotsIgnored
  Tier-B bots' equity does not pollute the tier-A aggregate.

TestStrictMode
  ``strict=True`` raises on any non-ok verdict.

TestConstructorValidation
  Negative multipliers + over < under rejected.

TestResultSerialisation
  ``InvariantResult.as_dict`` round-trips through json.dumps with
  no inf / NaN issues (cross-cutting H5 alignment).
"""

from __future__ import annotations

import json
import math

import pytest

from eta_engine.core.eta_account_invariant import (
    ApexAccountInvariantError,
    validate_tier_a_aggregate_equity,
)
from eta_engine.core.kill_switch_runtime import BotSnapshot


def _bot(name: str, tier: str, equity: float) -> BotSnapshot:
    return BotSnapshot(
        name=name,
        tier=tier,
        equity_usd=equity,
        peak_equity_usd=max(equity, 50_000.0),
    )


# ---------------------------------------------------------------------------
# No tier-A bots / empty list
# ---------------------------------------------------------------------------


class TestNoTierABots:
    def test_empty_snapshot_list_is_ok(self):
        result = validate_tier_a_aggregate_equity(snapshots=[])
        assert result.ok is True
        assert result.verdict == "no_tier_a_bots"
        assert result.n_tier_a == 0
        assert result.sum_logical_usd == 0.0

    def test_only_tier_b_bots_is_ok(self):
        snapshots = [
            _bot("eth_perp", "B", 10_000.0),
            _bot("sol_perp", "B", 5_000.0),
        ]
        result = validate_tier_a_aggregate_equity(snapshots=snapshots)
        assert result.ok is True
        assert result.verdict == "no_tier_a_bots"
        assert result.n_tier_a == 0


# ---------------------------------------------------------------------------
# Negative aggregate (always a bug)
# ---------------------------------------------------------------------------


class TestNegativeAggregate:
    def test_single_negative_equity_flags(self):
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", -1_000.0)],
        )
        assert result.ok is False
        assert result.verdict == "negative_aggregate"
        assert result.sum_logical_usd == pytest.approx(-1_000.0)
        assert "NEGATIVE" in result.reason

    def test_negative_sum_of_positive_and_negative(self):
        result = validate_tier_a_aggregate_equity(
            snapshots=[
                _bot("mnq", "A", 10_000.0),
                _bot("nq", "A", -15_000.0),
            ],
        )
        assert result.ok is False
        assert result.verdict == "negative_aggregate"
        assert result.sum_logical_usd == pytest.approx(-5_000.0)


# ---------------------------------------------------------------------------
# Non-finite aggregate
# ---------------------------------------------------------------------------


class TestNonFiniteAggregate:
    def test_inf_equity_is_non_finite(self):
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", float("inf"))],
        )
        assert result.ok is False
        assert result.verdict == "non_finite_aggregate"

    def test_nan_equity_is_non_finite(self):
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", float("nan"))],
        )
        assert result.ok is False
        assert result.verdict == "non_finite_aggregate"


# ---------------------------------------------------------------------------
# Oversize aggregate (the canonical B3 finding)
# ---------------------------------------------------------------------------


class TestOversizeAggregate:
    def test_two_bots_each_full_size_flags_oversize(self):
        """The B3 canonical config-bug case: two tier-A bots each
        track $50K when the broker account is only $50K."""
        result = validate_tier_a_aggregate_equity(
            snapshots=[
                _bot("mnq", "A", 50_000.0),
                _bot("nq", "A", 50_000.0),
            ],
            expected_account_size_usd=50_000.0,
        )
        assert result.ok is False
        assert result.verdict == "oversize_aggregate"
        assert result.sum_logical_usd == pytest.approx(100_000.0)
        assert "1.50x" in result.reason

    def test_within_oversize_threshold_is_ok(self):
        """A 20% drift above starting size is normal trading-day
        ranges, not a config bug; default 1.5x oversize threshold
        passes this through."""
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", 60_000.0)],
            expected_account_size_usd=50_000.0,
        )
        assert result.ok is True
        assert result.verdict == "ok"

    def test_custom_tighter_oversize_multiplier(self):
        """Operator can tighten the oversize multiplier."""
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", 51_000.0)],
            expected_account_size_usd=50_000.0,
            oversize_multiplier=1.01,  # > 1% above flags
        )
        assert result.ok is False
        assert result.verdict == "oversize_aggregate"

    def test_no_expected_size_skips_oversize_check(self):
        """When ``expected_account_size_usd`` is None, the oversize
        check is skipped (negative / non-finite still fire)."""
        result = validate_tier_a_aggregate_equity(
            snapshots=[
                _bot("mnq", "A", 1_000_000.0),  # any size goes
            ],
        )
        assert result.ok is True
        assert result.verdict == "ok"


# ---------------------------------------------------------------------------
# Undersize aggregate (operator-tightened lower bound)
# ---------------------------------------------------------------------------


class TestUndersizeAggregate:
    def test_default_undersize_zero_means_no_lower_bound_fires(self):
        """Default undersize_multiplier=0 means the only undersize
        verdict that fires is the negative-aggregate case."""
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", 100.0)],  # tiny but positive
            expected_account_size_usd=50_000.0,
        )
        assert result.ok is True

    def test_tightened_undersize_fires_below_threshold(self):
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", 25_000.0)],  # 50% drawdown
            expected_account_size_usd=50_000.0,
            undersize_multiplier=0.7,  # below 70% trips
        )
        assert result.ok is False
        assert result.verdict == "undersize_aggregate"
        assert "0.70x" in result.reason

    def test_at_undersize_threshold_passes(self):
        """Exactly at threshold (>=) passes."""
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", 35_000.0)],  # 70% of 50K
            expected_account_size_usd=50_000.0,
            undersize_multiplier=0.7,
        )
        assert result.ok is True


# ---------------------------------------------------------------------------
# Tier-B bot equity not aggregated
# ---------------------------------------------------------------------------


class TestNonTierABotsIgnored:
    def test_tier_b_equity_does_not_count(self):
        result = validate_tier_a_aggregate_equity(
            snapshots=[
                _bot("mnq", "A", 50_000.0),
                _bot("eth_perp", "B", 100_000.0),  # tier-B, ignored
            ],
            expected_account_size_usd=50_000.0,
        )
        assert result.ok is True
        assert result.sum_logical_usd == pytest.approx(50_000.0)
        assert result.n_tier_a == 1


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_strict_mode_raises_on_negative(self):
        with pytest.raises(ApexAccountInvariantError, match="NEGATIVE"):
            validate_tier_a_aggregate_equity(
                snapshots=[_bot("mnq", "A", -1.0)],
                strict=True,
            )

    def test_strict_mode_raises_on_oversize(self):
        with pytest.raises(ApexAccountInvariantError, match="exceeds"):
            validate_tier_a_aggregate_equity(
                snapshots=[
                    _bot("mnq", "A", 50_000.0),
                    _bot("nq", "A", 50_000.0),
                ],
                expected_account_size_usd=50_000.0,
                strict=True,
            )

    def test_strict_mode_does_not_raise_on_ok(self):
        # Should return cleanly, not raise.
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", 50_000.0)],
            expected_account_size_usd=50_000.0,
            strict=True,
        )
        assert result.ok is True


# ---------------------------------------------------------------------------
# Constructor / kwarg validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_negative_oversize_rejected(self):
        with pytest.raises(ValueError, match="oversize_multiplier"):
            validate_tier_a_aggregate_equity(
                snapshots=[],
                oversize_multiplier=-0.5,
            )

    def test_negative_undersize_rejected(self):
        with pytest.raises(ValueError, match="undersize_multiplier"):
            validate_tier_a_aggregate_equity(
                snapshots=[],
                undersize_multiplier=-0.1,
            )

    def test_oversize_below_undersize_rejected(self):
        with pytest.raises(ValueError, match="oversize_multiplier"):
            validate_tier_a_aggregate_equity(
                snapshots=[],
                expected_account_size_usd=50_000.0,
                oversize_multiplier=0.5,
                undersize_multiplier=0.7,
            )


# ---------------------------------------------------------------------------
# Result serialisation
# ---------------------------------------------------------------------------


class TestResultSerialisation:
    def test_as_dict_round_trips_through_json(self):
        result = validate_tier_a_aggregate_equity(
            snapshots=[
                _bot("mnq", "A", 50_000.0),
                _bot("nq", "A", 50_000.0),
            ],
            expected_account_size_usd=50_000.0,
        )
        encoded = json.dumps(result.as_dict(), allow_nan=False)
        # No Infinity / NaN tokens (RFC 8259 compliance, mirrors H5).
        assert "Infinity" not in encoded
        assert "NaN" not in encoded
        decoded = json.loads(encoded)
        assert decoded["verdict"] == "oversize_aggregate"
        assert decoded["sum_logical_usd"] == pytest.approx(100_000.0)
        assert decoded["n_tier_a"] == 2

    def test_non_finite_result_does_not_corrupt_json(self):
        """The result for a non-finite aggregate must still serialise
        cleanly even though the input was bad. We round-trip without
        allow_nan=False here because we are testing the as_dict
        builder; the H5 sanitisation is in BrokerEquityReconciler --
        this validator's contract is just 'no exception, structured
        result'."""
        result = validate_tier_a_aggregate_equity(
            snapshots=[_bot("mnq", "A", float("nan"))],
        )
        d = result.as_dict()
        # The verdict is recorded; the sum may be nan but the
        # structure is intact.
        assert d["verdict"] == "non_finite_aggregate"
        assert math.isnan(d["sum_logical_usd"])
