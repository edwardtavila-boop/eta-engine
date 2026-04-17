"""
EVOLUTIONARY TRADING ALGO  //  obs
======================
Observability: metrics, alerts, structured logs, heartbeat monitoring.
Keep this runtime honest. Every fill, every kill, every bot silence -- recorded.
"""

from __future__ import annotations

from eta_engine.obs.alerts import (
    Alert,
    AlertLevel,
    BaseAlerter,
    DiscordAlerter,
    MultiAlerter,
    SlackAlerter,
    TelegramAlerter,
)
from eta_engine.obs.heartbeat import HeartbeatMonitor
from eta_engine.obs.logger import StructuredLogger
from eta_engine.obs.metrics import (
    REGISTRY,
    Metric,
    MetricsRegistry,
    MetricType,
)

__all__ = [
    "REGISTRY",
    "Alert",
    "AlertLevel",
    "BaseAlerter",
    "DiscordAlerter",
    "HeartbeatMonitor",
    "Metric",
    "MetricType",
    "MetricsRegistry",
    "MultiAlerter",
    "SlackAlerter",
    "StructuredLogger",
    "TelegramAlerter",
]
