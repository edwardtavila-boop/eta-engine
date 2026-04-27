"""
EVOLUTIONARY TRADING ALGO  //  data.library
==========================================
Single source of truth for every market-data CSV available locally.

Why this exists
---------------
We have a lot of data scattered across ``C:\\mnq_data\\`` (synced
active set) and ``C:\\mnq_data\\history\\`` (long history): 1s/1m/5m
MNQ ladder, hourly + daily MNQ (4+ years), correlated tickers
(ES1, NQ1, RTY1, DXY, VIX), and order-flow tick aggregates. Every
research script that wants to load a slice today is hand-coding the
path + schema. This module replaces that with one catalog.

Two on-disk shapes are handled transparently:

* **"main"** — header ``timestamp_utc, epoch_s, open, high, low, close, volume, session``.
  Files: ``C:\\mnq_data\\mnq_*.csv``.
* **"history"** — header ``time, open, high, low, close, volume`` where
  ``time`` is epoch seconds (UTC). Files: ``C:\\mnq_data\\history\\<SYMBOL>1_<TF>.csv``.

Exposed API
-----------
``DataLibrary`` — discover, list, fetch metadata, load bars.
``DatasetMeta`` — frozen dataclass: symbol, timeframe, schema_kind,
  path, row_count, start_ts, end_ts.
``default_library()`` — singleton bound to the conventional roots.

JARVIS readability
------------------
``DataLibrary.summary_markdown()`` produces a table that JARVIS (or
any operator) can render to know what's available without grep'ing
the filesystem. ``DataLibrary.summary_jarvis_payload()`` returns
the same info as a list[dict] suitable for journaling as a
``Actor.JARVIS`` event with ``intent="data_inventory"``.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable



# ---------------------------------------------------------------------------
# Conventional roots
# ---------------------------------------------------------------------------

DEFAULT_ROOTS: tuple[Path, ...] = (
    Path(r"C:\mnq_data"),
    Path(r"C:\mnq_data\history"),
    # CME crypto bars (BTC/MBT/ETH/MET). Directory may not exist
    # yet — DataLibrary._discover skips missing roots silently.
    # When fetch_btc_bars.py starts writing here, the next library
    # call surfaces the new datasets automatically.
    Path(r"C:\crypto_data"),
    Path(r"C:\crypto_data\history"),
    # On-chain time series (BTCONCHAIN_D.csv etc.) written by
    # scripts/fetch_onchain_history. Sentiment + macro feeds use the
    # same root + the SENT/MACRO suffix conventions documented in
    # data.audit._resolve_library_lookup.
    Path(r"C:\crypto_data\onchain"),
    Path(r"C:\crypto_data\sentiment"),
    Path(r"C:\crypto_data\macro"),
    # IBKR-native crypto bars for the pre-live drift comparison
    # (scripts/fetch_ibkr_crypto_bars writes here).
    Path(r"C:\crypto_data\ibkr\history"),
)


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# History shape: SYMBOL_<TF>.csv where TF is one of 1s/1m/5m/15m/1h/4h/D/W
# Examples: MNQ1_5m.csv, NQ1_4h.csv, MNQ1_D.csv
_HISTORY_RE = re.compile(
    r"^(?P<symbol>[A-Z]+\d?)_(?P<tf>\d+(?:s|m|h)|D|W)\.csv$"
)

# Main shape: mnq_<TICKER>_<DIGITS>.csv where DIGITS is 1 or 5 (minutes)
# OR: mnq_<TF>.csv where TF in {1s, 1m, 5m}
# Examples: mnq_es1_5.csv -> ES1 / 5m; mnq_5m.csv -> MNQ / 5m;
#           mnq_tick_1.csv -> TICK / 1m; mnq_vix_5.csv -> VIX / 5m.
_MAIN_TICKER_RE = re.compile(
    r"^mnq_(?P<ticker>[a-z]+\d?)_(?P<min>\d+)\.csv$"
)
_MAIN_BASE_RE = re.compile(
    r"^mnq_(?P<tf>\d+(?:s|m|h))\.csv$"
)

# Map main-shape minute digits to timeframe labels.
_MAIN_MIN_TO_TF = {"1": "1m", "5": "5m"}


def _parse_filename(p: Path) -> tuple[str, str, str] | None:
    """Return (symbol, timeframe, schema_kind) or None if not recognised."""
    name = p.name
    m = _HISTORY_RE.match(name)
    if m:
        return m.group("symbol").upper(), m.group("tf"), "history"
    m = _MAIN_TICKER_RE.match(name)
    if m:
        ticker = m.group("ticker").upper()
        tf = _MAIN_MIN_TO_TF.get(m.group("min"))
        if tf is None:
            return None
        return ticker, tf, "main"
    m = _MAIN_BASE_RE.match(name)
    if m:
        return "MNQ", m.group("tf"), "main"
    return None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetMeta:
    """Static metadata about one CSV. Cheap to construct, no bars loaded."""

    symbol: str
    timeframe: str
    schema_kind: str  # "main" or "history"
    path: Path
    row_count: int
    start_ts: datetime
    end_ts: datetime

    @property
    def key(self) -> str:
        """Stable lookup key. ``schema_kind`` disambiguates duplicates."""
        return f"{self.symbol}/{self.timeframe}/{self.schema_kind}"

    def days_span(self) -> float:
        """Calendar-day span of the data, useful for window-size tuning."""
        return (self.end_ts - self.start_ts).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Probe — read first + last data row without loading the whole file
# ---------------------------------------------------------------------------


def _probe(path: Path, schema_kind: str) -> tuple[int, datetime, datetime] | None:
    """Return (row_count, first_ts, last_ts) or None on parse failure.

    For history shape the first column is epoch seconds; for main
    shape it's an ISO-8601 timestamp_utc string. We tail the file to
    grab the last row without loading every line.
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            fh.readline()  # skip header
            first_data = fh.readline()
            if not first_data:
                return None
            row_count = 1
            last_data = first_data
            for line in fh:
                if line.strip():
                    last_data = line
                    row_count += 1
    except OSError:
        return None
    first_ts = _parse_ts_from_row(first_data, schema_kind)
    last_ts = _parse_ts_from_row(last_data, schema_kind)
    if first_ts is None or last_ts is None:
        return None
    return row_count, first_ts, last_ts


