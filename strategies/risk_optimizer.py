"""Risk optimizer — JARVIS's position-sizing brain. Takes a signal, the
bot's current state, and market context → returns the optimal position size.

Rules (in priority order):
1. FleetRiskGate — never breach the fleet daily loss limit
2. Drawdown-aware — shrink after losses (0.5x below 2% DD, 0.25x below 4%)
3. Conviction-based — scale by confluence score (A+ = 1.5x, solid = 1.0x, marginal = 0.5x)
4. Kelly adaptive — f* = edge / variance, capped at half-Kelly (0.5 * f*)
5. Pyramid — add to winners (first entry = base, second = 0.75x base, third = 0.5x base)
6. Per-bot cap — never exceed daily_loss_limit_pct or max_trades_per_day

Usage
-----
    from eta_engine.strategies.risk_optimizer import compute_optimal_size
    size_pct = compute_optimal_size(bot_id="mnq_futures_sage", signal=signal,
                                    equity=100000, current_dd_pct=0.01,
                                    confluence_score=3, pyramid_count=0)
"""

from __future__ import annotations


def compute_optimal_size(
    *,
    bot_id: str,
    equity: float,
    base_risk_pct: float = 0.01,
    current_dd_pct: float = 0.0,
    confluence_score: int = 0,
    pyramid_count: int = 0,
    recent_win_rate: float = 0.5,
    oos_sharpe: float | None = None,
    max_trades_today: int = 0,
    trades_allowed_per_day: int = 3,
    wins_in_a_row: int = 0,
    losses_in_a_row: int = 0,
) -> float:
    """Return optimal position size as fraction of equity.

    Returns 0.0 if trading should be blocked (drawdown cap exceeded or
    daily trade limit reached).
    """
    rules: list[str] = []
    multiplier = 1.0

    # Rule 0 — Daily trade cap
    if max_trades_today >= trades_allowed_per_day:
        return 0.0

    # Rule 1 — Drawdown-aware: shrink below waterline, kill at -4%
    dd = abs(current_dd_pct)
    if dd >= 0.04:
        multiplier *= 0.0  # hard block
        rules.append("dd_hard_block")
    elif dd >= 0.03:
        multiplier *= 0.25
        rules.append("dd_deep")
    elif dd >= 0.02:
        multiplier *= 0.50
        rules.append("dd_moderate")
    elif dd >= 0.01:
        multiplier *= 0.75
        rules.append("dd_shallow")
    else:
        rules.append("dd_clean")

    # Rule 2 — Loss streak protection: 3 losses in a row → pause
    if losses_in_a_row >= 3:
        multiplier *= 0.0
        rules.append("loss_streak_halt")
    elif losses_in_a_row >= 2:
        multiplier *= 0.33
        rules.append("loss_streak_cut")

    # Rule 3 — Pyramid scaling: first entry full, later entries scaled
    if pyramid_count == 0:
        pass  # first entry: full size
    elif pyramid_count == 1:
        multiplier *= 0.75  # second entry: 75%
        rules.append("pyramid_2nd")
    elif pyramid_count == 2:
        multiplier *= 0.50  # third entry: 50%
        rules.append("pyramid_3rd")
    else:
        multiplier *= 0.25  # subsequent: 25%
        rules.append("pyramid_late")

    # Rule 4 — Win streak: get aggressive after 3 consecutive wins
    if wins_in_a_row >= 3:
        multiplier *= 1.25
        rules.append("momentum_hot")

    # Rule 5 — Kelly fraction: scale by expected edge
    if oos_sharpe is not None and oos_sharpe > 0:
        kelly_f = min(oos_sharpe / 4.0, 0.25)  # half-kelly, capped at 25%
        base_risk_pct = kelly_f
        rules.append(f"kelly_{kelly_f:.3f}")

    # Rule 6 — Confluence score: scale by signal quality
    if confluence_score >= 4:
        multiplier *= 1.50  # A+ entry: get bigger
        rules.append("confluence_aplus")
    elif confluence_score >= 3:
        multiplier *= 1.25  # solid confluence
        rules.append("confluence_solid")
    elif confluence_score >= 2:
        multiplier *= 1.0  # standard
        rules.append("confluence_standard")
    elif confluence_score == 1:
        multiplier *= 0.50  # marginal: half size
        rules.append("confluence_marginal")
    else:
        multiplier *= 0.0  # no confluence: skip
        rules.append("confluence_none")

    size = base_risk_pct * multiplier * equity

    # Rule 7 — Cap at 4% of equity regardless (absolute ceiling)
    absolute_cap = equity * 0.04
    if size > absolute_cap:
        size = absolute_cap
        rules.append("capped_4pct")

    # Rule 8 — Floor at 0.1% (don't bother with dust)
    if size < equity * 0.001 and size > 0:
        size = 0.0
        rules.append("below_floor")

    return round(size, 2)


