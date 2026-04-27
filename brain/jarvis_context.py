"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_context
=======================================
Continuous macro + risk context loop -- the "Financial Jarvis".

Why this exists
---------------
Between decisions you're alone. Jarvis is the always-on ambient observer:
regime, VIX, calendar, equity, open risk, override rate -- compiled into
one snapshot on demand (or on a schedule), delivered to operator and
to the command center.

Forces the "never on autopilot" principle to be real by surfacing:
  * did the regime flip while you were AFK?
  * is an FOMC print in 30 minutes?
  * is daily drawdown > 2% already?
  * has the override count in the last 24h doubled?

Design (v2 -- 2026-04-17 upgrade)
---------------------------------
Layer 1 (v1, unchanged):
  * Provider protocols (MacroProvider/EquityProvider/RegimeProvider/JournalProvider).
  * Pure ``build_snapshot(...)`` -> ``JarvisContext``.
  * First-match ``suggest_action(...)`` -> one of
    TRADE / STAND_ASIDE / REDUCE / REVIEW / KILL.

Layer 2 (v2 additions, all additive / optional):
  * ``compute_stress_score`` -- multi-factor weighted stress [0..1]
    with explicit components and a binding_constraint label.
  * ``compute_session_phase`` -- maps ts -> NYSE session phase enum.
  * ``compute_sizing_hint``   -- inverse of stress with session penalties.
  * ``detect_alerts``         -- emits INFO/WARN/CRITICAL alerts for
                                 approaching thresholds (i.e. before the
                                 rigid tier fires).
  * ``compute_margins``       -- "how far am I from each action tier?"
                                 in native units (pct of equity, R, etc.).
  * ``build_playbook``        -- concrete next-step actions per action.
  * ``build_explanation``     -- natural-language one-paragraph summary.
  * ``JarvisMemory``          -- bounded ring buffer of snapshots with
                                 trajectory analysis (improving / flat /
                                 worsening for dd and stress).
  * ``JarvisContextEngine``   -- builder + memory, produces a fully
                                 enriched JarvisContext per tick.

Stdlib + pydantic only.

Public API
----------
v1:
  * ``JarvisContext``         -- the snapshot (pydantic)
  * ``JarvisSuggestion``      -- suggested action + reason
  * ``ActionSuggestion``      -- enum
  * ``MacroSnapshot`` / ``EquitySnapshot`` / ``RegimeSnapshot`` /
    ``JournalSnapshot``
  * ``build_snapshot()``      -- snapshot builder (now also fills v2 fields)
  * ``suggest_action()``      -- first-match heuristic policy
  * ``JarvisContextBuilder``  -- convenience class that wires providers

v2:
  * ``AlertLevel`` / ``JarvisAlert`` / ``StressComponent`` /
    ``StressScore`` / ``SessionPhase`` / ``SizingHint`` /
    ``TrajectoryState`` / ``Trajectory`` / ``JarvisMargins``
  * ``compute_stress_score`` / ``compute_session_phase`` /
    ``compute_sizing_hint`` / ``detect_alerts`` /
    ``compute_margins`` / ``build_playbook`` /
    ``build_explanation``
  * ``JarvisMemory`` / ``JarvisContextEngine``
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime  # noqa: TC003  -- pydantic needs runtime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from eta_engine.core.market_quality import (
    build_market_context_summary,
    format_market_context_summary,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Types (v1)
# ---------------------------------------------------------------------------


class ActionSuggestion(StrEnum):
    TRADE = "TRADE"
    STAND_ASIDE = "STAND_ASIDE"
    REDUCE = "REDUCE"
    REVIEW = "REVIEW"
    KILL = "KILL"


class MacroSnapshot(BaseModel):
    vix_level: float | None = Field(default=None, ge=0.0)
    next_event_label: str | None = Field(
        default=None,
        description="e.g. 'FOMC 2026-05-01 14:00 ET'",
    )
    hours_until_next_event: float | None = Field(default=None, ge=0.0)
    macro_bias: str = Field(
        default="neutral",
        description="hawkish / dovish / neutral / crisis",
    )


class EquitySnapshot(BaseModel):
    account_equity: float = Field(ge=0.0)
    daily_pnl: float
    daily_drawdown_pct: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of intraday high equity bled off. 0.02 = 2%.",
    )
    open_positions: int = Field(ge=0)
    open_risk_r: float = Field(
        ge=0.0,
        description="Total R at risk across all open positions.",
    )


class RegimeSnapshot(BaseModel):
    regime: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    previous_regime: str | None = Field(default=None)
    flipped_recently: bool = Field(
        default=False,
        description="Has regime changed in the last hour?",
    )


class JournalSnapshot(BaseModel):
    kill_switch_active: bool = False
    autopilot_mode: str = Field(
        default="ACTIVE",
        description="ACTIVE / PAUSED / REQUIRE_ACK",
    )
    overrides_last_24h: int = Field(ge=0, default=0)
    blocked_last_24h: int = Field(ge=0, default=0)
    executed_last_24h: int = Field(ge=0, default=0)
    correlations_alert: bool = False


class JarvisSuggestion(BaseModel):
    action: ActionSuggestion
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Types (v2)
# ---------------------------------------------------------------------------


