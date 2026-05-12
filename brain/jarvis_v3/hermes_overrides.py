"""
JARVIS v3 // hermes_overrides
=============================
Operator-pinned overrides written by Hermes Agent's write-back tools.

Two surfaces share this sidecar:

* **size_modifier per bot** — read by ``portfolio_brain.assess()`` as a
  final multiplicative tweak applied AFTER all rule-based modifiers and
  AFTER the standard clamp. Operator says "trim atr_breakout_mnq to
  0.6x for the rest of the session"; portfolio_brain honors it until the
  override expires.

* **school_weight per (asset, school)** — read by
  ``hot_learner.current_weights()`` as a multiplicative overlay. Operator
  says "boost momentum school for MNQ by 20% today"; hot_learner applies
  the overlay on top of whatever the EMA-learner has converged to.

Design rules
------------
1. Every override has a hard ``expires_at`` UTC timestamp. Readers MUST
   treat expired entries as not-present. No infinite pins.
2. Magnitude is hard-clamped at write time. The MCP tool sanitizes
   inputs; the reader trusts the sidecar but re-clamps anyway.
3. ``apply_*()`` MUST NEVER raise on write failure (best-effort like
   trace_emitter.emit). ``get_*()`` MUST NEVER raise on read failure;
   return the "not overridden" default and log a warning.
4. Atomic write via temp-file rename to avoid partial-state reads from
   the supervisor running concurrently.
5. The sidecar lives at
   ``var/eta_engine/state/hermes_overrides.json`` per CLAUDE.md hard
   rule #1 (single canonical write target).

Module layout intentionally mirrors ``trace_emitter`` and
``hot_learner``: a small dataclass for the on-disk shape, an internal
``_load()/_save()`` pair, and a tight ``apply_*/get_*`` public API.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.hermes_overrides")

DEFAULT_OVERRIDES_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_overrides.json",
)

# Clamp ranges — readers re-apply these even if writer didn't.
# Size write-backs are intentionally de-risk-only for prop-fund discipline.
_SIZE_MOD_LOW, _SIZE_MOD_HIGH = 0.0, 1.0
_SCHOOL_WEIGHT_LOW, _SCHOOL_WEIGHT_HIGH = 0.0, 2.0

# Default TTL when caller doesn't pin one. 4 hours covers a typical
# operator-attended session without becoming a foot-gun.
_DEFAULT_TTL_MINUTES = 240

EXPECTED_HOOKS = (
    "apply_size_modifier",
    "get_size_modifier",
    "apply_school_weight",
    "get_school_weights",
    "active_overrides_summary",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: Any) -> datetime | None:  # noqa: ANN401 — best-effort
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _resolve_path(path: Path | None) -> Path:
    return Path(path) if path is not None else DEFAULT_OVERRIDES_PATH


def _empty_sidecar() -> dict[str, Any]:
    """The on-disk shape when no overrides have been written yet."""
    return {
        "_doc": (
            "Operator-pinned overrides from Hermes Agent write-back tools. "
            "Entries with expires_at <= now are ignored by readers; remove "
            "by hand or wait for TTL to elapse."
        ),
        "size_modifiers": {},
        "school_weights": {},
    }


def _load(path: Path | None = None) -> dict[str, Any]:
    """Best-effort read. Returns the empty shape on any failure."""
    target = _resolve_path(path)
    if not target.exists():
        return _empty_sidecar()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_sidecar()
        # Backfill any keys the writer might have produced before this
        # schema existed, so newer readers stay happy with older files.
        data.setdefault("size_modifiers", {})
        data.setdefault("school_weights", {})
        if not isinstance(data["size_modifiers"], dict):
            data["size_modifiers"] = {}
        if not isinstance(data["school_weights"], dict):
            data["school_weights"] = {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("hermes_overrides _load failed: %s", exc)
        return _empty_sidecar()


def _atomic_write(target: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via temp-file + os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, default=str)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp_hermes_overrides_",
        suffix=".json",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp_name, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _save(data: dict[str, Any], path: Path | None = None) -> bool:
    """Best-effort write. Returns True on success, False on failure (does NOT raise)."""
    target = _resolve_path(path)
    try:
        _atomic_write(target, data)
        return True
    except OSError as exc:
        logger.warning("hermes_overrides _save failed: %s", exc)
        return False


def _is_active(entry: dict[str, Any], now: datetime | None = None) -> bool:
    """True iff the entry has a future ``expires_at``."""
    if not isinstance(entry, dict):
        return False
    expires_at = _parse_iso(entry.get("expires_at"))
    if expires_at is None:
        return False
    return expires_at > (now or _now())


# ---------------------------------------------------------------------------
# Public API — size_modifier
# ---------------------------------------------------------------------------


def apply_size_modifier(
    bot_id: str,
    modifier: float,
    reason: str,
    ttl_minutes: int = _DEFAULT_TTL_MINUTES,
    source: str = "hermes_mcp",
    path: Path | None = None,
) -> dict[str, Any]:
    """Pin ``modifier`` for ``bot_id`` until ``ttl_minutes`` from now expires.

    Returns the persisted record (with applied/expires timestamps and the
    clamped modifier). On write failure logs and returns the record dict
    anyway — caller treats absence in subsequent ``get_*`` as the failure
    signal.
    """
    if not bot_id:
        return {"status": "REJECTED", "reason": "missing_bot_id"}
    try:
        mod = float(modifier)
    except (TypeError, ValueError):
        return {"status": "REJECTED", "reason": "modifier_not_numeric"}
    mod = _clamp(mod, _SIZE_MOD_LOW, _SIZE_MOD_HIGH)
    if ttl_minutes <= 0:
        ttl_minutes = _DEFAULT_TTL_MINUTES

    now = _now()
    expires = now + timedelta(minutes=ttl_minutes)
    entry = {
        "modifier": mod,
        "reason": str(reason),
        "applied_at": _iso(now),
        "expires_at": _iso(expires),
        "source": str(source),
    }

    data = _load(path)
    data["size_modifiers"][bot_id] = entry
    ok = _save(data, path)
    return {
        "status": "APPLIED" if ok else "WRITE_FAILED",
        "bot_id": bot_id,
        **entry,
    }


def get_size_modifier(
    bot_id: str, now: datetime | None = None, path: Path | None = None,
) -> float | None:
    """Return the active modifier for ``bot_id`` or ``None`` if no live pin.

    Readers should treat ``None`` as "no Hermes override applies" and
    proceed with whatever modifier the rule cascade produced. NEVER raises.
    """
    try:
        data = _load(path)
        entry = data.get("size_modifiers", {}).get(bot_id)
        if not _is_active(entry, now):
            return None
        try:
            mod = float(entry.get("modifier", 1.0))
        except (TypeError, ValueError):
            return None
        return _clamp(mod, _SIZE_MOD_LOW, _SIZE_MOD_HIGH)
    except Exception as exc:  # noqa: BLE001 — read path never raises
        logger.warning("hermes_overrides.get_size_modifier failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API — school_weight
# ---------------------------------------------------------------------------


def apply_school_weight(
    asset: str,
    school: str,
    weight: float,
    reason: str,
    ttl_minutes: int = _DEFAULT_TTL_MINUTES,
    source: str = "hermes_mcp",
    path: Path | None = None,
) -> dict[str, Any]:
    """Pin ``weight`` for ``(asset, school)`` until ``ttl_minutes`` elapse.

    Stored shape mirrors hot_learner's nested dict: ``school_weights[asset][school]``.
    """
    if not asset or not school:
        return {"status": "REJECTED", "reason": "missing_asset_or_school"}
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return {"status": "REJECTED", "reason": "weight_not_numeric"}
    w = _clamp(w, _SCHOOL_WEIGHT_LOW, _SCHOOL_WEIGHT_HIGH)
    if ttl_minutes <= 0:
        ttl_minutes = _DEFAULT_TTL_MINUTES

    now = _now()
    expires = now + timedelta(minutes=ttl_minutes)
    entry = {
        "weight": w,
        "reason": str(reason),
        "applied_at": _iso(now),
        "expires_at": _iso(expires),
        "source": str(source),
    }

    data = _load(path)
    asset_bucket = data["school_weights"].setdefault(asset, {})
    if not isinstance(asset_bucket, dict):
        asset_bucket = {}
        data["school_weights"][asset] = asset_bucket
    asset_bucket[school] = entry
    ok = _save(data, path)
    return {
        "status": "APPLIED" if ok else "WRITE_FAILED",
        "asset": asset,
        "school": school,
        **entry,
    }


def get_school_weights(
    asset: str, now: datetime | None = None, path: Path | None = None,
) -> dict[str, float]:
    """Return ``{school: weight}`` for the asset, filtered to live entries.

    Empty dict when no overrides apply. NEVER raises.
    """
    try:
        data = _load(path)
        bucket = data.get("school_weights", {}).get(asset, {})
        if not isinstance(bucket, dict):
            return {}
        out: dict[str, float] = {}
        for school, entry in bucket.items():
            if not _is_active(entry, now):
                continue
            try:
                w = float(entry.get("weight", 1.0))
            except (TypeError, ValueError):
                continue
            out[school] = _clamp(w, _SCHOOL_WEIGHT_LOW, _SCHOOL_WEIGHT_HIGH)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_overrides.get_school_weights failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Operator-facing summary
# ---------------------------------------------------------------------------


def active_overrides_summary(
    now: datetime | None = None, path: Path | None = None,
) -> dict[str, Any]:
    """Compact dict of currently-active overrides + their expiry timestamps.

    Powered into the ``jarvis_active_overrides`` MCP tool so Hermes can
    show the operator "what's currently pinned" without scraping the
    sidecar manually.
    """
    now_dt = now or _now()
    try:
        data = _load(path)
        active_sizes: dict[str, dict[str, Any]] = {}
        for bot_id, entry in data.get("size_modifiers", {}).items():
            if _is_active(entry, now_dt):
                active_sizes[bot_id] = entry
        active_schools: dict[str, dict[str, dict[str, Any]]] = {}
        for asset, bucket in data.get("school_weights", {}).items():
            if not isinstance(bucket, dict):
                continue
            live = {
                school: entry
                for school, entry in bucket.items()
                if _is_active(entry, now_dt)
            }
            if live:
                active_schools[asset] = live
        return {
            "size_modifiers": active_sizes,
            "school_weights": active_schools,
            "asof": _iso(now_dt),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_overrides.active_overrides_summary failed: %s", exc)
        return {"size_modifiers": {}, "school_weights": {}, "asof": _iso(now_dt)}


def clear_override(
    *,
    bot_id: str | None = None,
    asset: str | None = None,
    school: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Remove a single override entry (operator manual clear).

    Pass exactly one of:
      * ``bot_id`` — clears size_modifiers[bot_id]
      * ``asset`` + ``school`` — clears school_weights[asset][school]

    Returns ``{"status": "REMOVED"|"NOT_FOUND"|"REJECTED", ...}``.
    """
    if bot_id and not asset and not school:
        data = _load(path)
        sizes = data.get("size_modifiers", {})
        if bot_id not in sizes:
            return {"status": "NOT_FOUND", "kind": "size_modifier", "bot_id": bot_id}
        sizes.pop(bot_id, None)
        _save(data, path)
        return {"status": "REMOVED", "kind": "size_modifier", "bot_id": bot_id}
    if asset and school and not bot_id:
        data = _load(path)
        bucket = data.get("school_weights", {}).get(asset, {})
        if school not in bucket:
            return {
                "status": "NOT_FOUND",
                "kind": "school_weight",
                "asset": asset,
                "school": school,
            }
        bucket.pop(school, None)
        if not bucket:
            data["school_weights"].pop(asset, None)
        _save(data, path)
        return {
            "status": "REMOVED",
            "kind": "school_weight",
            "asset": asset,
            "school": school,
        }
    return {"status": "REJECTED", "reason": "ambiguous_arguments"}
