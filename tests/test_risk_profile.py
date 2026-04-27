"""
EVOLUTIONARY TRADING ALGO  //  tests.test_risk_profile
==========================================
Unit tests for ``core.risk_profile``. The risk profile is what the
private-portal user picks; if its values drift unexpectedly, every
position size and circuit breaker downstream drifts with it. These
tests pin the contract.

Three classes of test:

1. **Schema sanity** — every profile has every field, types are right.
2. **Internal consistency** — each profile's knobs cohere (more
   aggressive ⇒ all-knobs-in-the-same-direction; never one knob more
   conservative than the same-position knob in the lower tier).
3. **Registry behavior** — lookup, ordering, error paths.
"""

from __future__ import annotations

import pytest

from eta_engine.core.risk_profile import (
    AGGRESSIVE,
    BALANCED,
    CONSERVATIVE,
    DEFAULT_PROFILE,
    PROFILES,
    RiskProfile,
    get_profile,
    list_profiles,
)

# ---------------------------------------------------------------------------
# 1. Schema sanity
# ---------------------------------------------------------------------------


class TestProfileSchema:
    """Every profile is fully populated and its values are in valid ranges."""

    @pytest.mark.parametrize("p", [CONSERVATIVE, BALANCED, AGGRESSIVE])
    def test_required_fields_populated(self, p: RiskProfile) -> None:
        assert p.name in ("conservative", "balanced", "aggressive")
        assert p.label
        assert p.description
        assert p.recommended_capital_note

    @pytest.mark.parametrize("p", [CONSERVATIVE, BALANCED, AGGRESSIVE])
    def test_fractions_within_legal_bounds(self, p: RiskProfile) -> None:
        # risk_per_trade hard ceiling is 10% (enforced by
        # risk_engine.dynamic_position_size). Anything ≥ 5% would be
        # flagged in code review even if it's technically allowed.
        assert 0 < p.risk_per_trade_pct <= 0.05, (
            f"{p.name}: risk_per_trade_pct={p.risk_per_trade_pct} outside (0, 0.05]"
        )
        assert 0 < p.daily_loss_cap_pct <= 0.10
        assert 0 < p.trailing_dd_halt_pct <= 0.20

    @pytest.mark.parametrize("p", [CONSERVATIVE, BALANCED, AGGRESSIVE])
    def test_counts_are_positive_integers(self, p: RiskProfile) -> None:
        assert isinstance(p.max_concurrent_positions, int)
        assert p.max_concurrent_positions >= 1
        assert isinstance(p.consecutive_loss_pause, int)
        assert p.consecutive_loss_pause >= 1

    @pytest.mark.parametrize("p", [CONSERVATIVE, BALANCED, AGGRESSIVE])
    def test_confluence_score_is_zero_to_eight(self, p: RiskProfile) -> None:
        # The 8-axis confluence scorer (core/confluence_scorer.py)
        # returns 0–8; profile gates must be in that range.
        assert 0 <= p.min_confluence_score <= 8

    @pytest.mark.parametrize("p", [CONSERVATIVE, BALANCED, AGGRESSIVE])
    def test_capital_floor_above_zero(self, p: RiskProfile) -> None:
        # No "free" tier here — even conservative requires real capital
        # for the math to hold.
        assert p.recommended_min_capital_usd > 0

    def test_profile_is_frozen_dataclass(self) -> None:
        """Mutating a profile in-flight is a category of bug we want to
        prevent — once a user picks a profile, the values are locked
        for that strategy run. ``@dataclass(frozen=True)`` enforces it."""
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            CONSERVATIVE.risk_per_trade_pct = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Internal consistency — each knob moves the same direction across the
#    three profiles. Catches the "tweaked one knob without the others" bug.
# ---------------------------------------------------------------------------


