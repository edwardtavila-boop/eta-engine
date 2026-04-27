"""
EVOLUTIONARY TRADING ALGO  //  core.risk_profile
=====================================
User-selectable aggressiveness presets that drive every position-sizing
and circuit-breaker knob from a single choice. Three curated profiles —
``conservative``, ``balanced``, ``aggressive`` — designed to be the only
risk dial a user ever touches.

Why this lives separately from ``RiskTier`` in ``risk_engine.py``
----------------------------------------------------------------
``RiskTier`` (FUTURES / SEED / CASINO) describes *where the capital came
from*: regulated futures account, prop-funded eval, or degen self-funded.
That dimension constrains *what's legally and operationally allowed* on
the venue. A user can't move from CASINO to FUTURES by clicking a button.

``RiskProfile`` is the orthogonal user-facing knob: given the bucket
they're in, how aggressive should the bot be inside that bucket. A
conservative user on FUTURES gets a smaller position than an aggressive
user on FUTURES; both are still bound by the FUTURES tier's exchange and
margin rules. The two abstractions multiply, not replace each other.

Calibration philosophy
----------------------
Each profile coheres internally — every knob moves in the same
direction. Bumping ``risk_per_trade_pct`` without also bumping the
daily-loss cap creates a profile that takes 10 trades before a
circuit-breaker fires; bumping the daily cap without the per-trade risk
creates a profile that "feels safe" until one bad sequence hits the
ceiling. Profiles avoid that by tuning the whole stack together.

The numerical values come from walk-forward back-testing on MNQ — the
``balanced`` profile reproduces the published track-record assumptions
(1% per trade, 3% daily cap, 1.5×ATR stops). ``conservative`` is half
the risk, ``aggressive`` is double — both then have their other knobs
scaled to match. None of these are a guarantee; back-tested figures are
hypothetical and past performance does not guarantee future results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProfileName = Literal["conservative", "balanced", "aggressive"]


@dataclass(frozen=True)
class RiskProfile:
    """A coherent set of aggressiveness knobs.

    All fields are ratios (0.0–1.0) or counts. No raw dollar amounts —
    those are derived per-user from current equity at strategy boot.

    Attributes
    ----------
    name:
        Stable machine identifier. One of ``conservative`` /
        ``balanced`` / ``aggressive``. Used for config selection,
        URL params, and serialization.
    label:
        Short human-readable label suitable for a dashboard chip.
    description:
        One-sentence explanation of the trade-off. Surfaces in the
        profile picker and in the journal entry for every trade so the
        operator can later answer "what was the user's setting on this
        date?"
    risk_per_trade_pct:
        Fraction of equity put at risk on a single entry's stop. The
        bot's position sizing solves
        ``contracts = (equity * risk_per_trade_pct) / (atr * stop_atr_multiple)``.
        Hard ceiling enforced by ``risk_engine.dynamic_position_size`` is
        10 %; profiles stay well under that.
    max_concurrent_positions:
        Maximum number of open positions at any moment. Includes legs
        of bracket orders for the position-count purpose. ``1`` keeps
        the bot single-position so a flash event can only blow up once.
    stop_atr_multiple:
        Stop distance expressed as multiples of 14-period ATR. Tighter
        stops (smaller multiple) = smaller per-trade dollar risk for the
        same number of contracts, but a higher chance of being stopped
        out on noise. Wider stops = inverse.
    daily_loss_cap_pct:
        Fraction of equity at which the daily-loss circuit breaker
        flattens everything and pauses entries until the next session.
        The bot will refuse a new entry if taking it could push intraday
        PnL past this line.
    trailing_dd_halt_pct:
        Fraction of equity-from-peak at which the trailing-drawdown
        latch halts the bot until the operator manually re-arms.
        Independent of the daily cap — the daily cap is a session
        circuit breaker; this is a campaign-level latch.
    min_confluence_score:
        Lower bound on the 8-axis confluence score (0–8) for a signal
        to be acted on. Conservative profiles only trade the highest-
        confidence setups; aggressive profiles take more borderline
        signals.
    consecutive_loss_pause:
        Number of consecutive losing trades before the bot pauses for
        the rest of the session. Lower = quicker to step away from a
        losing streak; higher = more willing to ride through chop.
    recommended_min_capital_usd:
        Minimum account size at which this profile's worst-case daily
        loss leaves enough cushion to keep going. Below this, a single
        bad day eats too large a share of the account for the profile's
        circuit breakers to do their job. Surfaced to the user in the
        portal as "recommended operational capital."
    recommended_capital_note:
        One-sentence reasoning for the floor. Renders next to the
        recommendation so the user can see the math.
    """

    name: ProfileName
    label: str
    description: str

    # Position sizing
    risk_per_trade_pct: float
    max_concurrent_positions: int
    stop_atr_multiple: float

    # Circuit breakers
    daily_loss_cap_pct: float
    trailing_dd_halt_pct: float
    consecutive_loss_pause: int

    # Signal gating
    min_confluence_score: int

    # Capital recommendation
    recommended_min_capital_usd: float
    recommended_capital_note: str

    # Convenience for serialization (e.g., journaled trade records).
    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_concurrent_positions": self.max_concurrent_positions,
            "stop_atr_multiple": self.stop_atr_multiple,
            "daily_loss_cap_pct": self.daily_loss_cap_pct,
            "trailing_dd_halt_pct": self.trailing_dd_halt_pct,
            "consecutive_loss_pause": self.consecutive_loss_pause,
            "min_confluence_score": self.min_confluence_score,
            "recommended_min_capital_usd": self.recommended_min_capital_usd,
            "recommended_capital_note": self.recommended_capital_note,
        }


# ---------------------------------------------------------------------------
# Preset profiles
# ---------------------------------------------------------------------------
#
# Calibrated against MNQ walk-forward back-tests. Each profile triples-down
# on its philosophy: risk-per-trade, daily-cap, drawdown-halt, signal-gate
# all move together so a "conservative" run is conservative everywhere.
#
# Worst-case-day math (back-of-envelope, NOT a guarantee):
#   Conservative: hits daily cap at -1.5% of equity → -$37.50 on $2,500
#                  account; trailing DD halt at -3% from peak → -$75
#   Balanced:     -3% daily cap → -$150 on $5k; -5% trailing DD → -$250
#   Aggressive:   -5% daily cap → -$500 on $10k; -8% trailing DD → -$800
#
# These are deliberately set so that even the aggressive profile cannot
# wipe out a properly-capitalized account in a single bad day. The
# ``recommended_min_capital_usd`` floor is what makes that math hold.

CONSERVATIVE: RiskProfile = RiskProfile(
    name="conservative",
    label="Low — capital preservation",
    description=(
        "Smallest position size, tightest stops, only the highest-"
        "confluence setups. Designed for users who care more about not "
        "blowing up than about catching every trade."
    ),
    risk_per_trade_pct=0.005,  # 0.5 % of equity per entry
    max_concurrent_positions=1,
    stop_atr_multiple=1.0,
    daily_loss_cap_pct=0.015,  # -1.5 % halts the day
    trailing_dd_halt_pct=0.03,  # -3 % from peak halts the campaign
    consecutive_loss_pause=2,
    min_confluence_score=7,  # only 7+ / 8 setups
    recommended_min_capital_usd=2_500.0,
    recommended_capital_note=(
        "At this floor, a worst-case day (-1.5%) is $37.50 — small "
        "enough that the bot's circuit breakers have meaningful room "
        "to work before any single session erodes the account."
    ),
)

BALANCED: RiskProfile = RiskProfile(
    name="balanced",
    label="Medium — published methodology default",
    description=(
        "Reproduces the published methodology's risk parameters. "
        "1 % per trade, 1.5×ATR stops, takes the full set of validated "
        "signals. The profile the back-tests on /track-record assume."
    ),
    risk_per_trade_pct=0.010,  # 1 % of equity per entry
    max_concurrent_positions=2,
    stop_atr_multiple=1.5,
    daily_loss_cap_pct=0.030,  # -3 % halts the day
    trailing_dd_halt_pct=0.05,  # -5 % from peak halts the campaign
    consecutive_loss_pause=3,
    min_confluence_score=6,  # 6+ / 8 setups
    recommended_min_capital_usd=5_000.0,
    recommended_capital_note=(
        "Matches the back-tested track record's assumed account size. "
        "A worst-case day (-3%) is $150; the trailing-DD halt at -5% "
        "from peak is $250. Below this floor the per-trade dollar risk "
        "starts to look small enough that broker fees dominate."
    ),
)

AGGRESSIVE: RiskProfile = RiskProfile(
    name="aggressive",
    label="High — degen, eyes open",
    description=(
        "2 % per trade, wider stops, willing to take borderline "
        "signals. Higher expected return AND higher worst-case "
        "drawdown — both directions amplified. Not for accounts "
        "the user can't afford to see -8 % from peak."
    ),
    risk_per_trade_pct=0.020,  # 2 % of equity per entry
    max_concurrent_positions=3,
    stop_atr_multiple=2.0,
    daily_loss_cap_pct=0.050,  # -5 % halts the day
    trailing_dd_halt_pct=0.08,  # -8 % from peak halts the campaign
    consecutive_loss_pause=4,
    min_confluence_score=5,  # 5+ / 8 setups
    recommended_min_capital_usd=10_000.0,
    recommended_capital_note=(
        "At this floor, a worst-case day (-5%) is $500 and the "
        "trailing-DD halt at -8% from peak is $800. The aggressive "
        "profile compounds harder during good runs but also draws "
        "down harder during bad ones — the larger account is what "
        "lets the bot survive a normal-bad week without halting on "
        "noise."
    ),
)


# ---------------------------------------------------------------------------
# Registry + lookup
# ---------------------------------------------------------------------------

PROFILES: dict[ProfileName, RiskProfile] = {
    "conservative": CONSERVATIVE,
    "balanced": BALANCED,
    "aggressive": AGGRESSIVE,
}

#: Default profile when none is specified. The published methodology and
#: every back-test on /track-record assumes this profile.
DEFAULT_PROFILE: RiskProfile = BALANCED


def get_profile(name: str | ProfileName) -> RiskProfile:
    """Look up a profile by name. Case-insensitive, whitespace-tolerant.

    Raises :class:`ValueError` (not :class:`KeyError`) on unknown names
    so callers can surface a clean error to the user without having to
    catch two exception types.
    """
    key = (name or "").strip().lower()
    if key not in PROFILES:
        valid = ", ".join(PROFILES)
        raise ValueError(f"unknown risk profile {name!r}; expected one of: {valid}")
    return PROFILES[key]  # type: ignore[index]


def list_profiles() -> list[RiskProfile]:
    """Return the three profiles in canonical conservative→aggressive order.

    Stable ordering matters for the dashboard picker — the user expects
    the slider to go conservative on the left and aggressive on the
    right, not in dict-insertion order.
    """
    return [CONSERVATIVE, BALANCED, AGGRESSIVE]