class AlertLevel(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class JarvisAlert(BaseModel):
    level: AlertLevel
    code: str = Field(
        min_length=1,
        description="Stable machine-readable code, e.g. 'dd_approaching_reduce'.",
    )
    message: str = Field(min_length=1)
    severity: float = Field(
        ge=0.0,
        le=1.0,
        description="0 = gentle nudge, 1 = immediate action required.",
    )


class StressComponent(BaseModel):
    name: str = Field(min_length=1)
    value: float = Field(ge=0.0, le=1.0, description="Raw 0..1 stress for this factor.")
    weight: float = Field(ge=0.0, description="Fractional contribution weight.")
    note: str = ""

    @property
    def contribution(self) -> float:
        """weight * value -- how much this component pushes up composite."""
        return self.weight * self.value


class StressScore(BaseModel):
    composite: float = Field(
        ge=0.0,
        le=1.0,
        description="Weighted aggregate [0,1] -- 0 = all clear, 1 = maximal stress.",
    )
    components: list[StressComponent]
    binding_constraint: str = Field(
        min_length=1,
        description="Name of the component with the highest contribution.",
    )


class SessionPhase(StrEnum):
    OVERNIGHT = "OVERNIGHT"  # 16:00 -> 04:00 ET (globex chop)
    PREMARKET = "PREMARKET"  # 04:00 -> 09:30 ET
    OPEN_DRIVE = "OPEN_DRIVE"  # 09:30 -> 10:30 ET (1st hour, high vol)
    MORNING = "MORNING"  # 10:30 -> 12:00 ET
    LUNCH = "LUNCH"  # 12:00 -> 13:30 ET (chop)
    AFTERNOON = "AFTERNOON"  # 13:30 -> 15:30 ET
    CLOSE = "CLOSE"  # 15:30 -> 16:00 ET (MOC)


class SizingHint(BaseModel):
    size_mult: float = Field(
        ge=0.0,
        le=1.0,
        description="Suggested multiplier vs baseline risk. 1.0 = full size.",
    )
    reason: str = Field(min_length=1)
    kelly_cap: float | None = Field(default=None, ge=0.0, le=1.0)


class TrajectoryState(StrEnum):
    IMPROVING = "IMPROVING"
    FLAT = "FLAT"
    WORSENING = "WORSENING"
    UNKNOWN = "UNKNOWN"


class Trajectory(BaseModel):
    dd: TrajectoryState = TrajectoryState.UNKNOWN
    stress: TrajectoryState = TrajectoryState.UNKNOWN
    overrides_velocity_per_24h: float = Field(
        default=0.0,
        description="Rolling rate of new overrides (per 24h).",
    )
    samples: int = Field(ge=0, default=0)
    window_seconds: float = Field(ge=0.0, default=0.0)


class JarvisMargins(BaseModel):
    """How much room you have before each action tier fires.

    All values in the native unit of the constraint. Positive = headroom,
    zero or negative = already breached.
    """

    dd_to_reduce: float = Field(
        description="DD_REDUCE_THRESHOLD - equity.daily_drawdown_pct",
    )
    dd_to_stand_aside: float = Field(
        description="DD_STAND_ASIDE_THRESHOLD - equity.daily_drawdown_pct",
    )
    dd_to_kill: float = Field(
        description="DD_KILL_THRESHOLD - equity.daily_drawdown_pct",
    )
    overrides_to_review: int = Field(
        description="OVERRIDE_REVIEW_THRESHOLD - journal.overrides_last_24h",
    )
    open_risk_to_cap_r: float = Field(
        description="OPEN_RISK_HARD_CAP_R - equity.open_risk_r",
    )


# ---------------------------------------------------------------------------
# JarvisContext (v1 fields + v2 enrichments)
# ---------------------------------------------------------------------------


class JarvisContext(BaseModel):
    """The single unified snapshot -- what Jarvis knows right now.

    v1 fields (required) and v2 enrichment fields (optional; populated by
    ``build_snapshot`` when the inputs make them meaningful).
    """

    ts: datetime
    macro: MacroSnapshot
    equity: EquitySnapshot
    regime: RegimeSnapshot
    journal: JournalSnapshot
    suggestion: JarvisSuggestion
    notes: list[str] = Field(default_factory=list)

    # v2 enrichment -- optional so callers that build contexts manually
    # (tests, older scripts) continue to work unchanged.
    stress_score: StressScore | None = None
    session_phase: SessionPhase | None = None
    sizing_hint: SizingHint | None = None
    alerts: list[JarvisAlert] = Field(default_factory=list)
    margins: JarvisMargins | None = None
    trajectory: Trajectory | None = None
    playbook: list[str] = Field(default_factory=list)
    explanation: str = ""
    market_context: dict[str, Any] | None = None
    market_context_summary: dict[str, Any] | None = None
    market_context_summary_text: str | None = None

    # v3 (2026-04-26) — JARVIS-as-admin needs phase / regime / bleed /
    # gate-report state in context for the admin gate. Built via
    # `eta_engine.brain.jarvis_session_state.snapshot()`. Optional so
    # v1/v2 callers keep working.
    session_state: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Denormalized SessionStateSnapshot.model_dump(mode='json') — "
            "phase, freeze_label, slow_bleed_level, rolling_expectancy_r, "
            "regime_composite, regime_label, gate_report_status, "
            "trial_budget_remaining."
        ),
    )


# ---------------------------------------------------------------------------
# Provider protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class MacroProvider(Protocol):
    def get_macro(self) -> MacroSnapshot: ...


@runtime_checkable
class EquityProvider(Protocol):
    def get_equity(self) -> EquitySnapshot: ...


@runtime_checkable
class RegimeProvider(Protocol):
    def get_regime(self) -> RegimeSnapshot: ...


@runtime_checkable
class JournalProvider(Protocol):
    def get_journal_snapshot(self) -> JournalSnapshot: ...


# ---------------------------------------------------------------------------
# Action-tier thresholds (v1)
# ---------------------------------------------------------------------------

# Thresholds. Kept as module-level so tests can monkey-patch.
DD_REDUCE_THRESHOLD = 0.02  # 2%
DD_STAND_ASIDE_THRESHOLD = 0.03  # 3%
DD_KILL_THRESHOLD = 0.05  # 5%
OVERRIDE_REVIEW_THRESHOLD = 3
HOURS_TO_MAJOR_EVENT = 1.0  # stand aside 1h before FOMC/CPI
OPEN_RISK_HARD_CAP_R = 3.0

# v2 alert thresholds (fire BEFORE the rigid tier kicks in)
DD_REDUCE_WARN_RATIO = 0.75  # warn at 75% of REDUCE threshold
DD_STAND_ASIDE_WARN_RATIO = 0.85  # warn at 85% of STAND_ASIDE threshold
DD_KILL_WARN_RATIO = 0.80  # warn at 80% of KILL threshold
OVERRIDE_WARN_DELTA = 1  # warn when 1 below REVIEW threshold
OPEN_RISK_WARN_RATIO = 0.80  # warn at 80% of cap
HOURS_TO_EVENT_SOON = 4.0  # info at <4h to event
HOURS_TO_EVENT_IMMINENT = 1.5  # warn at <1.5h

# v2 stress component weights -- must sum to 1.0
STRESS_WEIGHTS: dict[str, float] = {
    "macro_event": 0.25,
    "equity_dd": 0.25,
    "open_risk": 0.15,
    "regime_risk": 0.10,
    "override_rate": 0.10,
    "autopilot": 0.07,
    "correlations": 0.05,
    "macro_bias": 0.03,
}

