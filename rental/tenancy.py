"""
EVOLUTIONARY TRADING ALGO  //  rental.tenancy
=================================
Per-tenant isolation, entitlements, API-key handling.

Rules
-----
  * Customer API keys MUST be trade-only (no withdrawal / transfer scopes).
  * Keys are stored opaquely -- this module exposes a hash and the
    declared scope, never the raw secret. A real deployment uses a
    KMS/Vault; the in-memory stub mirrors that shape so tests don't need
    live infrastructure.
  * Every entitlement check returns (allowed, reason). Callers log the
    reason before dropping the request.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from eta_engine.rental.tiers import BotSku, RentalTier, Tier, TierCatalog


class TenantStatus(StrEnum):
    PENDING = "PENDING"  # record created, waiting for payment + keys
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"  # billing grace / manual pause
    CANCELLED = "CANCELLED"  # sub ended, entitlements stripped


class ApiKeyScope(StrEnum):
    """What the customer key is permitted to do at their exchange."""

    TRADE_ONLY = "TRADE_ONLY"
    READ_ONLY = "READ_ONLY"
    REJECTED = "REJECTED"  # anything with withdrawal permission lands here


@dataclass(frozen=True)
class ApiKeyRecord:
    """Opaque reference to a customer API key.

    In production this is a KMS key id; in-memory we hold a salted SHA-256
    digest of the secret so tests + local dev have a real hash shape without
    ever touching the raw bytes after creation.
    """

    exchange: str  # "bybit", "okx", "binance"
    key_id: str  # public half of the exchange key
    secret_sha256: str  # salted digest, NEVER the raw
    salt_hex: str  # per-key salt
    scope: ApiKeyScope = ApiKeyScope.TRADE_ONLY
    declared_permissions: tuple[str, ...] = ()

    @property
    def is_safe(self) -> bool:
        return self.scope == ApiKeyScope.TRADE_ONLY and "WITHDRAW" not in self.declared_permissions

    @staticmethod
    def from_secret(
        *,
        exchange: str,
        key_id: str,
        secret: str,
        declared_permissions: tuple[str, ...] = (),
    ) -> ApiKeyRecord:
        """Build a record from a raw secret. The secret is hashed + dropped."""
        salt = secrets.token_bytes(16)
        digest = hashlib.sha256(salt + secret.encode("utf-8")).hexdigest()
        scope = ApiKeyScope.REJECTED if "WITHDRAW" in declared_permissions else ApiKeyScope.TRADE_ONLY
        return ApiKeyRecord(
            exchange=exchange,
            key_id=key_id,
            secret_sha256=digest,
            salt_hex=salt.hex(),
            scope=scope,
            declared_permissions=declared_permissions,
        )


@dataclass(frozen=True)
class Entitlement:
    """What a tenant's paid-up subscription currently permits."""

    tenant_id: str
    tier: RentalTier
    bot_skus: frozenset[BotSku]
    max_concurrent_positions: int
    max_equity_managed_usd: int | None
    includes_custom_tweaks: bool
    expires_utc: datetime

    def is_entitled_now(self, *, now: datetime | None = None) -> tuple[bool, str]:
        now = now if now is not None else datetime.now(UTC)
        if now >= self.expires_utc:
            return False, f"entitlement expired at {self.expires_utc.isoformat()}"
        return True, "ok"

    def is_entitled_sku(self, sku: BotSku) -> tuple[bool, str]:
        if sku not in self.bot_skus:
            return False, f"sku {sku.value} not in entitlement ({self.tier.value})"
        return True, "ok"


@dataclass
class Tenant:
    """One rental customer."""

    tenant_id: str
    email: str
    tier: RentalTier
    status: TenantStatus = TenantStatus.PENDING
    created_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    entitlement: Entitlement | None = None
    api_keys: dict[BotSku, ApiKeyRecord] = field(default_factory=dict)
    # arbitrary per-tenant overrides the customer has bought (e.g. size caps)
    custom_tweaks: dict[str, float] = field(default_factory=dict)

    def attach_key(self, sku: BotSku, key: ApiKeyRecord) -> None:
        if not key.is_safe:
            raise PermissionError(
                f"API key for {sku.value} rejected: unsafe scope "
                f"{key.scope.value} with perms {key.declared_permissions}",
            )
        self.api_keys[sku] = key

    def is_entitled(
        self,
        sku: BotSku,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """Composite check: tier includes SKU, sub active, not expired, key attached."""
        if self.status != TenantStatus.ACTIVE:
            return False, f"tenant not ACTIVE (status={self.status.value})"
        if self.entitlement is None:
            return False, "no entitlement record"
        ok, why = self.entitlement.is_entitled_now(now=now)
        if not ok:
            return False, why
        ok, why = self.entitlement.is_entitled_sku(sku)
        if not ok:
            return False, why
        if sku not in self.api_keys:
            return False, f"no api key attached for {sku.value}"
        if not self.api_keys[sku].is_safe:
            return False, f"api key for {sku.value} has unsafe scope"
        return True, "ok"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TenantRegistry:
    """In-memory tenant registry.

    In production this is a row-level-secured table in Postgres + a Redis
    cache; the in-memory registry mirrors the same API so unit tests and the
    CLI provisioner can use it unchanged.
    """

    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}

    def add(self, tenant: Tenant) -> None:
        if tenant.tenant_id in self._tenants:
            raise ValueError(f"tenant {tenant.tenant_id} already exists")
        self._tenants[tenant.tenant_id] = tenant

    def get(self, tenant_id: str) -> Tenant:
        if tenant_id not in self._tenants:
            raise KeyError(f"no tenant {tenant_id}")
        return self._tenants[tenant_id]

    def remove(self, tenant_id: str) -> None:
        self._tenants.pop(tenant_id, None)

    def active_tenants(self) -> list[Tenant]:
        return [t for t in self._tenants.values() if t.status == TenantStatus.ACTIVE]

    def __len__(self) -> int:
        return len(self._tenants)


def entitlement_from_tier(
    *,
    tenant_id: str,
    tier: Tier,
    expires_utc: datetime,
) -> Entitlement:
    """Build an Entitlement from a Tier row.

    Keeps tier -> entitlement projection in one place so we can evolve the
    tier catalog without touching every caller.
    """
    return Entitlement(
        tenant_id=tenant_id,
        tier=tier.id,
        bot_skus=tier.bot_skus,
        max_concurrent_positions=tier.max_concurrent_positions,
        max_equity_managed_usd=tier.max_equity_managed_usd,
        includes_custom_tweaks=tier.includes_custom_tweaks,
        expires_utc=expires_utc,
    )


def build_active_tenant(
    *,
    tenant_id: str,
    email: str,
    tier_id: RentalTier,
    expires_utc: datetime,
    catalog: TierCatalog | None = None,
) -> Tenant:
    """Convenience for provisioning flows: one call -> active tenant."""
    cat = catalog if catalog is not None else TierCatalog()
    tier = cat.by_id(tier_id)
    ent = entitlement_from_tier(
        tenant_id=tenant_id,
        tier=tier,
        expires_utc=expires_utc,
    )
    return Tenant(
        tenant_id=tenant_id,
        email=email,
        tier=tier_id,
        status=TenantStatus.ACTIVE,
        entitlement=ent,
    )
