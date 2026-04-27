"""Central equity + baseline + withdrawn dashboard — P6_FUNNEL central_dashboard.

Aggregates per-bot state into a single snapshot suitable for the Streamlit
dashboard or a CLI tearsheet. The snapshot combines:

* Per-bot equity, baseline, PnL, drawdown, trade count
* Total portfolio equity vs total baseline
* Cumulative withdrawn-to-cold (from funnel.sweep_engine)
* Alert-level rollup (any bot in PAUSE, any baseline breach, etc.)

The module is read-only — it doesn't drive state, it renders it. Callers hand
in the live :class:`PortfolioState` + optional sweep ledger + staking balances.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from eta_engine.core.market_quality import format_market_context_summary

if TYPE_CHECKING:
    from eta_engine.funnel.equity_monitor import PortfolioState

logger = logging.getLogger(__name__)


class BotSnapshot(BaseModel):
    name: str
    equity_usd: float
    baseline_usd: float
    unrealized_pnl: float
    todays_pnl: float
    trades_today: int
    dd_pct: float
    alert_level: str = "OK"  # OK | WATCH | PAUSE | KILL
    market_context_summary: dict[str, Any] | None = None
    market_context_summary_text: str | None = None


class StakingSnapshot(BaseModel):
    protocol: str
    asset: str
    balance: float
    apy_pct: float
    est_yield_per_year_usd: float


class CentralDashboardSnapshot(BaseModel):
    """Point-in-time snapshot of the whole eta_engine portfolio."""

    timestamp_utc: str
    bots: list[BotSnapshot] = Field(default_factory=list)
    staking: list[StakingSnapshot] = Field(default_factory=list)
    total_equity_usd: float
    total_baseline_usd: float
    total_excess_usd: float
    total_withdrawn_cold_usd: float
    worst_bot_dd_pct: float
    worst_bot_name: str | None
    any_kill_triggered: bool
    portfolio_health: str  # OK | WATCH | PAUSE | KILL
    notes: list[str] = Field(default_factory=list)


def _render_bot(
    name: str,
    equity: float,
    baseline: float,
    unrealized_pnl: float,
    todays_pnl: float,
    trades_today: int,
    max_dd_pct: float,
    alert_level: str,
    market_context_summary: dict[str, Any] | None,
    market_context_summary_text: str | None,
) -> BotSnapshot:
    return BotSnapshot(
        name=name,
        equity_usd=round(equity, 2),
        baseline_usd=round(baseline, 2),
        unrealized_pnl=round(unrealized_pnl, 2),
        todays_pnl=round(todays_pnl, 2),
        trades_today=trades_today,
        dd_pct=round(max_dd_pct, 3),
        alert_level=alert_level,
        market_context_summary=market_context_summary,
        market_context_summary_text=market_context_summary_text,
    )


def build_snapshot(
    portfolio: PortfolioState,
    *,
    bot_details: dict[str, dict[str, Any]] | None = None,
    staking_balances: list[dict[str, Any]] | None = None,
    withdrawn_cold_usd: float = 0.0,
) -> CentralDashboardSnapshot:
    """Compose a :class:`CentralDashboardSnapshot` from live state.

    Parameters
    ----------
    portfolio
        Live :class:`PortfolioState` from funnel.equity_monitor.
    bot_details
        Mapping of bot-name → per-bot stats (``unrealized_pnl``, ``todays_pnl``,
        ``trades_today``, ``dd_pct``, ``alert_level``). Missing keys default to 0.
    staking_balances
        List of ``{"protocol": ..., "asset": ..., "balance": ..., "apy_pct": ...}``
        from the staking allocator (each adapter exposes ``get_balance`` + ``get_apy``).
    withdrawn_cold_usd
        Cumulative ledger from funnel.sweep_engine cold-wallet sweeps.
    """
    details = bot_details or {}
    bot_snaps: list[BotSnapshot] = []
    worst_dd = 0.0
    worst_bot_name: str | None = None
    any_kill = False
    notes: list[str] = []
    worst_alert_rank = 0
    alert_rank_map = {"OK": 0, "WATCH": 1, "PAUSE": 2, "KILL": 3}

    for name, bot_state in portfolio.bots.items():
        d = details.get(name, {})
        alert_level = d.get("alert_level", "OK")
        market_context_summary = d.get("market_context_summary")
        if not isinstance(market_context_summary, dict):
            market_context_summary = None
        market_context_summary_text = d.get("market_context_summary_text")
        if not isinstance(market_context_summary_text, str) or not market_context_summary_text.strip():
            market_context_summary_text = (
                format_market_context_summary(market_context_summary) if market_context_summary else None
            )
        if alert_level == "KILL":
            any_kill = True
        worst_alert_rank = max(worst_alert_rank, alert_rank_map.get(alert_level, 0))
        dd_pct = float(d.get("dd_pct", 0.0))
        equity_usd = float(bot_state.current_equity)
        baseline_usd = float(bot_state.baseline_usd)
        snap = _render_bot(
            name=name,
            equity=equity_usd,
            baseline=baseline_usd,
            unrealized_pnl=float(d.get("unrealized_pnl", 0.0)),
            todays_pnl=float(d.get("todays_pnl", bot_state.todays_pnl)),
            trades_today=int(d.get("trades_today", 0)),
            max_dd_pct=dd_pct,
            alert_level=alert_level,
            market_context_summary=market_context_summary,
            market_context_summary_text=market_context_summary_text,
        )
        bot_snaps.append(snap)
        if dd_pct > worst_dd:
            worst_dd = dd_pct
            worst_bot_name = name
        if equity_usd < baseline_usd * 0.95:
            notes.append(f"{name} equity {equity_usd:.0f} < 95% baseline {baseline_usd:.0f}")

    staking_snaps: list[StakingSnapshot] = []
    for s in staking_balances or []:
        balance = float(s.get("balance", 0.0))
        apy = float(s.get("apy_pct", 0.0))
        # Assume `balance_usd` if supplied; else balance is in native units and
        # we approximate USD yield via `balance * apy/100 * usd_price` — but
        # price lookup is out-of-scope here, so we treat balance as USD notional.
        est_yield = balance * apy / 100.0
        staking_snaps.append(
            StakingSnapshot(
                protocol=str(s.get("protocol", "unknown")),
                asset=str(s.get("asset", "")),
                balance=round(balance, 6),
                apy_pct=round(apy, 2),
                est_yield_per_year_usd=round(est_yield, 2),
            )
        )

    total_equity = round(float(portfolio.total_equity), 2)
    total_baseline = round(sum(float(b.baseline_usd) for b in portfolio.bots.values()), 2)
    total_excess = round(total_equity - total_baseline, 2)

    health_label = (
        "KILL" if any_kill else "PAUSE" if worst_alert_rank >= 2 else "WATCH" if worst_alert_rank == 1 else "OK"
    )
    if total_equity < total_baseline * 0.90:
        notes.append(f"portfolio equity {total_equity} < 90% baseline {total_baseline}")
        if health_label == "OK":
            health_label = "WATCH"

    return CentralDashboardSnapshot(
        timestamp_utc=datetime.now(UTC).isoformat(),
        bots=bot_snaps,
        staking=staking_snaps,
        total_equity_usd=total_equity,
        total_baseline_usd=total_baseline,
        total_excess_usd=total_excess,
        total_withdrawn_cold_usd=round(withdrawn_cold_usd, 2),
        worst_bot_dd_pct=round(worst_dd, 3),
        worst_bot_name=worst_bot_name,
        any_kill_triggered=any_kill,
        portfolio_health=health_label,
        notes=notes,
    )


def dump_snapshot(snapshot: CentralDashboardSnapshot, path: Path | str) -> Path:
    """Persist a snapshot to ``path`` as JSON. Creates parent dir if needed."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    logger.info("central_dashboard snapshot written to %s", out)
    return out