# v2 session-phase risk multipliers (applied to sizing hint)
SESSION_SIZE_MULTIPLIERS: dict[SessionPhase, float] = {
    SessionPhase.OVERNIGHT: 0.40,  # thin liquidity -- slash size
    SessionPhase.PREMARKET: 0.70,
    SessionPhase.OPEN_DRIVE: 0.80,  # exciting but treacherous
    SessionPhase.MORNING: 1.00,
    SessionPhase.LUNCH: 0.70,  # chop
    SessionPhase.AFTERNOON: 0.95,
    SessionPhase.CLOSE: 0.60,  # MOC noise
}


# ---------------------------------------------------------------------------
# Heuristic action policy (v1)
# ---------------------------------------------------------------------------


def suggest_action(
    macro: MacroSnapshot,
    equity: EquitySnapshot,
    regime: RegimeSnapshot,
    journal: JournalSnapshot,
    market_context: dict[str, Any] | None = None,
) -> JarvisSuggestion:
    """Pure policy from the four snapshots.

    Priority order (highest -> lowest, first-match-wins):
      1. KILL  -- kill-switch already active, or daily DD >= 5%
      2. STAND_ASIDE -- major macro event within 1 hour, or autopilot=REQUIRE_ACK
      3. REDUCE      -- daily DD >= 2% OR open_risk_r > 3R OR CRISIS regime
      4. REVIEW      -- overrides_last_24h >= 3 OR regime flipped recently
                        OR correlations_alert
      5. TRADE       -- otherwise
    """
    warnings: list[str] = []

    # 1. KILL
    if journal.kill_switch_active:
        return JarvisSuggestion(
            action=ActionSuggestion.KILL,
            reason="kill-switch active",
            confidence=1.0,
            warnings=["manual reset required"],
        )
    if equity.daily_drawdown_pct >= DD_KILL_THRESHOLD:
        return JarvisSuggestion(
            action=ActionSuggestion.KILL,
            reason=f"daily drawdown {equity.daily_drawdown_pct:.2%} >= kill threshold {DD_KILL_THRESHOLD:.0%}",
            confidence=1.0,
            warnings=["stop trading now"],
        )

    # 2. STAND_ASIDE
    if (
        macro.hours_until_next_event is not None
        and macro.hours_until_next_event <= HOURS_TO_MAJOR_EVENT
        and macro.next_event_label
    ):
        return JarvisSuggestion(
            action=ActionSuggestion.STAND_ASIDE,
            reason=f"macro event '{macro.next_event_label}' in {macro.hours_until_next_event:.1f}h",
            confidence=0.9,
            warnings=["no new entries until event resolves"],
        )
    if journal.autopilot_mode == "REQUIRE_ACK":
        return JarvisSuggestion(
            action=ActionSuggestion.STAND_ASIDE,
            reason="autopilot watchdog requested acknowledgement",
            confidence=0.8,
            warnings=["operator ack required"],
        )

    # 3. REDUCE (dd >= 3% = STAND_ASIDE precedence kept for backwards compat)
    if equity.daily_drawdown_pct >= DD_STAND_ASIDE_THRESHOLD:
        return JarvisSuggestion(
            action=ActionSuggestion.STAND_ASIDE,
            reason=f"daily drawdown {equity.daily_drawdown_pct:.2%} >= {DD_STAND_ASIDE_THRESHOLD:.0%}",
            confidence=0.85,
            warnings=["cooldown recommended"],
        )
    if equity.daily_drawdown_pct >= DD_REDUCE_THRESHOLD:
        warnings.append(f"dd {equity.daily_drawdown_pct:.2%} -- half size")
        return JarvisSuggestion(
            action=ActionSuggestion.REDUCE,
            reason=f"daily drawdown {equity.daily_drawdown_pct:.2%} >= reduce threshold {DD_REDUCE_THRESHOLD:.0%}",
            confidence=0.75,
            warnings=warnings,
        )
    if equity.open_risk_r > OPEN_RISK_HARD_CAP_R:
        return JarvisSuggestion(
            action=ActionSuggestion.REDUCE,
            reason=f"open risk {equity.open_risk_r:.2f}R > cap {OPEN_RISK_HARD_CAP_R}R",
            confidence=0.8,
            warnings=["trim positions to restore headroom"],
        )
    if regime.regime == "CRISIS":
        return JarvisSuggestion(
            action=ActionSuggestion.REDUCE,
            reason="CRISIS regime detected",
            confidence=0.85,
            warnings=["size down / defensive bias"],
        )

    # 4. REVIEW
    if journal.overrides_last_24h >= OVERRIDE_REVIEW_THRESHOLD:
        return JarvisSuggestion(
            action=ActionSuggestion.REVIEW,
            reason=f"{journal.overrides_last_24h} gate overrides in last 24h",
            confidence=0.7,
            warnings=["review override log before next entry"],
        )
    if regime.flipped_recently:
        return JarvisSuggestion(
            action=ActionSuggestion.REVIEW,
            reason=f"regime flipped to {regime.regime}"
            + (f" from {regime.previous_regime}" if regime.previous_regime else ""),
            confidence=0.7,
            warnings=["playbook may have changed"],
        )
    if journal.correlations_alert:
        return JarvisSuggestion(
            action=ActionSuggestion.REVIEW,
            reason="correlation cluster alert",
            confidence=0.7,
            warnings=["review correlated exposure"],
        )

    if market_context:
        market_regime = str(market_context.get("market_regime") or "UNKNOWN").upper()
        try:
            market_quality = float(market_context.get("market_quality", 0.0))
        except (TypeError, ValueError):
            market_quality = 0.0
        if market_regime == "RISK_OFF" or market_quality <= 3.5:
            return JarvisSuggestion(
                action=ActionSuggestion.REDUCE,
                reason=(f"market context {market_regime.lower()} (quality {market_quality:.1f})"),
                confidence=0.72,
                warnings=["market context weak", "size down"],
            )
        if market_regime == "MIXED" or market_quality <= 6.0:
            warnings.append(
                f"market context {market_regime.lower()} quality {market_quality:.1f}",
            )
            return JarvisSuggestion(
                action=ActionSuggestion.TRADE,
                reason=(f"market context {market_regime.lower()} (quality {market_quality:.1f})"),
                confidence=0.76,
                warnings=warnings,
            )

    # 5. TRADE
    return JarvisSuggestion(
        action=ActionSuggestion.TRADE,
        reason="all gates green",
        confidence=0.8,
    )


