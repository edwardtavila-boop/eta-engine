"""
EVOLUTIONARY TRADING ALGO  //  core
======================
Trading infrastructure. No safety nets.

Modules:
    risk_engine       - Position sizing, Kelly, drawdown kills
    confluence_scorer - 0-10 multi-factor signal scoring
    session_filter    - Session windows + news blackout gates
    data_pipeline     - Async feed abstraction (Bybit, Tradovate)
    sweep_engine      - Profit funnel sweep allocation
"""

from eta_engine.core.confluence_scorer import (
    ConfluenceFactor,
    ConfluenceResult,
    score_confluence,
)
from eta_engine.core.data_pipeline import (
    BarData,
    BybitFeed,
    DataFeed,
    FundingRate,
    L2Snapshot,
    TradovateFeed,
)
from eta_engine.core.risk_engine import (
    RiskTier,
    calculate_max_leverage,
    check_daily_loss_cap,
    check_max_drawdown_kill,
    dynamic_position_size,
    fractional_kelly,
    liquidation_distance,
)
from eta_engine.core.session_filter import (
    HIGH_IMPACT_TAGS,
    SessionWindow,
    is_htf_window,
    is_news_blackout,
)
from eta_engine.core.sweep_engine import (
    SweepConfig,
    SweepResult,
    check_sweep,
    execute_sweep,
)

__all__ = [
    # risk_engine
    "RiskTier",
    "calculate_max_leverage",
    "check_daily_loss_cap",
    "check_max_drawdown_kill",
    "dynamic_position_size",
    "fractional_kelly",
    "liquidation_distance",
    # confluence_scorer
    "ConfluenceFactor",
    "ConfluenceResult",
    "score_confluence",
    # session_filter
    "HIGH_IMPACT_TAGS",
    "SessionWindow",
    "is_htf_window",
    "is_news_blackout",
    # data_pipeline
    "BarData",
    "BybitFeed",
    "DataFeed",
    "FundingRate",
    "L2Snapshot",
    "TradovateFeed",
    # sweep_engine
    "SweepConfig",
    "SweepResult",
    "check_sweep",
    "execute_sweep",
]
