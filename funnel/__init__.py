"""
EVOLUTIONARY TRADING ALGO  //  funnel
=========================
Equity monitoring, inter-bot transfers, cold-wallet sweep.
Money flows up or out. Never sideways.
"""

from eta_engine.funnel.equity_monitor import (
    BotEquity,
    EquityMonitor,
    PortfolioState,
)
from eta_engine.funnel.fiat_to_crypto import (
    CryptoTarget,
    FiatSource,
    OnrampPipeline,
    OnrampPolicy,
    OnrampProvider,
    OnrampRequest,
    OnrampStage,
    OnrampState,
    StubOnrampExecutor,
)
from eta_engine.funnel.integrations import (
    BotIntegration,
    FunnelLayer,
    IntegrationsReport,
    ObservabilityIntegration,
    OnrampRoute,
    StakingIntegration,
    VenueIntegration,
    build_integrations_report,
    canonical_bots,
    canonical_funnel_layers,
    canonical_observability,
    canonical_onramp_routes,
    canonical_staking,
    canonical_venues,
    render_text,
)
from eta_engine.funnel.orchestrator import FunnelOrchestrator, FunnelTickResult
from eta_engine.funnel.transfer import (
    TransferRequest,
    TransferResult,
    execute_transfer,
    sweep_to_cold,
)

__all__ = [
    "BotEquity",
    "BotIntegration",
    "CryptoTarget",
    "EquityMonitor",
    "FiatSource",
    "FunnelLayer",
    "FunnelOrchestrator",
    "FunnelTickResult",
    "IntegrationsReport",
    "ObservabilityIntegration",
    "OnrampPipeline",
    "OnrampPolicy",
    "OnrampProvider",
    "OnrampRequest",
    "OnrampRoute",
    "OnrampStage",
    "OnrampState",
    "PortfolioState",
    "StakingIntegration",
    "StubOnrampExecutor",
    "TransferRequest",
    "TransferResult",
    "VenueIntegration",
    "build_integrations_report",
    "canonical_bots",
    "canonical_funnel_layers",
    "canonical_observability",
    "canonical_onramp_routes",
    "canonical_staking",
    "canonical_venues",
    "execute_transfer",
    "render_text",
    "sweep_to_cold",
]
