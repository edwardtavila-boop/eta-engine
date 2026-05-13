"""
EVOLUTIONARY TRADING ALGO  //  strategies.regime_gate
=====================================================
Regime-aware gate for grid / mean-reversion strategies.

Why this exists
---------------
Devils-advocate flagged on 2026-04-27: *"Grid trading has a known
failure mode: ranging markets feed it, trending markets eat it.
Without a regime gate, when does crypto_seed blow up in real
money?"*

The fix is a deterministic check the bot consults before firing.
This module provides ``is_grid_safe(bar)`` -> bool: returns True
when the bar's regime metrics indicate the market is in a state
where grid / DCA / mean-reversion entries are profitable in
expectation, False when a directional move is likely to
liquidate inventory at the worst price.

The check uses two well-known indicators:

1. **ADX(14)** — directional movement strength. Low ADX (< 25)
   indicates a ranging market; high ADX (> 25) indicates a trend.
   Below 20 is canonical "no trend" territory.
2. **Realized vol regime** — if the bar's ``vol_z`` (volatility
   z-score vs trailing window) is far above the trailing mean,
   the market is in a high-vol regime. High realized vol +
   high ADX = trend break, the worst environment for grid.

Both signals are read from the bar's enriched dict (the same
shape that crypto_seed already consumes in its scoring path).
Missing values default to "safe" so an under-populated bar
doesn't disable the bot — operators must supply the regime
features for the gate to actually fire.

Adoption
--------
Bots that want regime-aware grid behaviour call ``is_grid_safe``
in their ``evaluate_entry`` and return False when it returns False.
crypto_seed is the canonical caller; future grid-style bots can
adopt by adding the same one-liner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


# Devils-advocate's suggested defaults for the canonical "no trend"
# regime. Operators can override per-bot via the registry's
# extras["regime_gate_config"] payload.
DEFAULT_ADX_MAX: float = 25.0  # below this = ranging
DEFAULT_VOL_Z_MAX: float = 2.0  # |vol_z| above this = vol-regime shift


@dataclass(frozen=True)
class RegimeGateConfig:
    """Knobs for the grid-safe gate."""

    adx_max: float = DEFAULT_ADX_MAX
    vol_z_max: float = DEFAULT_VOL_Z_MAX
    #: When True, missing ADX in the bar dict is treated as
    #: "trending" (fail-shut). When False, missing ADX is
    #: treated as "ranging" (fail-open). Default True so an
    #: incomplete feature set can't accidentally green-light
    #: grid trading in a known-trending tape.
    fail_shut_on_missing_adx: bool = True

    @classmethod
    def from_extras(cls, extras: Mapping[str, object] | None) -> RegimeGateConfig:
        """Parse extras["regime_gate_config"] with defaults on
        missing or malformed."""
        if not extras:
            return cls()
        raw = extras.get("regime_gate_config")
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls(
                adx_max=float(raw.get("adx_max", DEFAULT_ADX_MAX)),
                vol_z_max=float(raw.get("vol_z_max", DEFAULT_VOL_Z_MAX)),
                fail_shut_on_missing_adx=bool(
                    raw.get("fail_shut_on_missing_adx", True),
                ),
            )
        except (TypeError, ValueError):
            return cls()


def is_grid_safe(
    bar: Mapping[str, object],
    *,
    config: RegimeGateConfig | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason).

    ``allowed=True`` iff the regime is consistent with grid /
    mean-reversion expectations. ``reason`` is a short explanation
    suitable for journal logging.
    """
    cfg = config or RegimeGateConfig()

    # ADX gate
    adx_raw = bar.get("adx_14") if hasattr(bar, "get") else None
    if adx_raw is None:
        if cfg.fail_shut_on_missing_adx:
            return False, "no adx_14 in bar; fail-shut (set fail_shut_on_missing_adx=False to override)"
        # Fail-open path
    else:
        try:
            adx = float(adx_raw)
        except (TypeError, ValueError):
            return False, f"non-numeric adx_14 ({adx_raw!r}); fail-shut"
        if adx > cfg.adx_max:
            return (
                False,
                f"adx_14 {adx:.1f} > {cfg.adx_max:.1f} -> trending; grid disabled",
            )

    # Vol-regime gate
    vol_z_raw = bar.get("vol_z") if hasattr(bar, "get") else None
    if vol_z_raw is not None:
        try:
            vol_z = float(vol_z_raw)
        except (TypeError, ValueError):
            vol_z = 0.0
        if abs(vol_z) > cfg.vol_z_max:
            return (
                False,
                f"|vol_z| {abs(vol_z):.2f} > {cfg.vol_z_max:.2f} -> regime-shift; grid disabled",
            )

    return True, "ranging regime; grid allowed"
