"""Correlation-regime detector (Tier-2 #12, 2026-04-27).

Detects when pairwise correlations have shifted from their long-run
baseline -- a leading indicator of regime change. When MNQ-NQ
correlation drops from 0.99 to 0.70, something material is happening
to the equity-index complex; when BTC-ETH correlation breaks down,
crypto is decoupling.

Compares ROLLING 30-day correlations to BASELINE 90-day correlations
(both sourced from refresh_correlation_matrix output if available, or
hardcoded defaults from jarvis_correlation otherwise).

Fires a ``correlation_regime_shift`` Resend alert when |delta| > 0.15
on any pair. ALFRED can use this as an extra stress component.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CorrRegimeShift:
    pair: str
    baseline: float
    rolling: float
    delta: float
    severity: str  # "minor" | "material" | "extreme"


def detect_shifts(
    rolling_pairs: dict[str, float],
    baseline_pairs: dict[str, float],
    *,
    minor_threshold: float = 0.10,
    material_threshold: float = 0.15,
    extreme_threshold: float = 0.30,
) -> list[CorrRegimeShift]:
    """Compare rolling vs baseline correlation per pair.

    Both inputs are ``{"A|B": correlation}`` dicts. Returns the pairs
    whose absolute delta exceeds ``minor_threshold``.
    """
    shifts: list[CorrRegimeShift] = []
    for pair, base in baseline_pairs.items():
        roll = rolling_pairs.get(pair)
        if roll is None:
            continue
        delta = roll - base
        absd = abs(delta)
        if absd < minor_threshold:
            continue
        if absd >= extreme_threshold:
            severity = "extreme"
        elif absd >= material_threshold:
            severity = "material"
        else:
            severity = "minor"
        shifts.append(
            CorrRegimeShift(
                pair=pair,
                baseline=round(base, 4),
                rolling=round(roll, 4),
                delta=round(delta, 4),
                severity=severity,
            )
        )
    return sorted(shifts, key=lambda s: -abs(s.delta))


def load_baseline() -> dict[str, float]:
    """Pull baseline correlations from the learned file (or fall back
    to the hardcoded jarvis_correlation defaults)."""
    learned_path = ROOT / "state" / "correlation" / "learned.json"
    if learned_path.exists():
        try:
            data = json.loads(learned_path.read_text(encoding="utf-8"))
            return data.get("pairs", {})
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: hardcoded defaults from jarvis_correlation
    from eta_engine.brain.jarvis_correlation import _CORRELATIONS

    return {f"{a}|{b}": v for (a, b), v in _CORRELATIONS.items()}


def write_shift_report(shifts: list[CorrRegimeShift]) -> Path:
    """Persist the shift list for the daily kaizen pickup."""
    out = ROOT / "state" / "correlation_regime"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"shifts_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(
        json.dumps(
            [
                {"pair": s.pair, "baseline": s.baseline, "rolling": s.rolling, "delta": s.delta, "severity": s.severity}
                for s in shifts
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