def _parse_ts_from_row(line: str, schema_kind: str) -> datetime | None:
    """Extract the timestamp from one CSV data line."""
    parts = line.strip().split(",")
    if not parts:
        return None
    raw = parts[0].strip().strip('"')
    if schema_kind == "history":
        try:
            return datetime.fromtimestamp(int(float(raw)), tz=UTC)
        except (TypeError, ValueError):
            return None
    # main shape — ISO-8601, optional Z
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


class DataLibrary:
    """Lazy catalog of every recognised CSV under the configured roots."""

    def __init__(self, roots: Iterable[Path] | None = None) -> None:
        self.roots: tuple[Path, ...] = tuple(roots) if roots else DEFAULT_ROOTS
        self._datasets: list[DatasetMeta] = []
        self._discover()

    def _discover(self) -> None:
        seen_paths: set[Path] = set()
        for root in self.roots:
            if not root.exists() or not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_file() or entry.suffix.lower() != ".csv":
                    continue
                if entry in seen_paths:
                    continue
                parsed = _parse_filename(entry)
                if parsed is None:
                    continue
                symbol, tf, schema_kind = parsed
                probe = _probe(entry, schema_kind)
                if probe is None:
                    continue
                row_count, start_ts, end_ts = probe
                self._datasets.append(
                    DatasetMeta(
                        symbol=symbol,
                        timeframe=tf,
                        schema_kind=schema_kind,
                        path=entry,
                        row_count=row_count,
                        start_ts=start_ts,
                        end_ts=end_ts,
                    )
                )
                seen_paths.add(entry)

    # ── query ──

    def list(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        schema_kind: str | None = None,
    ) -> list[DatasetMeta]:
        out = list(self._datasets)
        if symbol:
            out = [d for d in out if d.symbol.upper() == symbol.upper()]
        if timeframe:
            out = [d for d in out if d.timeframe == timeframe]
        if schema_kind:
            out = [d for d in out if d.schema_kind == schema_kind]
        return out

    def get(
        self,
        *,
        symbol: str,
        timeframe: str,
        schema_kind: str | None = None,
    ) -> DatasetMeta | None:
        """Return the single dataset matching, preferring history when ambiguous."""
        matches = self.list(symbol=symbol, timeframe=timeframe, schema_kind=schema_kind)
        if not matches:
            return None
        if schema_kind is not None:
            return matches[0]
        # Prefer the longer-history version (typically "history").
        return max(matches, key=lambda d: d.row_count)

    def symbols(self) -> list[str]:
        return sorted({d.symbol for d in self._datasets})

    def timeframes(self) -> list[str]:
        return sorted({d.timeframe for d in self._datasets}, key=_tf_sort_key)

    # ── bars loader ──

    def load_bars(self, dataset: DatasetMeta, *, limit: int | None = None) -> list:
        """Load ``BarData`` for the given dataset. Imports lazily to keep
        the library importable in environments where ``BarData`` (which
        depends on pydantic) hasn't been installed.
        """
        from eta_engine.core.data_pipeline import BarData

        bars: list = []
        with dataset.path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                ts: datetime | None = None
                if dataset.schema_kind == "history":
                    raw = row.get("time")
                    if raw is not None:
                        try:
                            ts = datetime.fromtimestamp(int(float(raw)), tz=UTC)
                        except (TypeError, ValueError):
                            ts = None
                else:
                    raw = row.get("timestamp_utc") or row.get("timestamp")
                    if raw:
                        if raw.endswith("Z"):
                            raw = raw[:-1] + "+00:00"
                        try:
                            ts = datetime.fromisoformat(raw)
                        except ValueError:
                            ts = None
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                try:
                    bars.append(
                        BarData(
                            timestamp=ts,
                            symbol=dataset.symbol,
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row.get("volume", 0.0) or 0.0),
                        )
                    )
                except (KeyError, ValueError):
                    continue
                if limit and len(bars) >= limit:
                    break
        return bars

    # ── reporting ──

    def summary_markdown(self) -> str:
        """Single-table dump suitable for a status page or JARVIS event."""
        lines = [
            "# Data Library",
            "",
            f"_Roots: {', '.join(str(r) for r in self.roots)}_",
            f"_Datasets: {len(self._datasets)} | "
            f"Symbols: {len(self.symbols())} | "
            f"Timeframes: {len(self.timeframes())}_",
            "",
            "| Symbol | Timeframe | Schema | Rows | Start | End | Days |",
            "|---|---|---|---:|---|---|---:|",
        ]
        for d in sorted(
            self._datasets,
            key=lambda d: (d.symbol, _tf_sort_key(d.timeframe), d.schema_kind),
        ):
            lines.append(
                f"| {d.symbol} | {d.timeframe} | {d.schema_kind} | "
                f"{d.row_count:,} | {d.start_ts.date()} | {d.end_ts.date()} | "
                f"{d.days_span():.1f} |"
            )
        return "\n".join(lines)

    def summary_jarvis_payload(self) -> list[dict]:
        """Structured form for journaling as an ``Actor.JARVIS`` event."""
        return [
            {
                "symbol": d.symbol,
                "timeframe": d.timeframe,
                "schema_kind": d.schema_kind,
                "rows": d.row_count,
                "start": d.start_ts.isoformat(),
                "end": d.end_ts.isoformat(),
                "days": round(d.days_span(), 2),
                "path": str(d.path),
            }
            for d in self._datasets
        ]


# ---------------------------------------------------------------------------
# Sort helpers
# ---------------------------------------------------------------------------

_TF_ORDER = {
    "1s": 0, "5s": 1, "10s": 2, "30s": 3,
    "1m": 4, "5m": 5, "15m": 6, "30m": 7,
    "1h": 8, "2h": 9, "4h": 10, "1d": 11, "D": 11, "1w": 12, "W": 12,
}


def _tf_sort_key(tf: str) -> int:
    return _TF_ORDER.get(tf.lower() if tf not in {"D", "W"} else tf, 99)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_default: DataLibrary | None = None


def default_library() -> DataLibrary:
    """Return a process-wide cached library bound to ``DEFAULT_ROOTS``."""
    global _default
    if _default is None:
        _default = DataLibrary()
    return _default
