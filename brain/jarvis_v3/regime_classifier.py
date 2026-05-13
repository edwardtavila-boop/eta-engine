"""
JARVIS v3 // regime_classifier (T8)

Lightweight rule-based regime classifier. Given recent fleet state +
sentiment cache + trace volatility, classifies the current market
regime into one of a small set of labels (CALM_TREND / VOL_TREND /
RANGE / CHAOS / EUPHORIA / CAPITULATION) and surfaces a recommended
override pack — pre-defined size_modifier + school_weight tuples that
work well in that regime.

Operator workflow: see ``current_regime()`` for the live label; if
they agree with the recommendation, invoke
``apply_pack(name)`` which fires the matching ``jarvis_set_size_modifier``
and ``jarvis_pin_school_weight`` overrides.

V1 is rule-based, not ML — the operator defines packs once, the
classifier picks one. A future T8.v2 can train a model on per-school
features from schema v2 traces + outcomes.

Public interface
----------------

* ``current_regime()`` — returns ``RegimeReport`` with the active
  label + confidence + matching recommended pack.
* ``apply_pack(name, ttl_minutes)`` — composes the pack's overrides
  into hermes_overrides.json via existing apply_* surfaces.
* ``list_packs()`` — operator-readable list of available packs.
* ``RegimeReport`` dataclass.

NEVER raises.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.regime_classifier")

EXPECTED_HOOKS = ("current_regime", "apply_pack", "list_packs")


@dataclass(frozen=True)
class OverridePack:
    """A pre-defined override bundle indexed by name."""

    name: str
    regime_match: str
    rationale: str
    size_modifiers: dict[str, float]  # {bot_id_pattern: multiplier}
    school_weights: dict[str, dict[str, float]]  # {asset: {school: weight}}


# ---------------------------------------------------------------------------
# Built-in override packs. Operator can edit at runtime by replacing this
# dict (or its entries) via Hermes memory + a future MCP tool. For v1 it's
# code-defined; that's intentional — operator should review what each pack
# does before any classifier auto-applies it.
# ---------------------------------------------------------------------------

BUILTIN_PACKS: dict[str, OverridePack] = {
    "calm_trend": OverridePack(
        name="calm_trend",
        regime_match="CALM_TREND",
        rationale=(
            "Quiet up/down trend. Momentum schools have the edge. "
            "Bias size UP slightly, push mean-revert down. Modest tilt."
        ),
        size_modifiers={},  # no per-bot pin, all bots get the school tilt
        school_weights={
            "MNQ": {"momentum": 1.15, "mean_revert": 0.85},
            "BTC": {"momentum": 1.15, "mean_revert": 0.85},
            "ETH": {"momentum": 1.10, "mean_revert": 0.90},
        },
    ),
    "vol_trend": OverridePack(
        name="vol_trend",
        regime_match="VOL_TREND",
        rationale=(
            "Volatile trend (post-FOMC, post-earnings). Momentum still wins "
            "but sizing must be defensive. Trim every bot to 0.7×."
        ),
        size_modifiers={"*": 0.7},  # '*' = all bots
        school_weights={
            "MNQ": {"momentum": 1.10},
            "BTC": {"momentum": 1.10},
        },
    ),
    "range": OverridePack(
        name="range",
        regime_match="RANGE",
        rationale=(
            "Choppy range. Mean-revert wins, momentum gets chopped up. Boost mean_revert, trim momentum to 0.6×."
        ),
        size_modifiers={},
        school_weights={
            "MNQ": {"mean_revert": 1.30, "momentum": 0.60},
            "BTC": {"mean_revert": 1.20, "momentum": 0.70},
            "ETH": {"mean_revert": 1.20, "momentum": 0.70},
        },
    ),
    "chaos": OverridePack(
        name="chaos",
        regime_match="CHAOS",
        rationale=("Whipsaw / news-driven chop. No school has edge. Trim everything to 0.4× and let the storm pass."),
        size_modifiers={"*": 0.4},
        school_weights={},
    ),
    "euphoria": OverridePack(
        name="euphoria",
        regime_match="EUPHORIA",
        rationale=(
            "Sentiment ≥ 0.85 + social volume spike. Momentum is the trap "
            "right now. Trim momentum to 0.6×, lean mean-revert."
        ),
        size_modifiers={},
        school_weights={
            "BTC": {"momentum": 0.60, "mean_revert": 1.30},
            "ETH": {"momentum": 0.60, "mean_revert": 1.30},
        },
    ),
    "capitulation": OverridePack(
        name="capitulation",
        regime_match="CAPITULATION",
        rationale=(
            "Sentiment ≤ 0.15 + capitulation topic flag. Operator opportunity "
            "but high uncertainty — flag, don't auto-apply sizing."
        ),
        size_modifiers={},
        school_weights={},
    ),
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeReport:
    # "CALM_TREND" | "VOL_TREND" | "RANGE" | "CHAOS" | "EUPHORIA" | "CAPITULATION" | "UNKNOWN"
    regime: str
    confidence: float  # 0..1
    rationale: str
    recommended_pack: str | None
    features: dict[str, Any]  # the input signals the classifier saw
    asof: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_sentiment(asset: str) -> dict[str, Any] | None:
    try:
        from eta_engine.brain.jarvis_v3 import sentiment_overlay

        return sentiment_overlay.current_sentiment(asset)
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime_classifier: sentiment fetch failed: %s", exc)
        return None


def _safe_fleet_drawdown() -> float | None:
    """Read latest portfolio_drawdown_today_r from the trace tail.
    Returns ``None`` if no v2 record is available.
    """
    try:
        from eta_engine.brain.jarvis_v3 import trace_emitter

        records = trace_emitter.tail(n=20) or []
        for rec in reversed(records):
            pi = rec.get("portfolio_inputs") or {}
            if isinstance(pi, dict) and "portfolio_drawdown_today_r" in pi:
                try:
                    return float(pi["portfolio_drawdown_today_r"])
                except (TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime_classifier: drawdown fetch failed: %s", exc)
    return None


def current_regime() -> RegimeReport:
    """Classify the current market regime using available signals.

    Decision ladder (first match wins):

      1. Sentiment fear_greed ≥ 0.85 AND social_volume_z ≥ 1.5
           → EUPHORIA
      2. Sentiment fear_greed ≤ 0.15 AND capitulation topic flag
           → CAPITULATION
      3. Fleet drawdown <= -3R                  → CHAOS
      4. Fleet drawdown between -1R and -3R     → VOL_TREND
      5. (high social_volume_z but neutral fg)  → VOL_TREND
      6. (low social_volume_z and neutral fg)   → RANGE
      7. Otherwise                                → CALM_TREND

    NEVER raises. Returns UNKNOWN with low confidence when no signals
    are available (typical on a fresh install).
    """
    asof = datetime.now(UTC).isoformat()
    btc_sent = _safe_sentiment("BTC") or {}
    drawdown = _safe_fleet_drawdown()
    features = {
        "btc_fear_greed": btc_sent.get("fear_greed"),
        "btc_social_volume_z": btc_sent.get("social_volume_z"),
        "btc_topic_flags": btc_sent.get("topic_flags") or {},
        "fleet_drawdown_today_r": drawdown,
    }

    fg = features["btc_fear_greed"]
    vol_z = features["btc_social_volume_z"]
    flags = features["btc_topic_flags"]
    dd = features["fleet_drawdown_today_r"]

    # 1. EUPHORIA
    if isinstance(fg, (int, float)) and isinstance(vol_z, (int, float)) and float(fg) >= 0.85 and float(vol_z) >= 1.5:
        return RegimeReport(
            regime="EUPHORIA",
            confidence=0.85,
            rationale="fear_greed ≥ 0.85 with social_volume_z ≥ 1.5 — crowd is full long",
            recommended_pack="euphoria",
            features=features,
            asof=asof,
        )

    # 2. CAPITULATION
    if isinstance(fg, (int, float)) and float(fg) <= 0.15 and isinstance(flags, dict) and flags.get("capitulation"):
        return RegimeReport(
            regime="CAPITULATION",
            confidence=0.80,
            rationale="fear_greed ≤ 0.15 + capitulation flag — peak fear",
            recommended_pack="capitulation",
            features=features,
            asof=asof,
        )

    # 3 / 4. Drawdown-driven
    if isinstance(dd, (int, float)):
        if float(dd) <= -3.0:
            return RegimeReport(
                regime="CHAOS",
                confidence=0.75,
                rationale=f"fleet drawdown {dd:.1f}R — whipsaw conditions",
                recommended_pack="chaos",
                features=features,
                asof=asof,
            )
        if -3.0 < float(dd) <= -1.0:
            return RegimeReport(
                regime="VOL_TREND",
                confidence=0.65,
                rationale=f"fleet drawdown {dd:.1f}R but not yet chaotic — volatile trend",
                recommended_pack="vol_trend",
                features=features,
                asof=asof,
            )

    # 5 / 6. Social volume-driven
    if isinstance(vol_z, (int, float)):
        if float(vol_z) >= 1.5:
            return RegimeReport(
                regime="VOL_TREND",
                confidence=0.60,
                rationale=f"social_volume_z {vol_z:.1f}σ — elevated activity",
                recommended_pack="vol_trend",
                features=features,
                asof=asof,
            )
        if float(vol_z) <= -1.0:
            return RegimeReport(
                regime="RANGE",
                confidence=0.60,
                rationale=f"social_volume_z {vol_z:.1f}σ — quiet, range-bound",
                recommended_pack="range",
                features=features,
                asof=asof,
            )

    # 7. Default
    if any(v is not None for v in (fg, vol_z, dd)):
        return RegimeReport(
            regime="CALM_TREND",
            confidence=0.50,
            rationale="no extreme signals — quiet trend",
            recommended_pack="calm_trend",
            features=features,
            asof=asof,
        )
    return RegimeReport(
        regime="UNKNOWN",
        confidence=0.10,
        rationale="no sentiment, no drawdown signal available yet",
        recommended_pack=None,
        features=features,
        asof=asof,
    )


def list_packs() -> list[dict[str, Any]]:
    """Operator-readable list of available override packs."""
    return [asdict(p) for p in BUILTIN_PACKS.values()]


def apply_pack(
    name: str,
    ttl_minutes: int = 240,
    bot_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Apply the named pack's size_modifiers + school_weights.

    For size_modifiers keyed by "*" (all bots), the caller MUST supply
    ``bot_ids`` so we know which bots to pin — there's no introspection
    of "all bots" inside this module to avoid coupling to the fleet
    registry.

    Returns a summary of what was applied + any failures (best-effort).
    """
    if not name:
        return {"status": "REJECTED", "reason": "missing_pack_name"}
    pack = BUILTIN_PACKS.get(name)
    if pack is None:
        return {"status": "REJECTED", "reason": f"unknown_pack:{name}"}
    try:
        from eta_engine.brain.jarvis_v3 import hermes_overrides
    except Exception as exc:  # noqa: BLE001
        return {"status": "WRITE_FAILED", "reason": f"import_failed:{exc}"}

    applied_sizes: list[dict[str, Any]] = []
    applied_schools: list[dict[str, Any]] = []
    errors: list[str] = []

    # ─── size_modifiers ───
    for pattern, mod in pack.size_modifiers.items():
        if pattern == "*":
            if not bot_ids:
                errors.append("pack has '*' pattern but bot_ids list not provided")
                continue
            for bid in bot_ids:
                r = hermes_overrides.apply_size_modifier(
                    bot_id=bid,
                    modifier=float(mod),
                    reason=f"pack:{name} — {pack.rationale[:60]}",
                    ttl_minutes=ttl_minutes,
                )
                applied_sizes.append(r)
                if r.get("status") not in ("APPLIED", "REACQUIRED"):
                    errors.append(f"size_modifier({bid}): {r.get('status')}")
        else:
            r = hermes_overrides.apply_size_modifier(
                bot_id=pattern,
                modifier=float(mod),
                reason=f"pack:{name} — {pack.rationale[:60]}",
                ttl_minutes=ttl_minutes,
            )
            applied_sizes.append(r)

    # ─── school_weights ───
    for asset, schools in pack.school_weights.items():
        for school, weight in schools.items():
            r = hermes_overrides.apply_school_weight(
                asset=asset,
                school=school,
                weight=float(weight),
                reason=f"pack:{name} — {pack.rationale[:60]}",
                ttl_minutes=ttl_minutes,
            )
            applied_schools.append(r)

    return {
        "status": "APPLIED" if not errors else "PARTIAL",
        "pack_name": name,
        "regime_match": pack.regime_match,
        "applied_size_modifiers": applied_sizes,
        "applied_school_weights": applied_schools,
        "errors": errors,
        "ttl_minutes": ttl_minutes,
    }
