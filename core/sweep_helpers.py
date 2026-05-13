"""Shared helpers for parameter sweeps and glide-step tuning.

Used by _jarvis_final_revision.py and _jarvis_dual_fine_tune.py.
"""

from __future__ import annotations

from typing import Any


def glide_step(
    baseline: dict[str, Any],
    target: dict[str, Any],
    *,
    cap_rel: float = 0.34,
) -> dict[str, Any]:
    """Produce a MODERATE-compliant intermediate proposal.

    Caps each numeric param's relative change at ``cap_rel`` so the
    classifier tags the proposal as MODERATE (cap 0.34 < threshold 0.35).
    Non-numeric keys are passed through (structural change only if
    they differ; we keep them at baseline to stay MODERATE).
    """
    out: dict[str, Any] = {}
    for k, new in target.items():
        old = baseline.get(k)
        if isinstance(new, (int, float)) and isinstance(old, (int, float)) and old not in (0, 0.0):
            max_delta = abs(old) * cap_rel
            raw_delta = new - old
            clipped = raw_delta
            if abs(raw_delta) > max_delta:
                clipped = max_delta if raw_delta > 0 else -max_delta
            proposed = old + clipped
            proposed = int(round(proposed)) if isinstance(old, int) and isinstance(new, int) else round(proposed, 4)
            out[k] = proposed
        else:
            out[k] = old if old is not None else new
    return out
