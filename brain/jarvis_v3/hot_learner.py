"""
JARVIS v3 // hot_learner
========================
Within-session per-school weight adaptation.

This module tracks how each "school" (Wyckoff, order_flow, etc.) is performing
on closed trades and emits a small multiplicative weight modifier per asset
class that the conductor can blend into downstream consults.

Update rule (inside `observe_close`):
    For each (school, attribution) pair attached to a closed trade:
        signed_reward = attribution * r_outcome
            (positive when the school voted with the winning side,
             negative when it voted against)
        delta = 0.05 * signed_reward                # 5% step per unit-R
        new = clamp(current + delta, CAP_LOW, CAP_HIGH)

Per-asset segmentation: BTC observations never affect MNQ weights, etc.

Gating: a school's weight only surfaces from `current_weights()` once we
have observed it at least `MIN_OBSERVATIONS_TO_ACT` times for that asset.
This prevents the conductor from acting on a single noisy outcome.

Overnight decay (called from kaizen_loop): every weight is pulled back
toward 1.0 by `new = DECAY_RATIO * old + (1 - DECAY_RATIO) * 1.0`. This
keeps the hot learner truly "within-session" — yesterday's pattern fades
unless it reasserts itself today.

Persistence is plain JSON at `STATE_PATH`. A missing or malformed file
yields a default `HotLearnState()` rather than raising; callers in the
live consult path must never crash because the learner's disk state was
truncated mid-write.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATE_PATH = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hot_learner.json")

MIN_OBSERVATIONS_TO_ACT = 3
CAP_LOW = 0.5
CAP_HIGH = 1.5
DECAY_RATIO = 0.7  # new = DECAY_RATIO * old + (1 - DECAY_RATIO) * 1.0

_STEP_SIZE = 0.05  # 5% step per unit signed_reward in observe_close

logger = logging.getLogger("eta_engine.hot_learner")

EXPECTED_HOOKS = ("observe_close", "current_weights", "decay_overnight")


@dataclass
class HotLearnState:
    """Persisted state for the hot learner.

    weight_mods:
        Outer key: asset class (e.g. "BTC", "MNQ").
        Inner key: school name.
        Value: multiplier in [CAP_LOW, CAP_HIGH], default 1.0.
    n_closes_today:
        Count of closed trades observed since the last overnight decay.
    last_decay_ts:
        ISO timestamp of the last `decay_overnight()` call.
    obs_count_by_school:
        Per-asset-per-school observation count keyed by f"{asset}:{school}".
        Used to gate `current_weights()` against MIN_OBSERVATIONS_TO_ACT.
    """

    weight_mods: dict[str, dict[str, float]] = field(default_factory=dict)
    n_closes_today: int = 0
    last_decay_ts: str = ""
    obs_count_by_school: dict[str, int] = field(default_factory=dict)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _load() -> HotLearnState:
    """Load state from `STATE_PATH`. Missing or malformed → default state."""
    try:
        if not STATE_PATH.exists():
            return HotLearnState()
        raw = STATE_PATH.read_text(encoding="utf-8")
        if not raw.strip():
            return HotLearnState()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return HotLearnState()
        weight_mods_raw = data.get("weight_mods", {})
        weight_mods: dict[str, dict[str, float]] = {}
        if isinstance(weight_mods_raw, dict):
            for asset, inner in weight_mods_raw.items():
                if not isinstance(asset, str) or not isinstance(inner, dict):
                    continue
                cleaned: dict[str, float] = {}
                for school, weight in inner.items():
                    if isinstance(school, str) and isinstance(weight, (int, float)):
                        cleaned[school] = float(weight)
                weight_mods[asset] = cleaned

        obs_count_raw = data.get("obs_count_by_school", {})
        obs_count: dict[str, int] = {}
        if isinstance(obs_count_raw, dict):
            for key, count in obs_count_raw.items():
                if isinstance(key, str) and isinstance(count, (int, float)):
                    obs_count[key] = int(count)

        n_closes_today_raw = data.get("n_closes_today", 0)
        n_closes_today = int(n_closes_today_raw) if isinstance(n_closes_today_raw, (int, float)) else 0

        last_decay_ts_raw = data.get("last_decay_ts", "")
        last_decay_ts = last_decay_ts_raw if isinstance(last_decay_ts_raw, str) else ""

        return HotLearnState(
            weight_mods=weight_mods,
            n_closes_today=n_closes_today,
            last_decay_ts=last_decay_ts,
            obs_count_by_school=obs_count,
        )
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        logger.warning("hot_learner._load: malformed state, returning default (%s)", exc)
        return HotLearnState()


def _save(state: HotLearnState) -> None:
    """Persist state to `STATE_PATH`. Best-effort — failures logged, never raised."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(state), indent=2, sort_keys=True)
        STATE_PATH.write_text(payload, encoding="utf-8")
    except OSError as exc:
        logger.warning("hot_learner._save failed: %s", exc)


