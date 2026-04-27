"""
EVOLUTIONARY TRADING ALGO  //  data.parquet_loader
======================================
Streaming parquet reader over the DataBento cache. Bar chunks, no full-file load.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.core.data_pipeline import BarData
from eta_engine.data.models import DatasetManifest, DatasetRef, DataSource

if TYPE_CHECKING:
    from collections.abc import Iterator


class ParquetLoader:
    """Streams BarData out of a parquet file via pyarrow iter_batches."""

    def __init__(self, chunk_size: int = 10_000) -> None:
        self.chunk_size = chunk_size

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    def load(
        self,
        path: Path,
        start: datetime | None = None,
        end: datetime | None = None,
        symbol: str | None = None,
    ) -> Iterator[BarData]:
        """Yield BarData rows from a parquet file. Streams 10k-bar chunks.

        Expected schema columns (best-effort mapping, unknown columns ignored):
          timestamp / ts_event (int64 ns OR datetime)
          symbol (string) [optional]
          open / high / low / close (float)
          volume (float) [optional]
        """
        try:
            import pyarrow.parquet as pq
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pyarrow is required for ParquetLoader") from e

        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=self.chunk_size):
            rows = batch.to_pylist()
            for row in rows:
                bar = self._row_to_bar(row, default_symbol=symbol or path.stem)
                if bar is None:
                    continue
                if start is not None and bar.timestamp < start:
                    continue
                if end is not None and bar.timestamp > end:
                    continue
                yield bar

    @staticmethod
    def _row_to_bar(row: dict, default_symbol: str) -> BarData | None:
        ts_raw = row.get("timestamp") or row.get("ts_event") or row.get("time")
        if ts_raw is None:
            return None
        if isinstance(ts_raw, int):
            ts = datetime.fromtimestamp(ts_raw / 1e9, tz=UTC)
        elif isinstance(ts_raw, datetime):
            ts = ts_raw if ts_raw.tzinfo else ts_raw.replace(tzinfo=UTC)
        else:
            return None

        try:
            return BarData(
                timestamp=ts,
                symbol=str(row.get("symbol") or default_symbol),
                open=float(row.get("open", row.get("o", 0.0))),
                high=float(row.get("high", row.get("h", 0.0))),
                low=float(row.get("low", row.get("l", 0.0))),
                close=float(row.get("close", row.get("c", 0.0))),
                volume=float(row.get("volume", row.get("v", 0.0)) or 0.0),
            )
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Catalog / lineage
    # ------------------------------------------------------------------
    def scan_manifest(self, cache_dir: Path) -> DatasetManifest:
        """Walk a parquet cache tree and return a DatasetManifest."""
        cache_dir = Path(cache_dir)
        datasets: list[DatasetRef] = []
        total_rows = 0
        total_bytes = 0

        if not cache_dir.exists():
            return DatasetManifest(created_at=datetime.now(UTC), datasets=[])

        for pq_path in cache_dir.rglob("*.parquet"):
            try:
                ref = self._parquet_to_ref(pq_path)
            except Exception:  # noqa: BLE001
                continue
            if ref is None:
                continue
            datasets.append(ref)
            total_rows += ref.row_count or 0
            total_bytes += pq_path.stat().st_size

        return DatasetManifest(
            created_at=datetime.now(UTC),
            datasets=datasets,
            total_rows=total_rows,
            total_bytes=total_bytes,
        )

    def _parquet_to_ref(self, path: Path) -> DatasetRef | None:
        try:
            import pyarrow.parquet as pq
        except ImportError:  # pragma: no cover
            return None
        pf = pq.ParquetFile(path)
        n_rows = pf.metadata.num_rows
        # Parse symbol/freq from path: .../SYMBOL/FREQ/file.parquet
        parts = path.parts
        symbol = parts[-3] if len(parts) >= 3 else path.stem
        freq = parts[-2] if len(parts) >= 2 else "1m"
        now = datetime.now(UTC)
        return DatasetRef(
            symbol=symbol,
            freq=freq,
            start_date=now,
            end_date=now,
            source=DataSource.DATABENTO,
            path=path,
            row_count=n_rows,
        )

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------
    @staticmethod
    def compute_hash(path: Path) -> str:
        """SHA-256 of a file, streamed in 1MiB chunks."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
