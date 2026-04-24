"""
JARVIS v3 // next_level.digital_twin
====================================
Shadow-of-prod simulation layer.

A continuously-running clone of the production bot stack is fed the
same market data + signal stream -- but several seconds ahead or with
a proposed config change applied. When the twin throws errors or
breaches limits, JARVIS paper-trades the change before prod sees it.

Capabilities:

  * ``TwinConfigDelta``   -- describe a proposed change (param bump,
                              strategy enable, regime override)
  * ``TwinSignal``        -- signal emitted by either prod or twin
  * ``TwinComparator``    -- attach prod + twin streams, diff them
  * ``TwinBreachReport``  -- divergence found? Here's what, where, how severe
  * ``twin_verdict``      -- aggregate over a window: is the twin SAFE
                              to promote / AVOID / FURTHER_SOAK?

Pure; no actual trading or network. The caller integrates this into
the nightly autopilot pipeline.
"""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DeltaKind(StrEnum):
    PARAM_CHANGE   = "PARAM_CHANGE"
    STRATEGY_TOGGLE = "STRATEGY_TOGGLE"
    REGIME_OVERRIDE = "REGIME_OVERRIDE"
    SIZE_MULT_BUMP  = "SIZE_MULT_BUMP"
    NEW_GATE        = "NEW_GATE"


class TwinConfigDelta(BaseModel):
    """A proposed configuration change to run through the digital twin."""
    model_config = ConfigDict(frozen=True)

    delta_id:     str = Field(min_length=1)
    kind:         DeltaKind
    description:  str = Field(min_length=1)
    prod_value:   str = ""
    twin_value:   str = ""
    rationale:    str = ""


class TwinSignal(BaseModel):
    """A single signal emitted by prod or twin side."""
    model_config = ConfigDict(frozen=True)

    ts:          datetime
    source:      str = Field(pattern="^(PROD|TWIN)$")
    signal_id:   str = Field(min_length=1)
    subsystem:   str
    verdict:     str
    size_mult:   float = Field(ge=0.0, le=1.0, default=1.0)
    realized_r:  float | None = None   # populated after close
    tag:         str = ""


class TwinDivergence(BaseModel):
    """One prod/twin mismatch in a matched signal."""
    model_config = ConfigDict(frozen=True)

    signal_id:        str
    prod_verdict:     str
    twin_verdict:     str
    prod_size_mult:   float
    twin_size_mult:   float
    realized_r_prod:  float | None = None
    realized_r_twin:  float | None = None
    notes:            str = ""


class TwinVerdict(BaseModel):
    """Aggregate judgment over a window of matched signals."""
    model_config = ConfigDict(frozen=True)

    ts:              datetime
    window_hours:    float
    matched_signals: int = Field(ge=0)
    divergences:     int = Field(ge=0)
    divergence_rate: float = Field(ge=0.0, le=1.0)
    mean_r_prod:     float | None = None
    mean_r_twin:     float | None = None
    severity:        str = Field(pattern="^(GREEN|YELLOW|RED)$")
    verdict:         str = Field(pattern="^(PROMOTE|FURTHER_SOAK|AVOID)$")
    note:            str


class TwinComparator:
    """Accepts prod + twin signals and compares them by ``signal_id``."""

    def __init__(self, *, ring_size: int = 10_000) -> None:
        self._prod: dict[str, TwinSignal] = {}
        self._twin: dict[str, TwinSignal] = {}
        self._ring: deque[TwinSignal] = deque(maxlen=ring_size)

    def ingest(self, sig: TwinSignal) -> None:
        self._ring.append(sig)
        if sig.source == "PROD":
            self._prod[sig.signal_id] = sig
        else:
            self._twin[sig.signal_id] = sig

    def divergences(
        self, *, window_hours: float = 24.0, now: datetime | None = None,
    ) -> list[TwinDivergence]:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=window_hours)
        out: list[TwinDivergence] = []
        ids = set(self._prod) & set(self._twin)
        for sid in ids:
            p = self._prod[sid]
            t = self._twin[sid]
            if p.ts < cutoff and t.ts < cutoff:
                continue
            if p.verdict == t.verdict and abs(p.size_mult - t.size_mult) < 0.01:
                continue
            out.append(TwinDivergence(
                signal_id=sid,
                prod_verdict=p.verdict, twin_verdict=t.verdict,
                prod_size_mult=p.size_mult, twin_size_mult=t.size_mult,
                realized_r_prod=p.realized_r, realized_r_twin=t.realized_r,
                notes="",
            ))
        return out

    def verdict(
        self, *, window_hours: float = 24.0,
        max_divergence_rate_ok: float = 0.10,
        max_divergence_rate_yellow: float = 0.25,
        now: datetime | None = None,
    ) -> TwinVerdict:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=window_hours)
        matched: list[str] = []
        ids = set(self._prod) & set(self._twin)
        for sid in ids:
            p = self._prod[sid]
            t = self._twin[sid]
            if p.ts >= cutoff or t.ts >= cutoff:
                matched.append(sid)
        divs = self.divergences(window_hours=window_hours, now=now)
        n = len(matched)
        d = len(divs)
        rate = (d / n) if n else 0.0
        prod_rs = [
            self._prod[sid].realized_r for sid in matched
            if self._prod[sid].realized_r is not None
        ]
        twin_rs = [
            self._twin[sid].realized_r for sid in matched
            if self._twin[sid].realized_r is not None
        ]
        mean_r_prod = (sum(prod_rs) / len(prod_rs)) if prod_rs else None
        mean_r_twin = (sum(twin_rs) / len(twin_rs)) if twin_rs else None
        # Classify
        if rate >= max_divergence_rate_yellow:
            severity = "RED"
            verdict = "AVOID"
            note = (
                f"divergence {rate:.0%} >= {max_divergence_rate_yellow:.0%}"
                " -- do NOT promote twin config"
            )
        elif rate >= max_divergence_rate_ok:
            severity = "YELLOW"
            verdict = "FURTHER_SOAK"
            note = (
                f"divergence {rate:.0%} elevated -- continue shadow run"
            )
        else:
            # Twin must also be at-least-as-profitable
            if (
                mean_r_twin is not None
                and mean_r_prod is not None
                and mean_r_twin + 0.02 < mean_r_prod
            ):
                severity = "YELLOW"
                verdict = "FURTHER_SOAK"
                note = (
                    f"twin mean_r {mean_r_twin:+.2f} underperforms prod "
                    f"{mean_r_prod:+.2f} -- need more evidence"
                )
            else:
                severity = "GREEN"
                verdict = "PROMOTE"
                note = (
                    f"divergence {rate:.0%} OK, mean_r prod/twin parity"
                )
        return TwinVerdict(
            ts=now,
            window_hours=window_hours,
            matched_signals=n,
            divergences=d,
            divergence_rate=round(rate, 4),
            mean_r_prod=round(mean_r_prod, 4) if mean_r_prod is not None else None,
            mean_r_twin=round(mean_r_twin, 4) if mean_r_twin is not None else None,
            severity=severity,
            verdict=verdict,
            note=note,
        )

    def prune(self, *, keep_hours: float = 48.0,
              now: datetime | None = None) -> int:
        """Drop signals older than ``keep_hours``. Returns # pruned."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=keep_hours)
        before = len(self._prod) + len(self._twin)
        self._prod = {k: v for k, v in self._prod.items() if v.ts >= cutoff}
        self._twin = {k: v for k, v in self._twin.items() if v.ts >= cutoff}
        return before - (len(self._prod) + len(self._twin))
