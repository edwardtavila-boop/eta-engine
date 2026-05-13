"""Basis / contango tracker (Tier-2 #13, 2026-04-27).

Now that ETA Engine routes crypto exposure through CME futures (M2),
the per-perp ``funding_rate`` analog becomes BASIS: the premium of
the futures price over the spot index. Tracking it lets JARVIS detect:

  * BACKWARDATION (basis < 0): supply/demand stress, often bearish
  * CONTANGO (basis > 0): cost of carry, normal in calm
  * FAST CHANGES: regime shift (spot diverging from futures)

Inputs are JSON snapshots written by an agent-layer worker that polls
spot vs CME futures every N minutes:
  ``state/basis/<symbol>.json``
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
BASIS_DIR = ROOT / "state" / "basis"


@dataclass(frozen=True)
class BasisSnapshot:
    symbol: str  # CME contract code (e.g. "MBT" / "MET")
    ts: datetime
    spot_price: float
    futures_price: float
    basis_pct: float  # (futures - spot) / spot
    days_to_expiry: int
    annualized_basis: float
    is_stale: bool = False


def current_snapshot(symbol: str, *, max_age_min: float = 30.0) -> BasisSnapshot | None:
    path = BASIS_DIR / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    ts_str = data.get("ts")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (TypeError, ValueError, AttributeError):
        return None

    age = datetime.now(UTC) - ts
    is_stale = age > timedelta(minutes=max_age_min)

    return BasisSnapshot(
        symbol=str(data.get("symbol", symbol)).upper(),
        ts=ts,
        spot_price=float(data.get("spot_price", 0)),
        futures_price=float(data.get("futures_price", 0)),
        basis_pct=float(data.get("basis_pct", 0)),
        days_to_expiry=int(data.get("days_to_expiry", 0)),
        annualized_basis=float(data.get("annualized_basis", 0)),
        is_stale=is_stale,
    )


def write_snapshot(snap: BasisSnapshot) -> Path:
    BASIS_DIR.mkdir(parents=True, exist_ok=True)
    path = BASIS_DIR / f"{snap.symbol}.json"
    path.write_text(
        json.dumps(
            {
                "symbol": snap.symbol,
                "ts": snap.ts.isoformat(),
                "spot_price": snap.spot_price,
                "futures_price": snap.futures_price,
                "basis_pct": snap.basis_pct,
                "days_to_expiry": snap.days_to_expiry,
                "annualized_basis": snap.annualized_basis,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def regime_label(snap: BasisSnapshot) -> str:
    """Classify the basis state: BACKWARDATION / NORMAL / STEEP_CONTANGO."""
    if snap.basis_pct < -0.001:
        return "BACKWARDATION"
    if snap.annualized_basis > 0.10:  # 10%+ annualized
        return "STEEP_CONTANGO"
    return "NORMAL"
