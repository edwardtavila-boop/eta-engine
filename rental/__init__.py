"""
EVOLUTIONARY TRADING ALGO  //  rental
=========================
SaaS rental layer: lets consumers rent usage of the APEX bot framework
monthly / quarterly / annually without ever seeing the code.

Design
------
  * One isolated instance per tenant (docker/k8s container spec in
    ``rental.orchestrator``).
  * Trade-only API keys stored in ``rental.tenancy`` -- we NEVER see
    withdrawal permissions, we NEVER touch customer funds.
  * Thin downloadable client (Electron/Tauri) talks to the rental
    backend via the typed contract in ``rental.client_contract``.
    The client never contains strategy code.
  * Billing state machine in ``rental.billing`` handles trial,
    renewal, grace, cancel.

Law
---
Every entry point MUST assert ``Tenant.is_entitled()`` before taking
any action on behalf of a customer. No exceptions. Unauthenticated
flows raise ``PermissionError``.
"""

from eta_engine.rental.billing import (
    BillingCycle,
    BillingEvent,
    EventKind,
    Subscription,
    SubscriptionStatus,
)
from eta_engine.rental.client_contract import (
    ClientCommand,
    ClientCommandKind,
    ServerMessage,
    ServerMessageKind,
    make_hello,
    make_status_update,
    validate_command,
)
from eta_engine.rental.orchestrator import (
    MultiTenantOrchestrator,
    TenantContainerSpec,
    TenantInstanceState,
)
from eta_engine.rental.tenancy import (
    ApiKeyScope,
    Entitlement,
    Tenant,
    TenantRegistry,
    TenantStatus,
)
from eta_engine.rental.tiers import (
    BotSku,
    RentalTier,
    Tier,
    TierCatalog,
    price_for,
)

__all__ = [
    "ApiKeyScope",
    "BillingCycle",
    "BillingEvent",
    "BotSku",
    "ClientCommand",
    "ClientCommandKind",
    "Entitlement",
    "EventKind",
    "MultiTenantOrchestrator",
    "RentalTier",
    "ServerMessage",
    "ServerMessageKind",
    "Subscription",
    "SubscriptionStatus",
    "Tenant",
    "TenantContainerSpec",
    "TenantInstanceState",
    "TenantRegistry",
    "TenantStatus",
    "Tier",
    "TierCatalog",
    "make_hello",
    "make_status_update",
    "price_for",
    "validate_command",
]
