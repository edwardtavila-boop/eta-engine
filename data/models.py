"""
EVOLUTIONARY TRADING ALGO  //  data.models
==============================
Pydantic v2 models for data catalog / lineage / integrity.
"""

from __future__ import annotations

import datetime as _datetime_runtime  # noqa: F401  -- pydantic v2 forward-ref resolution
import pathlib as _pathlib_runtime  # noqa: F401  -- pydantic v2 forward-ref resolution
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path
else:
    datetime = _datetime_runtime.datetime
    Path = _pathlib_runtime.Path


class DataSource(StrEnum):
    """Known upstream providers."""

    DATABENTO = "DATABENTO"
    BYBIT = "BYBIT"
    TRADOVATE = "TRADOVATE"
    SYNTHETIC = "SYNTHETIC"


class DatasetRef(BaseModel):
    """Pointer to a single dataset in the parquet cache.

    Captures enough lineage info to regenerate and to verify integrity.
    """

    symbol: str
    freq: str = Field(description="One of 1m / 1s / tick / 1m_bbo / trades")
    start_date: datetime
    end_date: datetime
    source: DataSource
    path: Path | None = None
    hash_sha256: str | None = Field(default=None, description="SHA-256 of the parquet file")
    row_count: int | None = Field(default=None, ge=0)

    @property
    def duration_days(self) -> float:
        return (self.end_date - self.start_date).total_seconds() / 86_400.0


class DatasetManifest(BaseModel):
    """Top-level catalog of what is currently in the data cache."""

    version: str = "1.0"
    created_at: datetime
    datasets: list[DatasetRef] = Field(default_factory=list)
    total_rows: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)

    def symbols(self) -> list[str]:
        return sorted({d.symbol for d in self.datasets})

    def find(self, symbol: str, freq: str) -> list[DatasetRef]:
        return [d for d in self.datasets if d.symbol == symbol and d.freq == freq]


class DataIntegrityReport(BaseModel):
    """Output of cleaning.validate / gap scan over a bar stream."""

    missing_ranges: list[tuple[datetime, datetime]] = Field(default_factory=list)
    duplicates: int = Field(default=0, ge=0)
    outliers: int = Field(default=0, ge=0)
    gaps_filled: int = Field(default=0, ge=0)

    @property
    def is_clean(self) -> bool:
        return not self.missing_ranges and self.duplicates == 0 and self.outliers == 0

    def summary(self) -> str:
        return (
            f"missing={len(self.missing_ranges)} "
            f"dupes={self.duplicates} "
            f"outliers={self.outliers} "
            f"gaps_filled={self.gaps_filled}"
        )