def observe_close(asset: str, school_attribution: dict[str, float], r_outcome: float) -> None:
    """Update weight_mods based on a closed trade.

    Args:
        asset: asset class (e.g. "BTC", "MNQ").
        school_attribution: {school_name: attribution in [-1, 1]} where positive
            means the school voted for the winning side, negative means it
            voted against.
        r_outcome: signed R outcome of the trade. Positive = winner.
    """
    state = _load()
    asset_weights = state.weight_mods.setdefault(asset, {})

    for school, attribution in school_attribution.items():
        try:
            attribution_f = float(attribution)
        except (TypeError, ValueError):
            continue
        try:
            r_outcome_f = float(r_outcome)
        except (TypeError, ValueError):
            continue

        signed_reward = attribution_f * r_outcome_f
        delta = _STEP_SIZE * signed_reward
        current = asset_weights.get(school, 1.0)
        new = _clamp(current + delta, CAP_LOW, CAP_HIGH)
        asset_weights[school] = new

        key = f"{asset}:{school}"
        state.obs_count_by_school[key] = state.obs_count_by_school.get(key, 0) + 1

    state.n_closes_today += 1
    _save(state)


def current_weights(asset: str) -> dict[str, float]:
    """Return current weight_mods for `asset`, gated by MIN_OBSERVATIONS_TO_ACT.

    Schools that haven't been observed enough times for this asset are omitted.
    Returns an empty dict when no state exists or no school has crossed the gate.
    """
    state = _load()
    asset_weights = state.weight_mods.get(asset, {})
    if not asset_weights:
        return {}
    out: dict[str, float] = {}
    for school, weight in asset_weights.items():
        key = f"{asset}:{school}"
        if state.obs_count_by_school.get(key, 0) >= MIN_OBSERVATIONS_TO_ACT:
            out[school] = weight
    return out


def decay_overnight() -> None:
    """Decay every weight toward yesterday's snapshot (or 1.0) and reset
    per-session counters.

    Hermes Bridge Site C: before applying the decay, try to recall
    yesterday's persisted weights from Hermes Agent's memory provider.
    If a snapshot is found, weights decay toward THAT instead of 1.0 —
    so a school that's been reliably contributing across days keeps a
    modest persistent boost instead of getting reset every midnight.
    When Hermes is unreachable / backoff-active / the key doesn't exist
    yet, the legacy mean-revert-to-1.0 behavior is preserved exactly.

    After decay, today's weights are persisted (best-effort) so tomorrow
    has something to recall.

    Formula: new = DECAY_RATIO * old + (1 - DECAY_RATIO) * target
    where target = 1.0 by default, or schools[school] from Hermes memory
    if recall succeeded.

    Side effects: clears ``n_closes_today`` and ``obs_count_by_school``,
    updates ``last_decay_ts``, persists local state, attempts to persist
    each asset's weights to Hermes memory under key ``hot_weights_<asset>``.
    """
    state = _load()

    # Site C — recall yesterday's snapshot per-asset (best-effort)
    target_by_asset: dict[str, dict[str, float]] = {}
    try:
        from eta_engine.brain.jarvis_v3 import hermes_client
        for asset in state.weight_mods:
            recall = hermes_client.memory_recall(
                key=f"hot_weights_{asset}", timeout_s=1.0,
            )
            if recall.ok and isinstance(recall.data, dict):
                target_by_asset[asset] = {
                    str(k): float(v)
                    for k, v in recall.data.items()
                    if isinstance(v, (int, float))
                }
    except Exception:  # noqa: BLE001 — Hermes-down → fall through to 1.0 target
        target_by_asset = {}

    # Apply decay toward target (yesterday's snapshot or 1.0)
    for asset, schools in state.weight_mods.items():
        anchor = target_by_asset.get(asset, {})
        for school, weight in list(schools.items()):
            tgt = anchor.get(school, 1.0)
            new = DECAY_RATIO * weight + (1.0 - DECAY_RATIO) * tgt
            schools[school] = _clamp(new, CAP_LOW, CAP_HIGH)
        state.weight_mods[asset] = schools
    state.n_closes_today = 0
    state.obs_count_by_school = {}
    state.last_decay_ts = datetime.now(UTC).isoformat()
    _save(state)

    # Site C — persist today's snapshot for tomorrow's recall (best-effort)
    try:
        from eta_engine.brain.jarvis_v3 import hermes_client
        for asset, schools in state.weight_mods.items():
            hermes_client.memory_persist(
                key=f"hot_weights_{asset}",
                value=dict(schools),
                timeout_s=1.0,
            )
    except Exception:  # noqa: BLE001 — Hermes-down → next decay will use 1.0
        pass