def render_text(snapshot: CentralDashboardSnapshot) -> str:
    """Plain-text rendering for CLI / Telegram drops."""
    lines = [
        f"CENTRAL DASHBOARD @ {snapshot.timestamp_utc}",
        f"  health: {snapshot.portfolio_health}  any_kill: {snapshot.any_kill_triggered}",
        f"  total equity: ${snapshot.total_equity_usd:,.2f}"
        f"  baseline: ${snapshot.total_baseline_usd:,.2f}"
        f"  excess: ${snapshot.total_excess_usd:,.2f}",
        f"  withdrawn to cold: ${snapshot.total_withdrawn_cold_usd:,.2f}",
        "  bots:",
    ]
    for b in snapshot.bots:
        context_suffix = ""
        if b.market_context_summary_text:
            context_suffix = f"  {b.market_context_summary_text}"
        elif b.market_context_summary:
            mcs = b.market_context_summary
            context_suffix = (
                f"  market_context={mcs.get('market_context_regime', 'UNKNOWN')}"
                f" quality={float(mcs.get('market_context_quality', 0.0)):.2f}"
                f" tf={mcs.get('session_timeframe_key', 'UNKNOWN::UNKNOWN')}"
                f" spread={mcs.get('spread_regime', 'UNKNOWN')}"
            )
        lines.append(
            f"    {b.name:12s} eq=${b.equity_usd:>10,.2f}  base=${b.baseline_usd:>10,.2f}  "
            f"dd={b.dd_pct:>5.2f}%  trades={b.trades_today:>3d}  alert={b.alert_level}"
            f"{context_suffix}"
        )
    if snapshot.staking:
        lines.append("  staking:")
        for s in snapshot.staking:
            lines.append(f"    {s.protocol:8s} {s.asset:8s}  bal={s.balance:>12,.4f}  apy={s.apy_pct:>5.2f}%")
    if snapshot.notes:
        lines.append("  notes:")
        lines.extend(f"    - {n}" for n in snapshot.notes)
    return "\n".join(lines)


def from_json(payload: str | dict[str, Any]) -> CentralDashboardSnapshot:
    """Parse a previously-dumped snapshot. Useful for the Streamlit reader."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    return CentralDashboardSnapshot.model_validate(payload)
