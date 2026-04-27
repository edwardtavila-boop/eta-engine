"""EVOLUTIONARY TRADING ALGO  //  strategies.regime_exclusion.

Source of truth for which regimes the live policy MUST refuse to size
into, with the reason recorded next to the decision.

Why this module exists
----------------------
``scripts/run_cross_regime_validation.py`` runs an IS/OOS split per
synthetic regime (TRENDING / RANGING / HIGH_VOL / LOW_VOL). The
2026-04-17 run found:

    HIGH_VOL  IS +0.216R  ->  OOS -0.559R   (sign flip, deg +358.7%)

Sign flips on a regime where IS was already tradeable is the textbook
overfit signature. The validation report's verdict was literally
*"exclude this regime"*. This module operationalises that decision.

Behaviour
---------
* ``is_regime_excluded(label)`` returns ``True`` if the live policy
  must zero risk for the given regime.
* The exclusion set is loaded from
  ``docs/cross_regime/regime_exclusions.json`` if present, else falls
  back to a hard-coded default that includes the OOS-validated
  exclusions.
* ``ExclusionDecision.reason`` carries the human-readable cause so the
  decision journal can record *why* a strategy abstained.
* The loader is cheap; cache invalidation is by file mtime so the
  user can edit the JSON and the next call sees the new value without
  a restart.

Design constraints
------------------
* Pure stdlib. No pydantic. Strategies must stay import-cheap.
* No exceptions on a missing/corrupt config -- always fall through to
  the hard-coded default, with a single-line stderr warning.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class ExclusionDecision:
    """Outcome of an exclusion check."""

    excluded: bool
    reason: str

    def __bool__(self) -> bool:
        return self.excluded


# ---------------------------------------------------------------------------
# Hard-coded defaults -- match the 2026-04-17 cross_regime FAIL verdict
# ---------------------------------------------------------------------------


_DEFAULT_EXCLUSIONS: Final[dict[str, str]] = {
    "HIGH_VOL": (
        "OOS sign-flip: IS +0.216R -> OOS -0.559R "
        "(cross_regime_validation 2026-04-17). "
        "Re-enable only after re-validation passes "
        "(deg <= 60% AND OOS exp >= 0.15R AND OOS trades >= 20)."
    ),
    "CRISIS": (
        "Crisis regime: macro=crisis or vol>0.85+liquidity<0.2. "
        "Spreads blow out, fills become unmodellable. "
        "No live strategy is permitted to size into this regime."
    ),
}
"""Regime-label -> reason. Matches existing ``_risk_mult`` behaviour
for CRISIS (already zero) and adds HIGH_VOL per the OOS verdict."""


_CONFIG_PATH: Final[Path] = Path(__file__).resolve().parents[1] / "docs" / "cross_regime" / "regime_exclusions.json"


# ---------------------------------------------------------------------------
# mtime-keyed cache so edits to the JSON are picked up live
# ---------------------------------------------------------------------------


_lock = threading.Lock()
_cached_mtime: float | None = None
_cached_payload: dict[str, str] | None = None


def _load_from_disk() -> dict[str, str]:
    """Read the JSON config; on ANY error fall back to defaults."""
    global _cached_mtime, _cached_payload  # noqa: PLW0603
    if not _CONFIG_PATH.exists():
        return dict(_DEFAULT_EXCLUSIONS)
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
        if _cached_payload is not None and _cached_mtime == mtime:
            return dict(_cached_payload)
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        raw = json.loads(text)
        # Accept either {"excluded_regimes": {label: reason}} or flat
        # {label: reason}; both legitimate so the user can hand-edit.
        payload = raw["excluded_regimes"] if isinstance(raw, dict) and "excluded_regimes" in raw else raw
        if not isinstance(payload, dict):
            msg = "regime_exclusions payload not a dict"
            raise TypeError(msg)
        # Coerce values to str; skip non-string keys.
        cleaned = {str(k).upper(): str(v) for k, v in payload.items() if isinstance(k, str)}
        _cached_mtime = mtime
        _cached_payload = cleaned
        return dict(cleaned)
    except (OSError, ValueError, TypeError) as exc:
        sys.stderr.write(
            f"[regime_exclusion] failed to load {_CONFIG_PATH.name}: {exc!r}; using defaults\n",
        )
        return dict(_DEFAULT_EXCLUSIONS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def excluded_regimes() -> dict[str, str]:
    """Return ``{regime_label: reason}`` of currently excluded regimes."""
    with _lock:
        return _load_from_disk()


def is_regime_excluded(regime_label: str) -> ExclusionDecision:
    """Return :class:`ExclusionDecision` for the given regime label.

    Lookup is case-insensitive. Unknown labels are *never* excluded --
    the gate fails open so a typo in the regime classifier doesn't
    silently kill all sizing.
    """
    excl = excluded_regimes()
    reason = excl.get(regime_label.upper())
    if reason is None:
        return ExclusionDecision(excluded=False, reason="")
    return ExclusionDecision(excluded=True, reason=reason)


def write_default_config(*, force: bool = False) -> Path:
    """Write the default exclusion map to disk for hand-editing.

    Returns the path. If the file already exists and ``force`` is
    False, leaves it alone.
    """
    if _CONFIG_PATH.exists() and not force:
        return _CONFIG_PATH
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "spec_id": "REGIME_EXCLUSION_v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": "cross_regime_validation 2026-04-17",
        "excluded_regimes": dict(_DEFAULT_EXCLUSIONS),
        "notes": (
            'Add a regime by inserting `"<LABEL>": "<reason>"` under '
            "excluded_regimes. Remove a regime by deleting its key. The "
            "loader picks up edits on next call -- no restart needed."
        ),
    }
    _CONFIG_PATH.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    # Bust the cache so the very next read sees the new content.
    _invalidate_cache()
    return _CONFIG_PATH


def _invalidate_cache() -> None:
    """Test hook: forget the cached payload + mtime."""
    global _cached_mtime, _cached_payload  # noqa: PLW0603
    with _lock:
        _cached_mtime = None
        _cached_payload = None