class TestProfileMonotonicity:
    """Conservative < Balanced < Aggressive on every aggressiveness axis."""

    def test_risk_per_trade_increases(self) -> None:
        assert CONSERVATIVE.risk_per_trade_pct < BALANCED.risk_per_trade_pct < AGGRESSIVE.risk_per_trade_pct

    def test_daily_loss_cap_relaxes(self) -> None:
        # More aggressive ⇒ allows bigger daily loss before halting.
        assert CONSERVATIVE.daily_loss_cap_pct < BALANCED.daily_loss_cap_pct < AGGRESSIVE.daily_loss_cap_pct

    def test_trailing_dd_halt_relaxes(self) -> None:
        assert CONSERVATIVE.trailing_dd_halt_pct < BALANCED.trailing_dd_halt_pct < AGGRESSIVE.trailing_dd_halt_pct

    def test_max_positions_increases(self) -> None:
        assert (
            CONSERVATIVE.max_concurrent_positions
            <= BALANCED.max_concurrent_positions
            <= AGGRESSIVE.max_concurrent_positions
        )

    def test_consecutive_loss_pause_relaxes(self) -> None:
        # More aggressive ⇒ willing to ride through a longer losing
        # streak before stepping away.
        assert (
            CONSERVATIVE.consecutive_loss_pause <= BALANCED.consecutive_loss_pause <= AGGRESSIVE.consecutive_loss_pause
        )

    def test_confluence_threshold_loosens(self) -> None:
        # Conservative requires 7+/8 confluence; aggressive accepts 5+/8.
        assert CONSERVATIVE.min_confluence_score > BALANCED.min_confluence_score > AGGRESSIVE.min_confluence_score

    def test_capital_floor_increases_with_aggression(self) -> None:
        # Aggressive needs MORE capital, not less — the larger drawdowns
        # need a larger base to absorb without halting on noise.
        assert (
            CONSERVATIVE.recommended_min_capital_usd
            < BALANCED.recommended_min_capital_usd
            < AGGRESSIVE.recommended_min_capital_usd
        )

    def test_stop_atr_multiple_increases(self) -> None:
        # Wider stops on the aggressive end; tighter stops conservative.
        # This is the one knob whose direction is debatable, but our
        # rationale: aggressive profiles take more borderline signals,
        # which need more room to work.
        assert CONSERVATIVE.stop_atr_multiple < BALANCED.stop_atr_multiple < AGGRESSIVE.stop_atr_multiple


# ---------------------------------------------------------------------------
# 3. Registry + lookup
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_three_canonical_profiles_registered(self) -> None:
        assert set(PROFILES.keys()) == {"conservative", "balanced", "aggressive"}

    def test_default_is_balanced(self) -> None:
        # The published methodology / track-record uses balanced.
        # Changing the default is a customer-facing change — pin it.
        assert DEFAULT_PROFILE is BALANCED

    def test_get_profile_canonical_names(self) -> None:
        assert get_profile("conservative") is CONSERVATIVE
        assert get_profile("balanced") is BALANCED
        assert get_profile("aggressive") is AGGRESSIVE

    def test_get_profile_is_case_insensitive_and_trims(self) -> None:
        assert get_profile("Balanced") is BALANCED
        assert get_profile("  AGGRESSIVE  ") is AGGRESSIVE

    def test_get_profile_unknown_raises_value_error(self) -> None:
        # ValueError, not KeyError, so callers can surface a clean
        # error message via str(e) without re-wrapping.
        with pytest.raises(ValueError, match="unknown risk profile"):
            get_profile("yolo")
        # Empty / whitespace also unknown
        with pytest.raises(ValueError):
            get_profile("")
        with pytest.raises(ValueError):
            get_profile("   ")

    def test_list_profiles_returns_canonical_order(self) -> None:
        # The dashboard picker renders left-to-right in this order;
        # if anything reshuffles to dict-insertion order or alpha,
        # we want a loud failure.
        profiles = list_profiles()
        assert [p.name for p in profiles] == ["conservative", "balanced", "aggressive"]

    def test_as_dict_round_trips_every_field(self) -> None:
        # Profiles end up in the journal entry of every trade so the
        # operator can ask "what was the user's setting on this date?"
        # ``as_dict`` is the canonical serialization.
        d = BALANCED.as_dict()
        assert d["name"] == "balanced"
        assert d["risk_per_trade_pct"] == BALANCED.risk_per_trade_pct
        assert d["recommended_min_capital_usd"] == BALANCED.recommended_min_capital_usd
        # All public fields covered
        expected_keys = {
            "name",
            "label",
            "description",
            "risk_per_trade_pct",
            "max_concurrent_positions",
            "stop_atr_multiple",
            "daily_loss_cap_pct",
            "trailing_dd_halt_pct",
            "consecutive_loss_pause",
            "min_confluence_score",
            "recommended_min_capital_usd",
            "recommended_capital_note",
        }
        assert set(d.keys()) == expected_keys
