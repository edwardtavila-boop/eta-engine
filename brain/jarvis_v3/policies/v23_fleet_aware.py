"""Candidate policy v23 — FLEET-AWARE supercharge (2026-05-04).

Hypothesis
----------
After 11 rounds of strategy-lab work, the fleet has 18 signal generators
across 5 instrument types. JARVIS's gate logic is still pinned to a
hardcoded SubsystemId enum and a hardcoded overnight-whitelist set,
which means:

  * New bots (gold_dxy_inverse, zn_safe_haven, cl_oil_gold_ratio, etc.)
    have no SubsystemId, so their requests match an unlisted enum and
    fall through fall-back logic instead of getting class-aware gates.
  * The overnight whitelist must be edited in code every time a new
    bot is added — should be derived from an `instrument_class` extras
    field instead.
  * All bots get the same `size_cap_mult` regardless of their lab
    sharpe — a sharpe-2.5 bot gets the same cap as a sharpe-0.7 bot.
  * The registry's `block_regimes` field exists but JARVIS doesn't
    actually honor it — strategies fire in regimes they shouldn't.

v23 fixes all of these on top of v17/v22 with NO breaking changes:

  1. Resolves bot identity via:
       (a) explicit SubsystemId enum (existing)
       (b) `payload['bot_id']` lookup against per_bot_registry (new)
  2. Computes overnight eligibility from the bot's `instrument_class`
     extras field, falling back to the legacy hardcoded set.
  3. Reads `lab_audit_*` stamps from the bot's extras and scales the
     `size_cap_mult` by a sharpe-aware factor:
         sharpe >= 2.0  → 1.00x  (full size)
         1.0–2.0        → 0.75x  (haircut for tier-2)
         0.5–1.0        → 0.50x  (tier-3, half size)
         <0.5 / no lab  → 0.30x  (untested or marginal)
  4. Honors `block_regimes` from the registry: if the active global
     regime (from `var/eta_engine/state/regime_state.json`) is in the
     bot's block_regimes set, return DEFERRED.
  5. Falls back to v17/v22 cleanly on ANY error — wrapping is purely
     additive.

Activation
----------
Set env var ``JARVIS_V3_FLEET_AWARE=1`` (or feature flag of the same
name) to activate. Default OFF — every new behavior is opt-in.

Tests in eta_engine/tests/test_jarvis_v23_fleet_aware.py.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import workspace_roots

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    Verdict,
    evaluate_request,
)

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_context import JarvisContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Instrument class taxonomy
# ---------------------------------------------------------------------------


# Maps registry's `instrument_class` extras values to broad gate-relevant
# categories. Multiple registry values can collapse to one class.
_INSTRUMENT_CLASS_TO_BROAD = {
    "crypto": "crypto",
    "crypto_native": "crypto",
    "crypto_alt": "crypto",
    "crypto_alt_meme": "crypto",
    "crypto_perp": "crypto",
    # equity-index futures (CME): MNQ/NQ/ES/MES
    "futures_index": "futures_index",
    "equity_index": "futures_index",
    # commodities — all roughly 23h Globex sessions
    "commodity": "commodity",
    "commodity_metals": "commodity",
    "commodity_metals_micro": "commodity",
    "commodity_energy": "commodity",
    "commodity_grains": "commodity",
    # rates futures (CBOT): ZN/ZB/ZF
    "rates": "rates",
    "rates_intermediate": "rates",
    "rates_long_duration": "rates",
    # FX futures (CME): 6E/M6E/6B/6J
    "fx": "fx",
    "currency_futures": "fx",
}

# Broad classes that trade ~24h via Globex / Coinbase — eligible for the
# OVERNIGHT whitelist on the same confluence-pre-gated basis as the legacy
# hardcoded list. Each represents a real session structure, not a guess.
_OVERNIGHT_ELIGIBLE_CLASSES: frozenset[str] = frozenset({"crypto", "futures_index", "commodity", "rates", "fx"})


def _resolve_bot_assignment(req: ActionRequest) -> dict[str, Any] | None:
    """Look up the bot's registry assignment.

    Resolution order:
      1. ``payload['bot_id']`` — supervisor passes this on every signal.
      2. fall back to the SubsystemId-derived guess (e.g. BOT_MNQ → "mnq_bot")
         if the supervisor only sent a SubsystemId.

    Returns the StrategyAssignment as a dict (extras + headline fields), or
    None if no match. ALL ERRORS are swallowed — v23 must never raise.
    """
    bot_id = ""
    try:
        bot_id = str(req.payload.get("bot_id") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not bot_id:
        # Conservative: don't try to guess from SubsystemId yet — it would
        # expand the surface for false positives. Operator can add explicit
        # bot_id to payload as part of supervisor wiring.
        return None
    try:
        from eta_engine.strategies.per_bot_registry import get_for_bot

        a = get_for_bot(bot_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("v23: registry lookup failed for %s: %s", bot_id, exc)
        return None
    if a is None:
        return None
    return {
        "bot_id": a.bot_id,
        "strategy_id": a.strategy_id,
        "symbol": a.symbol,
        "timeframe": a.timeframe,
        "block_regimes": set(a.block_regimes) if a.block_regimes else set(),
        "extras": dict(a.extras) if a.extras else {},
    }


def _instrument_class(assignment: dict[str, Any]) -> str:
    """Resolve broad instrument class from the registry's `instrument_class`.

    Returns "" if not classified.
    """
    raw = str(assignment.get("extras", {}).get("instrument_class", "")).strip().lower()
    return _INSTRUMENT_CLASS_TO_BROAD.get(raw, "")


def _is_overnight_eligible(assignment: dict[str, Any]) -> bool:
    """Class-derived overnight eligibility.

    Used as an EXPANSION of the legacy hardcoded overnight whitelist —
    we never narrow eligibility, only widen it for newly-onboarded bots
    whose SubsystemId isn't in the legacy frozen-set yet.
    """
    cls = _instrument_class(assignment)
    return cls in _OVERNIGHT_ELIGIBLE_CLASSES


def _load_active_regime() -> str:
    """Load the active global regime from regime_state.json. Returns "" on error."""
    path = workspace_roots.ETA_REGIME_STATE_PATH
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("global_regime") or data.get("regime") or "").strip().lower()
    except (OSError, json.JSONDecodeError):
        return ""


def _lab_sharpe(assignment: dict[str, Any]) -> float | None:
    """Pull the most-recent lab-audit sharpe from a bot's extras stamps.

    Looks for keys like ``lab_audit_2026_05_04_round11`` first (highest-
    numbered), then any ``lab_audit_*``, then ``lab_promotion_*``. Returns
    None if no stamp present.
    """
    extras = assignment.get("extras", {})
    if not isinstance(extras, dict):
        return None
    candidates: list[tuple[str, dict]] = []
    for k, v in extras.items():
        if (
            isinstance(k, str)
            and isinstance(v, dict)
            and (k.startswith("lab_audit_") or k.startswith("lab_promotion_"))
        ):
            candidates.append((k, v))
    if not candidates:
        return None
    # Prefer the highest-sorted key (round11 > round10 > round7 > 2026-05-04).
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    for _, stamp in candidates:
        sharpe = stamp.get("sharpe")
        try:
            return float(sharpe) if sharpe is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _sharpe_to_size_factor(sharpe: float | None) -> float:
    """Map lab sharpe → size-cap multiplier (lower for unproven, full for tier-1)."""
    if sharpe is None:
        return 0.30  # untested
    if sharpe >= 2.0:
        return 1.00
    if sharpe >= 1.0:
        return 0.75
    if sharpe >= 0.5:
        return 0.50
    return 0.30


def _is_v23_enabled() -> bool:
    """Flag check — v23 runs only when explicitly enabled."""
    if os.environ.get("JARVIS_V3_FLEET_AWARE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        from eta_engine.brain.feature_flags import is_enabled as _ff_enabled

        return bool(_ff_enabled("JARVIS_V3_FLEET_AWARE"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Policy entry point
# ---------------------------------------------------------------------------


def evaluate_v23(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """Fleet-aware policy. Wraps v17 (or v22 if its flag is set).

    Adds three concurrent layers ON TOP of the wrapped policy:

      1. Regime-block veto — if the bot's `block_regimes` registry field
         contains the active global regime, return DEFERRED before the
         wrapped policy runs (cheap to compute, prevents wasted gate work).
      2. Class-derived overnight eligibility — if the wrapped policy
         denied with `overnight_refused` AND the bot is class-eligible
         (instrument_class in OVERNIGHT_ELIGIBLE), upgrade the verdict
         to APPROVED (or CONDITIONAL with size cap).
      3. Lab-sharpe sizing — scale `size_cap_mult` by `_sharpe_to_size_factor`
         derived from the bot's `lab_audit_*` stamps. Untested bots get
         0.30x; sharpe-2+ bots get 1.00x.

    Falls back to the wrapped verdict on any error.
    """
    # Choose the wrapped policy: v22 if its flag is on, else v17 champion.
    try:
        from eta_engine.brain.feature_flags import is_enabled as _ff_enabled

        sage_live = bool(_ff_enabled("V22_SAGE_MODULATION"))
    except Exception:  # noqa: BLE001
        sage_live = False

    if sage_live:
        try:
            from eta_engine.brain.jarvis_v3.policies.v22_sage_confluence import (
                evaluate_v22,
            )

            base_resp = evaluate_v22(req, ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("v23: v22 wrap failed (%s); falling back to v17", exc)
            base_resp = evaluate_request(req, ctx)
    else:
        base_resp = evaluate_request(req, ctx)

    # Resolve registry assignment. If we can't, return v17/v22 unchanged.
    assignment = _resolve_bot_assignment(req)
    if assignment is None:
        return base_resp

    # ---- Layer 1: regime-block veto ----
    block_regimes = assignment.get("block_regimes") or set()
    if block_regimes:
        active = _load_active_regime()
        # Don't override KILL or operator-only denials; only veto
        # when base verdict was approving or conditional.
        if (
            active
            and active in block_regimes
            and base_resp.verdict
            in (
                Verdict.APPROVED,
                Verdict.CONDITIONAL,
            )
        ):
            return base_resp.model_copy(
                update={
                    "verdict": Verdict.DEFERRED,
                    "reason": f"v23 regime-block: bot blocks regime '{active}'",
                    "reason_code": "v23_regime_blocked",
                    "conditions": (base_resp.conditions or [])
                    + [
                        f"blocked_regime={active}",
                        f"block_regimes={sorted(block_regimes)}",
                    ],
                }
            )

    # ---- Layer 2: class-derived overnight upgrade ----
    if (
        base_resp.verdict == Verdict.DENIED
        and base_resp.reason_code == "overnight_refused"
        and _is_overnight_eligible(assignment)
    ):
        # Upgrade to CONDITIONAL with sizing-hint cap (the wrapped policy
        # already validated that confluence pre-gate passed; this is just
        # restoring the legacy whitelist behavior for new bots).
        live_size = ctx.sizing_hint.size_mult if ctx.sizing_hint is not None else 1.0
        cls = _instrument_class(assignment)
        base_resp = base_resp.model_copy(
            update={
                "verdict": Verdict.CONDITIONAL,
                "reason": f"v23 class-eligible overnight ({cls}); confluence pre-validated",
                "reason_code": "v23_overnight_class_eligible",
                "conditions": [f"size_mult<={live_size:.4f}", f"instrument_class={cls}"],
                "size_cap_mult": min(live_size, 0.75),  # tighter than RTH
            }
        )

    # ---- Layer 3: lab-sharpe sizing ----
    if base_resp.verdict in (Verdict.APPROVED, Verdict.CONDITIONAL):
        sharpe = _lab_sharpe(assignment)
        factor = _sharpe_to_size_factor(sharpe)
        # Compose with whatever cap the base set (multiplicative). If base
        # had no cap, set one based on factor alone.
        existing_cap = base_resp.size_cap_mult
        new_cap = factor if existing_cap is None else min(existing_cap, factor)
        if existing_cap is None or abs(existing_cap - new_cap) > 1e-9:
            extra_conds = [
                f"v23_lab_sharpe={sharpe:.3f}" if sharpe is not None else "v23_lab_sharpe=untested",
                f"v23_size_factor={factor:.2f}",
            ]
            base_resp = base_resp.model_copy(
                update={
                    "size_cap_mult": new_cap,
                    "conditions": (base_resp.conditions or []) + extra_conds,
                }
            )

    return base_resp
