"""
JARVIS v3 // next_level.shadow
==============================
Shadow counterfactual portfolio.

Every DENIED request spawns a shadow trade in a simulated book. We track:
  * time-of-denial -> realized market path
  * R that would have been earned (or lost) if the trade had been taken
  * cumulative shadow P&L ("regret")

The operator sees a live "regret meter": how much did JARVIS's gates
cost the desk? When regret climbs high, policy review is warranted.

Pure + deterministic; relies on a caller-supplied price feed for
counterfactual resolution.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ShadowStatus(StrEnum):
    OPEN    = "OPEN"     # not yet resolved
    CLOSED  = "CLOSED"   # resolved (TP / SL / time-stop hit)
    EXPIRED = "EXPIRED"  # max holding period elapsed without resolution


class ShadowTrade(BaseModel):
    """One denied-request counterfactual."""
    model_config = ConfigDict(frozen=False)

    id:              str = Field(min_length=1)
    opened_at:       datetime
    subsystem:       str
    symbol:          str
    side:            str = Field(pattern="^(LONG|SHORT)$")
    entry_px:        float
    stop_px:         float
    target_px:       float
    r_distance:      float = Field(ge=0.0)
    # What Jarvis would have done
    jarvis_verdict:  str = "DENIED"
    closed_at:       datetime | None = None
    closed_px:       float | None = None
    realized_r:      float | None = None
    status:          ShadowStatus = ShadowStatus.OPEN


class RegretSummary(BaseModel):
    """Roll-up of shadow P&L across all denied trades in a window."""
    model_config = ConfigDict(frozen=True)

    window_hours:    float = Field(ge=0.0)
    n_shadow_trades: int = Field(ge=0)
    n_resolved:      int = Field(ge=0)
    hit_rate:        float | None = None
    mean_r:          float | None = None
    cumulative_r:    float
    severity:        str = Field(pattern="^(GREEN|YELLOW|RED)$")
    note:            str


class ShadowLedger:
    """In-memory ledger of shadow trades with JSON persistence."""

    def __init__(self) -> None:
        self._trades: dict[str, ShadowTrade] = {}

    def add(self, trade: ShadowTrade) -> None:
        self._trades[trade.id] = trade

    def get(self, trade_id: str) -> ShadowTrade | None:
        return self._trades.get(trade_id)

    def open_trades(self) -> list[ShadowTrade]:
        return [t for t in self._trades.values() if t.status == ShadowStatus.OPEN]

    def resolve(
        self,
        trade_id: str,
        *,
        closed_px: float,
        closed_at: datetime,
        status: ShadowStatus = ShadowStatus.CLOSED,
    ) -> None:
        t = self._trades.get(trade_id)
        if t is None:
            return
        direction = 1 if t.side == "LONG" else -1
        realized_r = (
            direction * (closed_px - t.entry_px) / t.r_distance
            if t.r_distance > 0 else 0.0
        )
        t.closed_at = closed_at
        t.closed_px = closed_px
        t.realized_r = round(realized_r, 4)
        t.status = status

    def tick(
        self,
        *,
        price_lookup: dict[str, float],
        now: datetime | None = None,
        max_holding_hours: float = 4.0,
    ) -> list[str]:
        """Resolve any open trades whose stop/target was hit, or expire the rest.

        ``price_lookup`` is a caller-supplied ``symbol -> last_price`` map.
        Returns the list of ids that changed state this tick.
        """
        now = now or datetime.now(UTC)
        changed: list[str] = []
        for t in list(self._trades.values()):
            if t.status != ShadowStatus.OPEN:
                continue
            px = price_lookup.get(t.symbol)
            if px is None:
                continue
            hit = False
            closed_px = px
            if t.side == "LONG":
                if px <= t.stop_px:
                    closed_px = t.stop_px
                    hit = True
                elif px >= t.target_px:
                    closed_px = t.target_px
                    hit = True
            else:  # SHORT
                if px >= t.stop_px:
                    closed_px = t.stop_px
                    hit = True
                elif px <= t.target_px:
                    closed_px = t.target_px
                    hit = True
            if hit:
                self.resolve(
                    t.id, closed_px=closed_px, closed_at=now,
                    status=ShadowStatus.CLOSED,
                )
                changed.append(t.id)
                continue
            # Expire if held too long
            if (now - t.opened_at) > timedelta(hours=max_holding_hours):
                self.resolve(
                    t.id, closed_px=px, closed_at=now,
                    status=ShadowStatus.EXPIRED,
                )
                changed.append(t.id)
        return changed

    def regret(
        self, window_hours: float = 24.0, now: datetime | None = None,
    ) -> RegretSummary:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=window_hours)
        trades = [t for t in self._trades.values() if t.opened_at >= cutoff]
        resolved = [t for t in trades if t.realized_r is not None]
        cum_r = sum((t.realized_r or 0.0) for t in resolved)
        mean = (cum_r / len(resolved)) if resolved else None
        hits = sum(1 for t in resolved if (t.realized_r or 0.0) > 0)
        hit_rate = (hits / len(resolved)) if resolved else None
        # Severity classification
        # If cumulative_r > +3R from blocked trades, that's a REGRET problem
        # (JARVIS denied too much alpha); flag for operator review.
        if cum_r >= 3.0:
            severity = "RED"
            note = (
                f"regret {cum_r:+.2f}R in {window_hours}h "
                "-- JARVIS is overly restrictive"
            )
        elif cum_r >= 1.0:
            severity = "YELLOW"
            note = f"regret {cum_r:+.2f}R in {window_hours}h -- monitor"
        elif cum_r <= -1.0:
            severity = "GREEN"
            note = (
                f"regret {cum_r:+.2f}R in {window_hours}h -- denials protected capital"
            )
        else:
            severity = "GREEN"
            note = f"regret near zero ({cum_r:+.2f}R) -- gates are well-tuned"
        return RegretSummary(
            window_hours=window_hours,
            n_shadow_trades=len(trades),
            n_resolved=len(resolved),
            hit_rate=round(hit_rate, 4) if hit_rate is not None else None,
            mean_r=round(mean, 4) if mean is not None else None,
            cumulative_r=round(cum_r, 4),
            severity=severity,
            note=note,
        )

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        data = {"trades": [t.model_dump(mode="json") for t in self._trades.values()]}
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> ShadowLedger:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        inst = cls()
        for t in data.get("trades", []):
            tt = ShadowTrade.model_validate(t)
            inst._trades[tt.id] = tt
        return inst


def shadow_from_denied_request(
    *,
    request_id: str,
    subsystem: str,
    symbol: str,
    side: str,
    entry_px: float,
    stop_px: float,
    target_px: float,
    now: datetime | None = None,
) -> ShadowTrade:
    """Convenience factory: given the shape of a denied order, create a shadow."""
    r_dist = abs(entry_px - stop_px)
    return ShadowTrade(
        id=request_id,
        opened_at=now or datetime.now(UTC),
        subsystem=subsystem,
        symbol=symbol,
        side=side,
        entry_px=entry_px,
        stop_px=stop_px,
        target_px=target_px,
        r_distance=r_dist,
    )
