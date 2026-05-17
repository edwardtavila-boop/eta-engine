"""Portfolio Brain (Stream 1 of JARVIS Supercharge, 2026-05-11).

Per-bot JARVIS consults are blind to joint-fleet exposure: each bot
sees only its own R but the *fleet* may already be heavy in BTC,
deep into the daily-drawdown bucket, or piled into a high-correlation
cluster. The Portfolio Brain wraps those signals into a single
``PortfolioContext`` and applies five rules in ``assess()`` to yield
a ``PortfolioVerdict`` -- a multiplicative ``size_modifier`` plus an
optional hard-block ``block_reason``.

This module is *read-only* with respect to fleet state. It NEVER
crashes the consult path: every external lookup is wrapped in
``_safe_import``/``_safe_call`` so missing siblings fall back to a
default. The conductor calls ``snapshot()`` once per consult, then
``assess()`` to produce the verdict.

Rules (applied in order, accumulating):

1. ``fleet_kill_active`` -> hard block, modifier 0.0.
2. ``portfolio_drawdown_today_r < -2.0`` -> multiply by 0.5.
3. Same-asset notional > 30k -> multiply by 0.7.
4. ``open_correlated_exposure > 0.75`` -> multiply by 0.6.
5. Clamp modifier to [0.0, 1.5].
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType  # noqa: TC003 -- used at runtime in helpers
from typing import Any

from eta_engine.scripts import workspace_roots

logger = logging.getLogger("eta_engine.portfolio_brain")

EXPECTED_HOOKS = ("assess", "snapshot")

# Used as the budget sentinel if the budget module isn't reachable.
_BUDGET_SENTINEL = 1e9

# Fallback fleet-state JSON path (used when fleet_allocator.current_exposure
# is not available at runtime).
_FLEET_STATE_PATH = workspace_roots.ETA_FLEET_STATE_PATH

# Rule thresholds (kept module-level for easy operator override + testing).
_DRAWDOWN_TIGHTEN_R = -2.0
_DRAWDOWN_MODIFIER = 0.5
_SAME_ASSET_NOTIONAL_LIMIT = 30_000.0
_SAME_ASSET_MODIFIER = 0.7
_CORRELATION_CLUSTER_LIMIT = 0.75
_CORRELATION_CLUSTER_MODIFIER = 0.6
_MODIFIER_CAP_LOW = 0.0
_MODIFIER_CAP_HIGH = 1.5


@dataclass(frozen=True)
class PortfolioContext:
    """Immutable fleet-wide snapshot fed into ``assess()``."""

    fleet_long_notional_by_asset: dict[str, float]
    fleet_short_notional_by_asset: dict[str, float]
    recent_entries_by_asset: dict[str, int]
    open_correlated_exposure: float  # 0..1
    portfolio_drawdown_today_r: float  # negative when in drawdown
    fleet_kill_active: bool


@dataclass(frozen=True)
class PortfolioVerdict:
    """Result of evaluating a request against the current portfolio."""

    size_modifier: float  # multiplicative, clamped [0.0, 1.5]
    block_reason: str | None
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def assess(req: Any, ctx: PortfolioContext) -> PortfolioVerdict:  # noqa: ANN401
    """Run the rule cascade against ``ctx`` for the given request."""
    notes: list[str] = []

    # Rule 1: hard kill switch short-circuits everything.
    if ctx.fleet_kill_active:
        return PortfolioVerdict(
            size_modifier=0.0,
            block_reason="fleet_kill_active",
            notes=("fleet_kill_active",),
        )

    modifier = 1.0

    # Rule 2: drawdown tighten.
    if ctx.portfolio_drawdown_today_r < _DRAWDOWN_TIGHTEN_R:
        modifier *= _DRAWDOWN_MODIFIER
        notes.append(f"drawdown_tighten: {ctx.portfolio_drawdown_today_r:.1f}R")

    # Rule 3: same-asset notional concentration.
    asset_key = _extract_asset_key(req)
    if asset_key is not None:
        same_asset_notional = ctx.fleet_long_notional_by_asset.get(asset_key, 0.0)
        if same_asset_notional > _SAME_ASSET_NOTIONAL_LIMIT:
            modifier *= _SAME_ASSET_MODIFIER
            notes.append(f"correlated_exposure_${int(same_asset_notional / 1000)}k")

    # Rule 4: macro correlation cluster.
    if ctx.open_correlated_exposure > _CORRELATION_CLUSTER_LIMIT:
        modifier *= _CORRELATION_CLUSTER_MODIFIER
        notes.append("correlation_cluster_high")

    # Rule 5: clamp (rule-based result).
    modifier = max(_MODIFIER_CAP_LOW, min(_MODIFIER_CAP_HIGH, modifier))

    # Rule 6: operator override via Hermes (Track 2 write-back). Applied
    # AFTER the rule cascade so the operator can intentionally trim BELOW
    # what the cascade produced (e.g. "trim everything to 0.5x for the
    # next session because I don't trust the regime"). Override has its
    # own clamp inside hermes_overrides.get_size_modifier(). NEVER raises.
    try:
        from eta_engine.brain.jarvis_v3 import hermes_overrides

        bot_id = getattr(req, "bot_id", "") or ""
        op_override = hermes_overrides.get_size_modifier(bot_id) if bot_id else None
        if op_override is not None:
            # Multiplicative so it composes with the cascade. e.g. cascade
            # said 1.0 and operator pinned 0.5 → 0.5 final. Cascade said
            # 0.7 (drawdown-tightened) and operator pinned 0.5 → 0.35 final.
            modifier = max(
                _MODIFIER_CAP_LOW,
                min(_MODIFIER_CAP_HIGH, modifier * op_override),
            )
            notes.append(f"hermes_size_override:{op_override:.2f}")
    except Exception:  # noqa: BLE001 — override read MUST NOT break consult
        pass

    return PortfolioVerdict(
        size_modifier=modifier,
        block_reason=None,
        notes=tuple(notes),
    )


def snapshot() -> PortfolioContext:
    """Build a fresh ``PortfolioContext`` from fleet-wide state.

    Best-effort: every external read is wrapped so missing modules /
    malformed state never crash the consult path. Defaults to a
    neutral context when every signal is unreachable.
    """
    long_by_asset: dict[str, float] = {}
    short_by_asset: dict[str, float] = {}
    recent_by_asset: dict[str, int] = {}
    open_corr = 0.0
    drawdown_r = 0.0
    fleet_kill = False

    # Fleet allocator wire ------------------------------------------------
    try:
        fleet_mod = _safe_import("eta_engine.brain.jarvis_v3.fleet_allocator")
    except Exception as exc:  # noqa: BLE001
        logger.debug("portfolio_brain: fleet_allocator import errored: %s", exc)
        fleet_mod = None
    if fleet_mod is not None:
        exposure = _safe_call(fleet_mod, "current_exposure")
        if isinstance(exposure, dict):
            long_by_asset = _coerce_float_dict(exposure.get("long_notional_by_asset", {}))
            short_by_asset = _coerce_float_dict(exposure.get("short_notional_by_asset", {}))
            recent_by_asset = _coerce_int_dict(exposure.get("recent_entries_by_asset", {}))

    # Fleet-state JSON fallback when fleet_allocator doesn't expose
    # current_exposure (or both notional dicts came back empty).
    if not long_by_asset and not short_by_asset:
        fleet_state = _load_fleet_state_json()
        if fleet_state:
            long_by_asset = _coerce_float_dict(fleet_state.get("long_notional_by_asset", {}))
            short_by_asset = _coerce_float_dict(fleet_state.get("short_notional_by_asset", {}))
            recent_by_asset = _coerce_int_dict(fleet_state.get("recent_entries_by_asset", {}))

    # Correlation regime detector -> open_correlated_exposure ----------------
    try:
        corr_mod = _safe_import("eta_engine.brain.jarvis_v3.corr_regime_detector")
    except Exception as exc:  # noqa: BLE001
        logger.debug("portfolio_brain: corr_regime_detector import errored: %s", exc)
        corr_mod = None
    if corr_mod is not None:
        open_corr = _compute_open_correlation(corr_mod)

    # Budget / drawdown / kill-switch wire -----------------------------------
    try:
        budget_mod = _safe_import("eta_engine.brain.jarvis_v3.budget")
    except Exception as exc:  # noqa: BLE001
        logger.debug("portfolio_brain: budget import errored: %s", exc)
        budget_mod = None
    if budget_mod is not None:
        budget_remaining = _safe_call(budget_mod, "risk_budget_remaining_today_r")
        if budget_remaining is None:
            budget_remaining = _BUDGET_SENTINEL
        # ``risk_budget_remaining_today_r`` is informational; we don't
        # currently derive drawdown from it (operator policy lives in the
        # budget module). We only use it to keep the wire in place for
        # downstream tooling / trace records.
        _ = budget_remaining
        explicit_dd = _safe_call(budget_mod, "portfolio_drawdown_today_r")
        if isinstance(explicit_dd, int | float):
            drawdown_r = float(explicit_dd)
        fleet_kill_flag = _safe_call(budget_mod, "fleet_kill_active")
        if isinstance(fleet_kill_flag, bool):
            fleet_kill = fleet_kill_flag

    # Firm-board consensus check is read-only -- we log it via notes when
    # available so the conductor can attach it to the trace, but it does
    # not currently drive size modulation here.
    try:
        firm_mod = _safe_import("eta_engine.brain.jarvis_v3.firm_board")
    except Exception as exc:  # noqa: BLE001
        logger.debug("portfolio_brain: firm_board import errored: %s", exc)
        firm_mod = None
    _ = firm_mod  # presence-only check; consumed by trace surface

    return PortfolioContext(
        fleet_long_notional_by_asset=long_by_asset,
        fleet_short_notional_by_asset=short_by_asset,
        recent_entries_by_asset=recent_by_asset,
        open_correlated_exposure=open_corr,
        portfolio_drawdown_today_r=drawdown_r,
        fleet_kill_active=fleet_kill,
    )


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _safe_import(module_name: str) -> ModuleType | None:
    """Import a module, swallowing every failure (returns None)."""
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 -- intentional best-effort
        logger.debug("portfolio_brain: import failed for %s: %s", module_name, exc)
        return None


def _safe_call(
    module: ModuleType,
    attr: str,
    *args: Any,  # noqa: ANN401 -- intentional pass-through
    **kwargs: Any,  # noqa: ANN401 -- intentional pass-through
) -> Any:  # noqa: ANN401 -- caller checks the return shape defensively
    """Call ``module.attr(...)`` if present and callable; swallow errors."""
    func = getattr(module, attr, None)
    if func is None:
        return None
    if not callable(func):
        return func
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "portfolio_brain: %s.%s call raised: %s",
            module.__name__,
            attr,
            exc,
        )
        return None


def _extract_asset_key(req: Any) -> str | None:  # noqa: ANN401
    """Pull the asset key (asset_class / asset / symbol) from a request."""
    for attr in ("asset_class", "asset", "symbol"):
        val = getattr(req, attr, None)
        if isinstance(val, str) and val:
            return val
    return None


def _coerce_float_dict(raw: Any) -> dict[str, float]:  # noqa: ANN401
    """Coerce an arbitrary mapping into ``dict[str, float]`` defensively."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _coerce_int_dict(raw: Any) -> dict[str, int]:  # noqa: ANN401
    """Coerce an arbitrary mapping into ``dict[str, int]`` defensively."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _load_fleet_state_json() -> dict[str, Any]:
    """Load the fleet-state JSON fallback file."""
    try:
        if not _FLEET_STATE_PATH.exists():
            return {}
        with _FLEET_STATE_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("portfolio_brain: fleet_state.json read failed: %s", exc)
    return {}


def _compute_open_correlation(corr_mod: ModuleType) -> float:
    """Derive a single 0..1 'open correlated exposure' from corr detector.

    We use whatever public surface the module offers, in priority order:
      1. ``open_correlated_exposure()`` -- explicit pre-computed value
      2. ``detect_shifts(...)`` -- worst recent severity, mapped to 0..1
    Falls back to 0.0 when nothing is reachable.
    """
    explicit = _safe_call(corr_mod, "open_correlated_exposure")
    if isinstance(explicit, int | float):
        return max(0.0, min(1.0, float(explicit)))

    # Best-effort: compare baseline vs rolling correlations if helpers exist.
    rolling = _safe_call(corr_mod, "load_rolling")
    baseline = _safe_call(corr_mod, "load_baseline")
    if isinstance(rolling, dict) and isinstance(baseline, dict):
        shifts = _safe_call(corr_mod, "detect_shifts", rolling, baseline)
        if isinstance(shifts, list) and shifts:
            severity_rank = {"minor": 0.3, "material": 0.6, "extreme": 0.9}
            worst = max(severity_rank.get(getattr(s, "severity", "minor"), 0.0) for s in shifts)
            return worst
    return 0.0
