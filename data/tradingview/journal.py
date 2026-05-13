"""
EVOLUTIONARY TRADING ALGO  //  data.tradingview.journal
=======================================================
Persisters for the four TradingView capture streams.

* :class:`TradingViewJournal` -- thin facade exposing ``record_bar``,
  ``record_indicator``, ``record_watchlist``, ``record_alert``. All
  appends are atomic per-line; the watchlist snapshot is written
  atomically via tempfile + rename.

Schemas
-------

bars (rolling per-day, gzipped)::

    {"ts": float, "symbol": str, "interval": str,
     "o": float, "h": float, "l": float, "c": float, "v": float}

indicators (single rolling jsonl)::

    {"ts": str (iso), "symbol": str, "interval": str,
     "indicator": str, "params": str|null, "value": float, "all": [float, ...]}

watchlist (single JSON snapshot, overwritten)::

    {"ts": str (iso), "lists": {<list_name>: [<row>, ...]}}

alerts (single rolling jsonl)::

    {"ts": str (iso), "kind": "fired"|"definition", ...row fields...}
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

log = logging.getLogger(__name__)

DEFAULT_DATA_ROOT = workspace_roots.ETA_TRADINGVIEW_DATA_ROOT


# ---------------------------------------------------------------------------
# Record dataclasses (light shape contracts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BarEntry:
    ts: float
    symbol: str
    interval: str
    o: float
    h: float
    l: float  # noqa: E741 -- standard OHLCV abbreviation; renaming would break the JSON wire schema
    c: float
    v: float


@dataclass(frozen=True)
class IndicatorEntry:
    ts: str
    symbol: str
    interval: str
    indicator: str
    params: str | None
    value: float
    all: list[float]


@dataclass(frozen=True)
class AlertEntry:
    ts: str
    kind: str  # "fired" | "definition"
    symbol: str | None
    name: str
    condition: str | None
    value: float | None
    active: bool
    fired_at: str | None


@dataclass(frozen=True)
class WatchlistSnapshot:
    ts: str
    lists: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Journal facade
# ---------------------------------------------------------------------------


class TradingViewJournal:
    """Persist TradingView captures to per-stream files under ``data_root``.

    All writes are best-effort: an ``OSError`` is logged and swallowed,
    keeping the capture loop running. The journal NEVER raises.
    """

    def __init__(self, data_root: Path | str | None = None) -> None:
        self.data_root = Path(data_root).expanduser() if data_root else DEFAULT_DATA_ROOT
        self.data_root.mkdir(parents=True, exist_ok=True)
        # Sub-paths
        self.bars_root = self.data_root / "bars"
        self.indicator_path = self.data_root / "indicators.jsonl"
        self.watchlist_path = self.data_root / "watchlist.json"
        self.alerts_path = self.data_root / "alerts.jsonl"
        self.bars_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Bars (per-symbol, per-day, gzipped JSONL)
    # ------------------------------------------------------------------
    def record_bar(self, entry: BarEntry) -> None:
        try:
            day = (
                datetime.fromtimestamp(entry.ts, tz=UTC).strftime("%Y-%m-%d")
                if entry.ts
                else datetime.now(UTC).strftime("%Y-%m-%d")
            )
            sym_safe = _sanitize(entry.symbol)
            sym_dir = self.bars_root / sym_safe
            sym_dir.mkdir(parents=True, exist_ok=True)
            path = sym_dir / f"{day}.jsonl.gz"
            with gzip.open(path, "ab") as f:
                f.write(json.dumps(asdict(entry), separators=(",", ":")).encode("utf-8"))
                f.write(b"\n")
        except OSError as e:
            log.warning("tradingview journal: bar append failed (%s): %s", path, e)

    # ------------------------------------------------------------------
    # Indicators (single rolling JSONL)
    # ------------------------------------------------------------------
    def record_indicator(self, entry: IndicatorEntry) -> None:
        self._append_jsonl(self.indicator_path, asdict(entry))

    # ------------------------------------------------------------------
    # Alerts (single rolling JSONL)
    # ------------------------------------------------------------------
    def record_alert(self, entry: AlertEntry) -> None:
        self._append_jsonl(self.alerts_path, asdict(entry))

    # ------------------------------------------------------------------
    # Watchlist (single JSON, atomic replace)
    # ------------------------------------------------------------------
    def record_watchlist(self, snapshot: WatchlistSnapshot) -> None:
        try:
            payload = {"ts": snapshot.ts, "lists": dict(snapshot.lists)}
            fd, tmp = tempfile.mkstemp(
                prefix=self.watchlist_path.name + ".",
                dir=str(self.watchlist_path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.watchlist_path)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError as e:
            log.warning(
                "tradingview journal: watchlist write failed (%s): %s",
                self.watchlist_path,
                e,
            )

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _append_jsonl(self, path: Path, row: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, separators=(",", ":")) + "\n"
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            log.warning("tradingview journal: append failed (%s): %s", path, e)


def _sanitize(symbol: str) -> str:
    """Coerce ``EXCHANGE:TICKER`` into a filesystem-safe directory name."""
    return symbol.replace("/", "_").replace(":", "_").replace("\\", "_") or "UNKNOWN"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
