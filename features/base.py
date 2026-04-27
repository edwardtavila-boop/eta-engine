"""
EVOLUTIONARY TRADING ALGO  //  features.base
================================
Abstract Feature contract + result model.
Every confluence input flows through this surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData


class FeatureResult(BaseModel):
    """Output of a single Feature computation."""

    name: str
    raw_value: float = 0.0
    normalized_score: float = Field(ge=0.0, le=1.0)
    weight: float = Field(gt=0.0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Feature(ABC):
    """Base class for all confluence features.

    Subclasses set `name` + `weight` class attributes and implement
    `compute(bar, ctx)` returning a score in [0, 1].
    """

    name: str = "base"
    weight: float = 1.0

    @abstractmethod
    def compute(self, bar: BarData, ctx: dict[str, Any]) -> float:
        """Return normalized score in [0, 1]."""
        ...

    def evaluate(self, bar: BarData, ctx: dict[str, Any]) -> FeatureResult:
        """Wrap `compute` into a `FeatureResult`."""
        score = self.compute(bar, ctx)
        clamped = max(0.0, min(1.0, float(score)))
        return FeatureResult(
            name=self.name,
            raw_value=float(score),
            normalized_score=clamped,
            weight=self.weight,
            timestamp=bar.timestamp if bar else datetime.now(UTC),
        )