# ---------------------------------------------------------------------------
# v2: stress-score aggregation
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_stress_score(
    macro: MacroSnapshot,
    equity: EquitySnapshot,
    regime: RegimeSnapshot,
    journal: JournalSnapshot,
) -> StressScore:
    """Compose a [0,1] stress score from independent factors.

    Each factor emits a 0..1 stress raw value. Weighted composite is the
    dot product of raw * weight. The factor with the largest contribution
    is recorded as the ``binding_constraint`` (i.e. the lever most
    responsible for the current stress level).
    """
    weights = STRESS_WEIGHTS
    components: list[StressComponent] = []

    # 1) macro_event: linear ramp from 0 (>= 6h away) to 1 (imminent or
    # post-event but macro_bias == crisis).
    if macro.hours_until_next_event is None or macro.next_event_label is None:
        macro_event_raw = 0.0
    else:
        hours = max(0.0, macro.hours_until_next_event)
        macro_event_raw = _clamp01(1.0 - hours / 6.0)
    components.append(
        StressComponent(
            name="macro_event",
            value=macro_event_raw,
            weight=weights["macro_event"],
            note=(
                f"event in {macro.hours_until_next_event:.1f}h"
                if macro.hours_until_next_event is not None
                else "no event"
            ),
        )
    )

    # 2) equity_dd: linear ramp from 0 (0% dd) to 1 (>= KILL threshold).
    dd = equity.daily_drawdown_pct
    dd_raw = _clamp01(dd / DD_KILL_THRESHOLD)
    components.append(
        StressComponent(
            name="equity_dd",
            value=dd_raw,
            weight=weights["equity_dd"],
            note=f"dd {dd:.2%}",
        )
    )

    # 3) open_risk: linear ramp from 0 (0R) to 1 (>= OPEN_RISK_HARD_CAP_R * 1.5).
    or_raw = _clamp01(equity.open_risk_r / (OPEN_RISK_HARD_CAP_R * 1.5))
    components.append(
        StressComponent(
            name="open_risk",
            value=or_raw,
            weight=weights["open_risk"],
            note=f"{equity.open_risk_r:.2f}R",
        )
    )

    # 4) regime_risk: CRISIS=1.0; flipped_recently=0.6; low confidence (<0.4)=0.3; else 0.
    reg_raw = 0.0
    reg_note = regime.regime
    if regime.regime == "CRISIS":
        reg_raw = 1.0
        reg_note = "CRISIS"
    elif regime.flipped_recently:
        reg_raw = 0.6
        reg_note = f"flipped to {regime.regime}"
    elif regime.confidence < 0.4:
        reg_raw = 0.3
        reg_note = f"low conf {regime.confidence:.0%}"
    components.append(
        StressComponent(
            name="regime_risk",
            value=reg_raw,
            weight=weights["regime_risk"],
            note=reg_note,
        )
    )

    # 5) override_rate: linear ramp from 0 (0 overrides) to 1 (2x threshold).
    ov_raw = _clamp01(
        journal.overrides_last_24h / (2.0 * OVERRIDE_REVIEW_THRESHOLD),
    )
    components.append(
        StressComponent(
            name="override_rate",
            value=ov_raw,
            weight=weights["override_rate"],
            note=f"{journal.overrides_last_24h} in 24h",
        )
    )

    # 6) autopilot: ACTIVE=0; PAUSED=0.5; REQUIRE_ACK=1.
    ap_map = {"ACTIVE": 0.0, "PAUSED": 0.5, "REQUIRE_ACK": 1.0, "FROZEN": 1.0}
    ap_raw = ap_map.get(journal.autopilot_mode, 0.4)
    components.append(
        StressComponent(
            name="autopilot",
            value=ap_raw,
            weight=weights["autopilot"],
            note=journal.autopilot_mode,
        )
    )

    # 7) correlations: boolean alert -> 1, else 0.
    corr_raw = 1.0 if journal.correlations_alert else 0.0
    components.append(
        StressComponent(
            name="correlations",
            value=corr_raw,
            weight=weights["correlations"],
            note="alert" if journal.correlations_alert else "clear",
        )
    )

    # 8) macro_bias: crisis=1.0; hawkish/dovish=0.2; neutral=0.
    mb = macro.macro_bias.lower()
    if mb == "crisis":
        mb_raw = 1.0
    elif mb in {"hawkish", "dovish"}:
        mb_raw = 0.2
    else:
        mb_raw = 0.0
    components.append(
        StressComponent(
            name="macro_bias",
            value=mb_raw,
            weight=weights["macro_bias"],
            note=macro.macro_bias,
        )
    )

    composite = sum(c.contribution for c in components)
    composite = _clamp01(composite)
    # binding constraint = largest contribution (weight * value)
    binding = max(components, key=lambda c: c.contribution).name
    return StressScore(
        composite=round(composite, 4),
        components=components,
        binding_constraint=binding,
    )


# ---------------------------------------------------------------------------
# v2: session phase
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")


