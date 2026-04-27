"""
EVOLUTIONARY TRADING ALGO  //  tests.test_rental_tenancy
============================================
Tenant, Entitlement, ApiKeyRecord, and TenantRegistry behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.rental.tenancy import (
    ApiKeyRecord,
    ApiKeyScope,
    Entitlement,
    Tenant,
    TenantRegistry,
    TenantStatus,
    build_active_tenant,
    entitlement_from_tier,
)
from eta_engine.rental.tiers import (
    DEFAULT_CATALOG,
    PORTFOLIO,
    BotSku,
    RentalTier,
)

# ---------------------------------------------------------------------------
# ApiKeyRecord
# ---------------------------------------------------------------------------


def test_api_key_record_from_secret_hashes_and_drops_secret() -> None:
    rec = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="key_abc",
        secret="sooper-sekret",
    )
    # Raw secret must never appear anywhere on the record
    for value in (rec.secret_sha256, rec.salt_hex, rec.key_id, rec.exchange):
        assert "sooper-sekret" not in value
    # Digest must be a realistic sha256 hex (64 chars)
    assert len(rec.secret_sha256) == 64
    # Each call must produce a fresh salt
    rec2 = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="key_abc",
        secret="sooper-sekret",
    )
    assert rec.salt_hex != rec2.salt_hex
    assert rec.secret_sha256 != rec2.secret_sha256


def test_api_key_with_withdraw_permission_is_rejected() -> None:
    rec = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="key_bad",
        secret="x",
        declared_permissions=("TRADE", "WITHDRAW"),
    )
    assert rec.scope is ApiKeyScope.REJECTED
    assert rec.is_safe is False


def test_trade_only_key_is_safe() -> None:
    rec = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="key_ok",
        secret="x",
        declared_permissions=("TRADE",),
    )
    assert rec.scope is ApiKeyScope.TRADE_ONLY
    assert rec.is_safe is True


# ---------------------------------------------------------------------------
# Entitlement
# ---------------------------------------------------------------------------


def _future(days: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _past(days: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def test_entitlement_active_when_not_expired() -> None:
    ent = entitlement_from_tier(
        tenant_id="t1",
        tier=PORTFOLIO,
        expires_utc=_future(30),
    )
    ok, reason = ent.is_entitled_now()
    assert ok
    assert reason == "ok"


def test_entitlement_expired_blocks() -> None:
    ent = entitlement_from_tier(
        tenant_id="t1",
        tier=PORTFOLIO,
        expires_utc=_past(1),
    )
    ok, reason = ent.is_entitled_now()
    assert not ok
    assert "expired" in reason


def test_entitlement_sku_membership() -> None:
    ent = entitlement_from_tier(
        tenant_id="t1",
        tier=PORTFOLIO,
        expires_utc=_future(30),
    )
    ok, _ = ent.is_entitled_sku(BotSku.BTC_SEED)
    assert ok
    ok, reason = ent.is_entitled_sku(BotSku.MNQ_APEX)
    assert not ok
    assert "MNQ_APEX" in reason


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------


def _active_tenant() -> Tenant:
    return build_active_tenant(
        tenant_id="cust_1",
        email="c@example.com",
        tier_id=RentalTier.PORTFOLIO,
        expires_utc=_future(30),
    )


def test_attach_safe_key_succeeds() -> None:
    tenant = _active_tenant()
    key = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="k1",
        secret="x",
        declared_permissions=("TRADE",),
    )
    tenant.attach_key(BotSku.BTC_SEED, key)
    assert tenant.api_keys[BotSku.BTC_SEED] is key


def test_attach_unsafe_key_raises() -> None:
    tenant = _active_tenant()
    bad = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="k1",
        secret="x",
        declared_permissions=("WITHDRAW",),
    )
    with pytest.raises(PermissionError, match="unsafe"):
        tenant.attach_key(BotSku.BTC_SEED, bad)


def test_is_entitled_composite_ok() -> None:
    tenant = _active_tenant()
    key = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="k1",
        secret="x",
        declared_permissions=("TRADE",),
    )
    tenant.attach_key(BotSku.BTC_SEED, key)
    ok, reason = tenant.is_entitled(BotSku.BTC_SEED)
    assert ok, reason


def test_is_entitled_fails_when_tenant_not_active() -> None:
    tenant = _active_tenant()
    tenant.status = TenantStatus.PAUSED
    ok, reason = tenant.is_entitled(BotSku.BTC_SEED)
    assert not ok
    assert "not ACTIVE" in reason


def test_is_entitled_fails_without_key() -> None:
    tenant = _active_tenant()
    # No key attached
    ok, reason = tenant.is_entitled(BotSku.BTC_SEED)
    assert not ok
    assert "no api key" in reason


def test_is_entitled_fails_on_out_of_tier_sku() -> None:
    # PORTFOLIO excludes MNQ_APEX
    tenant = _active_tenant()
    key = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="k1",
        secret="x",
        declared_permissions=("TRADE",),
    )
    tenant.attach_key(BotSku.BTC_SEED, key)
    ok, reason = tenant.is_entitled(BotSku.MNQ_APEX)
    assert not ok
    assert "MNQ_APEX" in reason


def test_is_entitled_fails_after_expiry() -> None:
    tenant = build_active_tenant(
        tenant_id="cust_1",
        email="c@example.com",
        tier_id=RentalTier.STARTER,
        expires_utc=_past(1),
    )
    key = ApiKeyRecord.from_secret(
        exchange="bybit",
        key_id="k1",
        secret="x",
        declared_permissions=("TRADE",),
    )
    tenant.attach_key(BotSku.BTC_SEED, key)
    ok, reason = tenant.is_entitled(BotSku.BTC_SEED)
    assert not ok
    assert "expired" in reason


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_add_and_get() -> None:
    reg = TenantRegistry()
    t = _active_tenant()
    reg.add(t)
    assert reg.get("cust_1") is t
    assert len(reg) == 1


def test_registry_duplicate_raises() -> None:
    reg = TenantRegistry()
    reg.add(_active_tenant())
    with pytest.raises(ValueError, match="already exists"):
        reg.add(_active_tenant())


def test_registry_active_tenants_filters() -> None:
    reg = TenantRegistry()
    active = _active_tenant()
    cancelled = build_active_tenant(
        tenant_id="cust_2",
        email="x@example.com",
        tier_id=RentalTier.STARTER,
        expires_utc=_future(30),
    )
    cancelled.status = TenantStatus.CANCELLED
    reg.add(active)
    reg.add(cancelled)
    actives = reg.active_tenants()
    assert len(actives) == 1
    assert actives[0] is active


def test_registry_get_unknown_raises() -> None:
    reg = TenantRegistry()
    with pytest.raises(KeyError):
        reg.get("ghost")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def test_build_active_tenant_populates_entitlement() -> None:
    t = build_active_tenant(
        tenant_id="cust_9",
        email="e@example.com",
        tier_id=RentalTier.PRO,
        expires_utc=_future(30),
        catalog=DEFAULT_CATALOG,
    )
    assert t.status is TenantStatus.ACTIVE
    assert t.entitlement is not None
    assert t.entitlement.tier is RentalTier.PRO
    assert BotSku.BTC_SEED in t.entitlement.bot_skus
    assert BotSku.ETH_PERP in t.entitlement.bot_skus


def test_entitlement_from_tier_uses_tier_fields() -> None:
    ent = entitlement_from_tier(
        tenant_id="t1",
        tier=PORTFOLIO,
        expires_utc=_future(30),
    )
    assert ent.max_concurrent_positions == PORTFOLIO.max_concurrent_positions
    assert ent.max_equity_managed_usd == PORTFOLIO.max_equity_managed_usd
    assert ent.includes_custom_tweaks is PORTFOLIO.includes_custom_tweaks


def test_frozen_entitlement_immutable() -> None:
    ent = Entitlement(
        tenant_id="t1",
        tier=RentalTier.STARTER,
        bot_skus=frozenset({BotSku.BTC_SEED}),
        max_concurrent_positions=3,
        max_equity_managed_usd=25_000,
        includes_custom_tweaks=False,
        expires_utc=_future(30),
    )
    with pytest.raises((AttributeError, TypeError)):  # frozen dataclass
        ent.max_concurrent_positions = 999  # type: ignore[misc]