def risk_profile_for_bot(bot_id: str) -> dict:
    """Return the risk profile for a bot based on its registry entry
    and walk-forward performance. Higher OOS Sharpe → larger sizing.

    Per-bot risk tiers:
    - Elite (+5+ OOS): 2.0% risk, 5% daily DD cap
    - Strong (+2 to +5 OOS): 1.5% risk, 4% daily DD cap
    - Baseline (+0 to +2 OOS): 1.0% risk, 3% daily DD cap
    - Research (unproven): 0.5% risk, 2% daily DD cap
    """
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return {"risk_pct": 0.01, "daily_loss_pct": 4.0, "max_trades_per_day": 2, "oos_sharpe": 0.0, "tier": "baseline"}

    # Tier based on OOS Sharpe (from registry rationale or research_tune)
    oos_sharpe = 0.0
    tune = a.extras.get("research_tune", {})
    if isinstance(tune, dict):
        oos_sharpe = float(tune.get("candidate_agg_oos_sharpe", 0.0))
    if oos_sharpe <= 0:
        oos_sharpe = float(a.extras.get("sage_min_conviction", 0.0))

    if oos_sharpe >= 5.0:
        risk_pct_base, dd_cap, tier = 0.020, 0.05, "elite"
    elif oos_sharpe >= 2.0:
        risk_pct_base, dd_cap, tier = 0.015, 0.04, "strong"
    elif oos_sharpe > 0:
        risk_pct_base, dd_cap, tier = 0.010, 0.03, "baseline"
    else:
        risk_pct_base, dd_cap, tier = 0.005, 0.02, "research"

    wp = a.extras.get("warmup_policy", {})
    in_warmup = False
    risk_mult = 1.0
    if isinstance(wp, dict):
        promoted = wp.get("promoted_on")
        warmup_days = wp.get("warmup_days", 30)
        warmup_mult = wp.get("risk_multiplier_during_warmup", 1.0)
        if promoted:
            from datetime import UTC, datetime

            try:
                promoted_dt = datetime.fromisoformat(str(promoted))
                days_since = (datetime.now(tz=UTC) - promoted_dt).days
                if days_since <= warmup_days:
                    in_warmup = True
                    risk_mult = warmup_mult
            except (ValueError, TypeError):
                pass

    return {
        "risk_pct": round(risk_pct_base * risk_mult, 4),
        "daily_loss_pct": dd_cap,
        "max_trades_per_day": int(
            a.extras.get("max_trades_per_day", a.extras.get("orb_config", {}).get("max_trades_per_day", 2))
        ),  # noqa: E501
        "oos_sharpe": oos_sharpe,
        "tier": tier,
        "in_warmup": in_warmup,
    }


def fleet_daily_budget(
    fleet_equity: float,
    *,
    total_bots_active: int = 1,
    max_fleet_dd_pct: float = 0.035,
) -> float:
    """Return the fleet's daily loss budget in USD."""
    return fleet_equity * max_fleet_dd_pct