def compute_session_phase(ts: datetime) -> SessionPhase:
    """Map a tz-aware datetime to an NYSE session phase (America/New_York)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    et = ts.astimezone(_ET)
    hm = et.hour + et.minute / 60.0
    # Weekends are overnight regardless (futures trade but RTH phases don't apply).
    if et.weekday() >= 5:  # Sat=5, Sun=6
        return SessionPhase.OVERNIGHT
    if 4.0 <= hm < 9.5:
        return SessionPhase.PREMARKET
    if 9.5 <= hm < 10.5:
        return SessionPhase.OPEN_DRIVE
    if 10.5 <= hm < 12.0:
        return SessionPhase.MORNING
    if 12.0 <= hm < 13.5:
        return SessionPhase.LUNCH
    if 13.5 <= hm < 15.5:
        return SessionPhase.AFTERNOON
    if 15.5 <= hm < 16.0:
        return SessionPhase.CLOSE
    return SessionPhase.OVERNIGHT


# ---------------------------------------------------------------------------
# v2: sizing hint
# ---------------------------------------------------------------------------


def compute_sizing_hint(
    stress: StressScore,
    session: SessionPhase,
    action: ActionSuggestion,
    market_context: dict[str, Any] | None = None,
) -> SizingHint:
    """Map stress composite + session phase + action tier -> size multiplier.

    Base multiplier comes from stress via a stepped ramp. Session phase
    applies a further multiplier. Live market context can nudge the size
    up or down when external data is available. Non-TRADE actions
    collapse to 0.
    """
    if action in {ActionSuggestion.KILL, ActionSuggestion.STAND_ASIDE}:
        return SizingHint(
            size_mult=0.0,
            reason=f"{action.value} -- no new risk",
        )

    s = stress.composite
    if s < 0.20:
        base = 1.00
        reason = "stress low -- full size authorized"
    elif s < 0.40:
        base = 0.75
        reason = f"stress moderate ({s:.0%}) -- size at 75%"
    elif s < 0.60:
        base = 0.50
        reason = f"stress elevated ({s:.0%}) -- size at 50%"
    elif s < 0.80:
        base = 0.25
        reason = f"stress high ({s:.0%}) -- size at 25%"
    else:
        base = 0.0
        reason = f"stress critical ({s:.0%}) -- stand aside"

    # REDUCE/REVIEW cap at 50% regardless of stress floor
    if action == ActionSuggestion.REDUCE:
        base = min(base, 0.50)
        reason = f"REDUCE tier -- capped; {reason}"
    elif action == ActionSuggestion.REVIEW:
        base = min(base, 0.75)
        reason = f"REVIEW tier -- soft cap; {reason}"

    session_mult = SESSION_SIZE_MULTIPLIERS.get(session, 1.0)
    market_mult, market_reason = _market_context_size_multiplier(
        market_context,
        action=action,
    )
    final = _clamp01(base * session_mult * market_mult)
    if session_mult < 1.0:
        reason += f"; session={session.value} x{session_mult:.2f}"
    if market_context:
        reason += f"; {market_reason}"

    return SizingHint(size_mult=round(final, 4), reason=reason)


def _market_context_size_multiplier(
    market_context: dict[str, Any] | None,
    *,
    action: ActionSuggestion,
) -> tuple[float, str]:
    if not market_context:
        return 1.0, "market_context=absent"
    if action in {ActionSuggestion.KILL, ActionSuggestion.STAND_ASIDE}:
        return 0.0, "market_context=no_new_risk"
    regime = str(market_context.get("market_regime") or "UNKNOWN").upper()
    try:
        quality = float(market_context.get("market_quality", 0.0)) / 10.0
    except (TypeError, ValueError):
        quality = 0.0
    quality = _clamp01(quality)
    if regime == "RISK_OFF" or quality <= 0.35:
        return 0.75, f"market_context={regime} quality={quality:.2f} x0.75"
    if regime == "MIXED" or quality <= 0.60:
        return 0.90, f"market_context={regime} quality={quality:.2f} x0.90"
    if regime == "RISK_ON" and quality >= 0.70:
        return 1.05, f"market_context={regime} quality={quality:.2f} x1.05"
    return 1.0, f"market_context={regime} quality={quality:.2f}"


def _market_context_note(market_context: dict[str, Any]) -> str:
    regime = market_context.get("market_regime") or "UNKNOWN"
    try:
        quality = float(market_context.get("market_quality", 0.0))
    except (TypeError, ValueError):
        quality = 0.0
    external = market_context.get("external_score")
    note = f"market_context={regime} quality={quality:.2f}"
    if external is not None:
        try:
            external_value = float(external)
        except (TypeError, ValueError):
            external_value = 0.0
        note += f" external={external_value:.2f}"
    return note


# ---------------------------------------------------------------------------
# v2: alerts (fire BEFORE the rigid tier)
# ---------------------------------------------------------------------------


def detect_alerts(
    macro: MacroSnapshot,
    equity: EquitySnapshot,
    regime: RegimeSnapshot,
    journal: JournalSnapshot,
) -> list[JarvisAlert]:
    """Emit graduated INFO/WARN/CRITICAL alerts for approaching thresholds."""
    alerts: list[JarvisAlert] = []

    # DD approaching each tier. Escalate with proximity.
    dd = equity.daily_drawdown_pct
    if dd >= DD_KILL_THRESHOLD:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.CRITICAL,
                code="dd_at_kill",
                message=f"daily DD {dd:.2%} >= kill {DD_KILL_THRESHOLD:.0%} -- flatten now",
                severity=1.0,
            )
        )
    elif dd >= DD_KILL_THRESHOLD * DD_KILL_WARN_RATIO:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.CRITICAL,
                code="dd_approaching_kill",
                message=f"daily DD {dd:.2%} within {(1 - DD_KILL_WARN_RATIO) * 100:.0f}% of kill",
                severity=0.9,
            )
        )
    elif dd >= DD_STAND_ASIDE_THRESHOLD:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="dd_at_stand_aside",
                message=f"daily DD {dd:.2%} >= stand-aside {DD_STAND_ASIDE_THRESHOLD:.0%}",
                severity=0.75,
            )
        )
    elif dd >= DD_STAND_ASIDE_THRESHOLD * DD_STAND_ASIDE_WARN_RATIO:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="dd_approaching_stand_aside",
                message=f"daily DD {dd:.2%} nearing stand-aside {DD_STAND_ASIDE_THRESHOLD:.0%}",
                severity=0.6,
            )
        )
    elif dd >= DD_REDUCE_THRESHOLD:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="dd_at_reduce",
                message=f"daily DD {dd:.2%} >= reduce {DD_REDUCE_THRESHOLD:.0%}",
                severity=0.5,
            )
        )
    elif dd >= DD_REDUCE_THRESHOLD * DD_REDUCE_WARN_RATIO:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.INFO,
                code="dd_approaching_reduce",
                message=f"daily DD {dd:.2%} nearing reduce {DD_REDUCE_THRESHOLD:.0%}",
                severity=0.3,
            )
        )

    # Open risk approaching cap
    if equity.open_risk_r > OPEN_RISK_HARD_CAP_R:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="open_risk_over_cap",
                message=f"open risk {equity.open_risk_r:.2f}R over cap {OPEN_RISK_HARD_CAP_R}R",
                severity=0.8,
            )
        )
    elif equity.open_risk_r >= OPEN_RISK_HARD_CAP_R * OPEN_RISK_WARN_RATIO:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.INFO,
                code="open_risk_approaching_cap",
                message=f"open risk {equity.open_risk_r:.2f}R nearing cap",
                severity=0.4,
            )
        )

    # Overrides approaching review
    oth = OVERRIDE_REVIEW_THRESHOLD
    if journal.overrides_last_24h >= oth:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="overrides_at_review",
                message=f"{journal.overrides_last_24h} overrides in 24h -- review",
                severity=0.6,
            )
        )
    elif journal.overrides_last_24h >= oth - OVERRIDE_WARN_DELTA:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.INFO,
                code="overrides_approaching_review",
                message=f"{journal.overrides_last_24h} overrides in 24h -- 1 from review",
                severity=0.35,
            )
        )

    # Macro event proximity
    hrs = macro.hours_until_next_event
    label = macro.next_event_label
    if hrs is not None and label:
        if hrs <= HOURS_TO_MAJOR_EVENT:
            alerts.append(
                JarvisAlert(
                    level=AlertLevel.CRITICAL,
                    code="macro_event_imminent",
                    message=f"{label} in {hrs:.1f}h -- stand aside",
                    severity=0.9,
                )
            )
        elif hrs <= HOURS_TO_EVENT_IMMINENT:
            alerts.append(
                JarvisAlert(
                    level=AlertLevel.WARN,
                    code="macro_event_soon",
                    message=f"{label} in {hrs:.1f}h -- size down",
                    severity=0.6,
                )
            )
        elif hrs <= HOURS_TO_EVENT_SOON:
            alerts.append(
                JarvisAlert(
                    level=AlertLevel.INFO,
                    code="macro_event_upcoming",
                    message=f"{label} in {hrs:.1f}h",
                    severity=0.3,
                )
            )

    # Regime flip
    if regime.flipped_recently:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="regime_flipped",
                message=f"regime flipped -> {regime.regime}"
                + (f" (from {regime.previous_regime})" if regime.previous_regime else ""),
                severity=0.5,
            )
        )
    elif regime.confidence < 0.4:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.INFO,
                code="regime_low_confidence",
                message=f"regime {regime.regime} low conf {regime.confidence:.0%}",
                severity=0.25,
            )
        )

    # Autopilot state
    if journal.autopilot_mode == "REQUIRE_ACK":
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="autopilot_require_ack",
                message="watchdog requires ack on a stale position",
                severity=0.7,
            )
        )
    elif journal.autopilot_mode == "FROZEN":
        alerts.append(
            JarvisAlert(
                level=AlertLevel.CRITICAL,
                code="autopilot_frozen",
                message="autopilot frozen after force-flatten",
                severity=1.0,
            )
        )
    elif journal.autopilot_mode not in {"ACTIVE"}:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.INFO,
                code="autopilot_non_standard_mode",
                message=f"autopilot_mode={journal.autopilot_mode}",
                severity=0.2,
            )
        )

    # Correlations
    if journal.correlations_alert:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.WARN,
                code="correlations_alert",
                message="correlation cluster active -- trim concentrated exposure",
                severity=0.55,
            )
        )

    # Kill switch
    if journal.kill_switch_active:
        alerts.append(
            JarvisAlert(
                level=AlertLevel.CRITICAL,
                code="kill_switch_active",
                message="kill switch is ACTIVE -- manual reset required",
                severity=1.0,
            )
        )

    # Crisis macro bias (independent of event proximity)
    if macro.macro_bias.lower() == "crisis":
        alerts.append(
            JarvisAlert(
                level=AlertLevel.CRITICAL,
                code="macro_bias_crisis",
                message="macro_bias=CRISIS -- defensive stance",
                severity=0.85,
            )
        )

    # Sort by severity descending for operator readability.
    alerts.sort(key=lambda a: a.severity, reverse=True)
    return alerts


# ---------------------------------------------------------------------------
# v2: margins
# ---------------------------------------------------------------------------


def compute_margins(
    equity: EquitySnapshot,
    journal: JournalSnapshot,
) -> JarvisMargins:
    """How far from each rigid action tier, in native units."""
    dd = equity.daily_drawdown_pct
    return JarvisMargins(
        dd_to_reduce=round(DD_REDUCE_THRESHOLD - dd, 6),
        dd_to_stand_aside=round(DD_STAND_ASIDE_THRESHOLD - dd, 6),
        dd_to_kill=round(DD_KILL_THRESHOLD - dd, 6),
        overrides_to_review=OVERRIDE_REVIEW_THRESHOLD - journal.overrides_last_24h,
        open_risk_to_cap_r=round(OPEN_RISK_HARD_CAP_R - equity.open_risk_r, 4),
    )


# ---------------------------------------------------------------------------
# v2: playbook
# ---------------------------------------------------------------------------


_PLAYBOOK_BY_ACTION: dict[ActionSuggestion, list[str]] = {
    ActionSuggestion.TRADE: [
        "take only A+ setups that pass the full checklist",
        "size per Kelly cap + sizing_hint.size_mult",
        "journal decision rationale before entry",
    ],
    ActionSuggestion.REVIEW: [
        "do NOT enter new positions until reviewed",
        "open the override log / regime flip / correlation map as relevant",
        "if review passes, resume but at reduced size for 3 trades",
    ],
    ActionSuggestion.REDUCE: [
        "cut open size in half OR move stops to breakeven",
        "no pyramiding, no averaging down",
        "cool off one full session if DD hits stand-aside level",
    ],
    ActionSuggestion.STAND_ASIDE: [
        "flatten OR move stops tight; do not add risk",
        "wait for the binding constraint to clear (event, DD, ack)",
        "log the stand-aside decision in the journal",
    ],
    ActionSuggestion.KILL: [
        "flatten all positions immediately",
        "cancel resting orders",
        "manually reset kill switch and re-run pre-market checklist",
        "do NOT re-enter the same day",
    ],
}


def build_playbook(
    suggestion: JarvisSuggestion,
    stress: StressScore | None = None,
    session: SessionPhase | None = None,
) -> list[str]:
    """Return a concrete step list for the chosen action, session-aware."""
    base = list(_PLAYBOOK_BY_ACTION[suggestion.action])

    if suggestion.action == ActionSuggestion.TRADE and stress is not None:
        if stress.composite >= 0.4:
            base.append(
                f"stress {stress.composite:.0%} -- tighten stops and cut size vs normal",
            )
        base.append(f"binding constraint: {stress.binding_constraint}")

    if session == SessionPhase.OPEN_DRIVE:
        base.append("first-hour rules: wait for the 1st 15m high/low to break")
    elif session == SessionPhase.LUNCH:
        base.append("lunch chop: fade only, no trend continuation trades")
    elif session == SessionPhase.CLOSE:
        base.append("MOC: no new entries inside last 15m")
    elif session == SessionPhase.OVERNIGHT:
        base.append("overnight: half-size max, wider stops, liquidity thin")

    return base


# ---------------------------------------------------------------------------
# v2: natural-language explanation
# ---------------------------------------------------------------------------


def build_explanation(
    suggestion: JarvisSuggestion,
    stress: StressScore,
    margins: JarvisMargins,
    session: SessionPhase,
    sizing: SizingHint,
) -> str:
    """One-paragraph operator-grade summary of why Jarvis says what it says."""
    parts: list[str] = []
    parts.append(
        f"Jarvis says {suggestion.action.value} (confidence {suggestion.confidence:.0%}) because {suggestion.reason}."
    )
    parts.append(f"Composite stress is {stress.composite:.0%}, dominated by {stress.binding_constraint}.")
    # Margins to the next DD tier that hasn't fired yet.
    if margins.dd_to_kill > 0 and margins.dd_to_stand_aside <= 0:
        parts.append(f"Already past stand-aside; {margins.dd_to_kill * 100:.2f}% to kill.")
    elif margins.dd_to_stand_aside > 0 and margins.dd_to_reduce <= 0:
        parts.append(f"In REDUCE range; {margins.dd_to_stand_aside * 100:.2f}% of headroom before stand-aside.")
    elif margins.dd_to_reduce > 0:
        parts.append(
            f"DD headroom: {margins.dd_to_reduce * 100:.2f}% before REDUCE, "
            f"{margins.dd_to_stand_aside * 100:.2f}% before STAND_ASIDE."
        )
    parts.append(f"Session {session.value}. Suggested size {sizing.size_mult:.0%}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Snapshot builder (v1, now also fills v2 fields)
# ---------------------------------------------------------------------------


def build_snapshot(
    *,
    macro: MacroSnapshot,
    equity: EquitySnapshot,
    regime: RegimeSnapshot,
    journal: JournalSnapshot,
    ts: datetime | None = None,
    notes: list[str] | None = None,
    market_context: dict[str, Any] | None = None,
    market_context_summary: dict[str, Any] | None = None,
) -> JarvisContext:
    """Assemble the four snapshots + suggestion + v2 enrichments."""
    now = ts or datetime.now(UTC)
    suggestion = suggest_action(
        macro,
        equity,
        regime,
        journal,
        market_context=market_context,
    )
    stress = compute_stress_score(macro, equity, regime, journal)
    session = compute_session_phase(now)
    sizing = compute_sizing_hint(
        stress,
        session,
        suggestion.action,
        market_context=market_context,
    )
    alerts = detect_alerts(macro, equity, regime, journal)
    margins = compute_margins(equity, journal)
    playbook = build_playbook(suggestion, stress=stress, session=session)
    explanation = build_explanation(
        suggestion,
        stress,
        margins,
        session,
        sizing,
    )
    merged_notes = list(notes or [])
    if market_context:
        merged_notes.append(_market_context_note(market_context))
    summary = market_context_summary
    if summary is None and market_context is not None:
        summary = build_market_context_summary(market_context)
    summary_text = format_market_context_summary(summary) if summary else None
    return JarvisContext(
        ts=now,
        macro=macro,
        equity=equity,
        regime=regime,
        journal=journal,
        suggestion=suggestion,
        notes=merged_notes,
        stress_score=stress,
        session_phase=session,
        sizing_hint=sizing,
        alerts=alerts,
        margins=margins,
        playbook=playbook,
        explanation=explanation,
        market_context=market_context,
        market_context_summary=summary,
        market_context_summary_text=summary_text,
    )


# ---------------------------------------------------------------------------
# Builder that wires providers (v1)
# ---------------------------------------------------------------------------


class JarvisContextBuilder:
    """Holds the four providers; one call refreshes everything.

    Parameters
    ----------
    macro_provider, equity_provider, regime_provider, journal_provider:
        Any object matching the respective Protocol.
    clock:
        Callable returning current datetime. Defaults to ``datetime.now(UTC)``.
        Injectable for tests.
    """

    def __init__(
        self,
        *,
        macro_provider: MacroProvider,
        equity_provider: EquityProvider,
        regime_provider: RegimeProvider,
        journal_provider: JournalProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(macro_provider, MacroProvider):
            raise TypeError("macro_provider must implement MacroProvider")
        if not isinstance(equity_provider, EquityProvider):
            raise TypeError("equity_provider must implement EquityProvider")
        if not isinstance(regime_provider, RegimeProvider):
            raise TypeError("regime_provider must implement RegimeProvider")
        if not isinstance(journal_provider, JournalProvider):
            raise TypeError("journal_provider must implement JournalProvider")
        self._macro = macro_provider
        self._equity = equity_provider
        self._regime = regime_provider
        self._journal = journal_provider
        self._clock = clock or (lambda: datetime.now(UTC))

    def snapshot(
        self,
        *,
        notes: list[str] | None = None,
        market_context: dict[str, Any] | None = None,
        market_context_summary: dict[str, Any] | None = None,
    ) -> JarvisContext:
        return build_snapshot(
            macro=self._macro.get_macro(),
            equity=self._equity.get_equity(),
            regime=self._regime.get_regime(),
            journal=self._journal.get_journal_snapshot(),
            ts=self._clock(),
            notes=notes,
            market_context=market_context,
            market_context_summary=market_context_summary,
        )


# ---------------------------------------------------------------------------
# v2: JarvisMemory + JarvisContextEngine
# ---------------------------------------------------------------------------


_TRAJ_EPS_DD = 0.0025  # 0.25% DD delta counts as movement
_TRAJ_EPS_STRESS = 0.05  # 5-point stress delta counts as movement


def _trajectory(series: list[float], eps: float) -> TrajectoryState:
    """Classify a short series as IMPROVING/FLAT/WORSENING.

    Uses first-vs-last delta. Worsening = strictly higher last than first
    by more than ``eps``; improving = strictly lower by more than eps;
    otherwise flat.
    """
    if len(series) < 2:
        return TrajectoryState.UNKNOWN
    delta = series[-1] - series[0]
    if delta > eps:
        return TrajectoryState.WORSENING
    if delta < -eps:
        return TrajectoryState.IMPROVING
    return TrajectoryState.FLAT


class JarvisMemory:
    """Bounded FIFO of recent JarvisContext snapshots for trajectory analysis.

    Thread-safety: not thread-safe. Engine ticks are expected from a single
    actor.
    """

    def __init__(self, *, maxlen: int = 64) -> None:
        if maxlen < 2:
            raise ValueError("JarvisMemory maxlen must be >= 2")
        self._buf: deque[JarvisContext] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._buf)

    def append(self, ctx: JarvisContext) -> None:
        self._buf.append(ctx)

    def snapshots(self) -> list[JarvisContext]:
        return list(self._buf)

    def trajectory(self) -> Trajectory:
        if len(self._buf) < 2:
            return Trajectory()
        dd_series = [c.equity.daily_drawdown_pct for c in self._buf]
        stress_series = [(c.stress_score.composite if c.stress_score else 0.0) for c in self._buf]
        first = self._buf[0].ts
        last = self._buf[-1].ts
        window = (last - first).total_seconds()

        # overrides_velocity_per_24h
        ov_series = [c.journal.overrides_last_24h for c in self._buf]
        ov_delta = max(0, ov_series[-1] - ov_series[0])
        ov_velocity = (ov_delta / window * 86400.0) if window > 0 else 0.0

        return Trajectory(
            dd=_trajectory(dd_series, _TRAJ_EPS_DD),
            stress=_trajectory(stress_series, _TRAJ_EPS_STRESS),
            overrides_velocity_per_24h=round(ov_velocity, 4),
            samples=len(self._buf),
            window_seconds=round(window, 3),
        )


def _as_macro_provider(obj: object) -> MacroProvider:
    if isinstance(obj, MacroProvider):
        return obj
    if callable(obj):

        class _MacroAdapter:
            def get_macro(self) -> MacroSnapshot:
                return obj()

        return _MacroAdapter()
    raise TypeError(
        "macro_provider must be a MacroProvider or a callable returning MacroSnapshot",
    )


def _as_equity_provider(obj: object) -> EquityProvider:
    if isinstance(obj, EquityProvider):
        return obj
    if callable(obj):

        class _EquityAdapter:
            def get_equity(self) -> EquitySnapshot:
                return obj()

        return _EquityAdapter()
    raise TypeError(
        "equity_provider must be an EquityProvider or a callable returning EquitySnapshot",
    )


def _as_regime_provider(obj: object) -> RegimeProvider:
    if isinstance(obj, RegimeProvider):
        return obj
    if callable(obj):

        class _RegimeAdapter:
            def get_regime(self) -> RegimeSnapshot:
                return obj()

        return _RegimeAdapter()
    raise TypeError(
        "regime_provider must be a RegimeProvider or a callable returning RegimeSnapshot",
    )


def _as_journal_provider(obj: object) -> JournalProvider:
    if isinstance(obj, JournalProvider):
        return obj
    if callable(obj):

        class _JournalAdapter:
            def get_journal_snapshot(self) -> JournalSnapshot:
                return obj()

        return _JournalAdapter()
    raise TypeError(
        "journal_provider must be a JournalProvider or a callable returning JournalSnapshot",
    )


class JarvisContextEngine:
    """Continuous-loop engine: builds snapshots and maintains memory.

    Thin wrapper over ``JarvisContextBuilder`` + ``JarvisMemory`` so that
    ``engine.tick()`` returns a fully enriched JarvisContext (including
    trajectory from memory).

    Parameters
    ----------
    builder:
        JarvisContextBuilder that produces raw snapshots. Either pass this
        explicitly, OR pass the four ``*_provider`` convenience kwargs below
        (Protocol-compliant objects or bare callables returning the
        corresponding snapshot type).
    memory:
        JarvisMemory instance. If omitted, a default of maxlen=64 is created.
    macro_provider, equity_provider, regime_provider, journal_provider:
        Convenience: used to construct a ``JarvisContextBuilder`` internally
        when ``builder`` is not supplied. Each accepts either a
        Protocol-compliant object or a bare callable returning the matching
        snapshot (e.g. ``lambda: my_macro_snapshot``).
    clock:
        Clock override (used only when ``builder`` is synthesized here).
    """

    def __init__(
        self,
        *,
        builder: JarvisContextBuilder | None = None,
        memory: JarvisMemory | None = None,
        macro_provider: object = None,
        equity_provider: object = None,
        regime_provider: object = None,
        journal_provider: object = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if builder is None:
            missing = [
                name
                for name, val in (
                    ("macro_provider", macro_provider),
                    ("equity_provider", equity_provider),
                    ("regime_provider", regime_provider),
                    ("journal_provider", journal_provider),
                )
                if val is None
            ]
            if missing:
                raise TypeError(
                    f"JarvisContextEngine requires either builder= or all four provider kwargs; missing: {missing}",
                )
            builder = JarvisContextBuilder(
                macro_provider=_as_macro_provider(macro_provider),
                equity_provider=_as_equity_provider(equity_provider),
                regime_provider=_as_regime_provider(regime_provider),
                journal_provider=_as_journal_provider(journal_provider),
                clock=clock,
            )
        self._builder = builder
        # NOTE: can't use `memory or JarvisMemory()` because JarvisMemory
        # has __len__ and an empty memory is falsy.
        self.memory = memory if memory is not None else JarvisMemory()

    def tick(self, *, notes: list[str] | None = None) -> JarvisContext:
        ctx = self._builder.snapshot(notes=notes)
        # Attach the memory-based trajectory BEFORE appending the new ctx --
        # ticks should report trajectory up to (not including) the current
        # sample so operators can see "this is where we came from".
        traj = self.memory.trajectory()
        if traj.samples > 0:
            ctx = ctx.model_copy(update={"trajectory": traj})
        # v3.5 upgrade #10 (2026-04-26) — auto-populate session_state on
        # every tick so downstream consumers (jarvis_admin.evaluate_request)
        # never see a stale or absent snapshot. Failures here must NEVER
        # raise — falling back to None keeps v1/v2 callers working.
        if ctx.session_state is None:
            try:
                from eta_engine.brain.jarvis_session_state import snapshot

                snap = snapshot()
                ctx = ctx.model_copy(
                    update={
                        "session_state": snap.model_dump(mode="json"),
                    }
                )
                # v3.6 upgrade #14 — append the snapshot to the state
                # journal so post-hoc audit can replay "what did JARVIS
                # know at this ts?". Best-effort — never blocks the tick.
                try:
                    from eta_engine.brain.jarvis_state_journal import (
                        JarvisStateJournal,
                    )

                    JarvisStateJournal().append(snap)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        self.memory.append(ctx)
        return ctx
