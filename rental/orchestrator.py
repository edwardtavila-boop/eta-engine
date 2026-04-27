"""
EVOLUTIONARY TRADING ALGO  //  rental.orchestrator
======================================
Multi-tenant container orchestration: one isolated bot instance per tenant.

The orchestrator does NOT run containers itself. It produces ``TenantContainerSpec``
records that a Docker / Kubernetes driver consumes out-of-band. The spec is a
pure data object so the same code drives:
  * local ``docker compose`` dev -- a driver writes ``docker-compose.override.yml``
  * prod k8s -- a driver produces manifests via kubernetes-python

Isolation guarantees
--------------------
  * Separate container per tenant (namespaced).
  * Per-tenant Postgres schema + Redis namespace (env-injected).
  * Read-only mount of the strategy container image -- tenants can never
    tamper with our code at runtime.
  * CPU / memory caps per tier.
  * Outbound network restricted to the exchange endpoints declared in the
    tier config (enforced by the driver, not here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from eta_engine.rental.tenancy import Tenant, TenantStatus
from eta_engine.rental.tiers import BotSku, RentalTier


class InstanceState(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TenantContainerSpec:
    """Declarative container description for one tenant instance.

    The driver turns this into actual docker / k8s resources. Every field is
    JSON-serializable so it can be persisted in the tenant registry and
    replayed if a tenant's pod needs to be rescheduled.
    """

    tenant_id: str
    sku: BotSku
    tier: RentalTier
    image: str = "evolutionarytradingalgo/bot:current"
    cpu_limit: str = "0.5"  # cores (overridden per tier below)
    memory_limit: str = "512Mi"
    env: dict[str, str] = field(default_factory=dict)
    mounts_read_only: tuple[str, ...] = ()
    network_allowlist: tuple[str, ...] = ()
    restart_policy: str = "on-failure"
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class TenantInstanceState:
    tenant_id: str
    sku: BotSku
    state: InstanceState = InstanceState.PLANNED
    last_transition_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: str = ""


# Per-tier resource table. Bigger tiers get more CPU + memory.
_TIER_CPU_MEM: dict[RentalTier, tuple[str, str]] = {
    RentalTier.TRIAL: ("0.25", "256Mi"),
    RentalTier.STARTER: ("0.5", "512Mi"),
    RentalTier.PRO: ("1.0", "1Gi"),
    RentalTier.PORTFOLIO: ("2.0", "2Gi"),
    RentalTier.ELITE: ("4.0", "4Gi"),
}


class MultiTenantOrchestrator:
    """Plans + tracks per-tenant bot instances.

    The orchestrator keeps a lightweight state table (``instance_states``) and
    exposes pure methods: given a tenant, it produces the container specs
    that should be running, and given a driver callback it drives transitions.

    It is intentionally ignorant of Docker / k8s -- callers hand in a
    ``driver_apply(spec)`` callable that knows how to talk to whichever
    runtime is available.
    """

    def __init__(self) -> None:
        self._specs: dict[tuple[str, BotSku], TenantContainerSpec] = {}
        self._states: dict[tuple[str, BotSku], TenantInstanceState] = {}

    # -- planning -----------------------------------------------------------

    def plan(self, tenant: Tenant) -> list[TenantContainerSpec]:
        """Return the list of container specs this tenant SHOULD have running
        given their entitlement.

        Idempotent: calling it twice produces the same specs. Tenants in
        CANCELLED / PAUSED return an empty list (driver should stop them).
        """
        if tenant.status != TenantStatus.ACTIVE or tenant.entitlement is None:
            return []

        specs: list[TenantContainerSpec] = []
        cpu, mem = _TIER_CPU_MEM.get(tenant.tier, ("0.5", "512Mi"))
        for sku in tenant.entitlement.bot_skus:
            if sku not in tenant.api_keys:
                continue  # skip until customer provisions trade keys
            key = tenant.api_keys[sku]
            specs.append(
                TenantContainerSpec(
                    tenant_id=tenant.tenant_id,
                    sku=sku,
                    tier=tenant.tier,
                    cpu_limit=cpu,
                    memory_limit=mem,
                    env={
                        "APEX_TENANT_ID": tenant.tenant_id,
                        "APEX_SKU": sku.value,
                        "APEX_TIER": tenant.tier.value,
                        "APEX_EXCHANGE": key.exchange,
                        "APEX_KEY_ID": key.key_id,
                        # NOTE: secrets are NOT injected here -- the driver
                        # pulls them from KMS at mount time using key_id.
                        "APEX_PAPER_FALLBACK": "1",
                    },
                    mounts_read_only=(
                        "/opt/apex/brain:ro",
                        "/opt/apex/funnel:ro",
                    ),
                    network_allowlist=(
                        "api.bybit.com",
                        "api.okx.com",
                        "stream.bybit.com",
                    ),
                    labels={
                        "apex.tenant": tenant.tenant_id,
                        "apex.sku": sku.value,
                        "apex.tier": tenant.tier.value,
                    },
                ),
            )
        return specs

    def reconcile(self, tenant: Tenant) -> list[TenantContainerSpec]:
        """Diff desired vs stored; return the new desired specs and update state."""
        desired = self.plan(tenant)
        # Replace stored specs for this tenant wholesale.
        drop = [(tid, sku) for (tid, sku) in self._specs if tid == tenant.tenant_id]
        for k in drop:
            self._specs.pop(k, None)
        for spec in desired:
            key = (spec.tenant_id, spec.sku)
            self._specs[key] = spec
            self._states.setdefault(
                key,
                TenantInstanceState(
                    tenant_id=spec.tenant_id,
                    sku=spec.sku,
                    state=InstanceState.PLANNED,
                ),
            )
        return desired

    # -- state tracking -----------------------------------------------------

    def mark(
        self,
        *,
        tenant_id: str,
        sku: BotSku,
        state: InstanceState,
        note: str = "",
    ) -> TenantInstanceState:
        key = (tenant_id, sku)
        rec = self._states.get(key) or TenantInstanceState(tenant_id=tenant_id, sku=sku)
        rec.state = state
        rec.last_transition_utc = datetime.now(UTC)
        rec.last_error = note if state == InstanceState.FAILED else ""
        self._states[key] = rec
        return rec

    def instance_state(
        self,
        tenant_id: str,
        sku: BotSku,
    ) -> TenantInstanceState | None:
        return self._states.get((tenant_id, sku))

    def all_specs(self) -> list[TenantContainerSpec]:
        return list(self._specs.values())

    def all_states(self) -> list[TenantInstanceState]:
        return list(self._states.values())
