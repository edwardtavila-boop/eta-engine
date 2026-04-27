"""
EVOLUTIONARY TRADING ALGO  //  tests.test_rental_orchestrator
=================================================
Multi-tenant container planning + isolation guarantees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.rental.orchestrator import (
    InstanceState,
    MultiTenantOrchestrator,
    TenantContainerSpec,
)
from eta_engine.rental.tenancy import (
    ApiKeyRecord,
    TenantStatus,
    build_active_tenant,
)
from eta_engine.rental.tiers import BotSku, RentalTier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(days: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _key(exchange: str = "bybit", key_id: str = "k1") -> ApiKeyRecord:
    return ApiKeyRecord.from_secret(
        exchange=exchange,
        key_id=key_id,
        secret="x",
        declared_permissions=("TRADE",),
    )


def _portfolio_tenant() -> object:
    t = build_active_tenant(
        tenant_id="cust_1",
        email="c@example.com",
        tier_id=RentalTier.PORTFOLIO,
        expires_utc=_future(30),
    )
    t.attach_key(BotSku.BTC_SEED, _key(key_id="btc"))
    t.attach_key(BotSku.ETH_PERP, _key(key_id="eth"))
    t.attach_key(BotSku.SOL_PERP, _key(key_id="sol"))
    t.attach_key(BotSku.STAKING_SWEEP, _key(key_id="stake"))
    return t


# ---------------------------------------------------------------------------
# plan()
# ---------------------------------------------------------------------------


def test_plan_emits_spec_per_sku_with_attached_key() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    specs = orch.plan(tenant)
    skus = {s.sku for s in specs}
    assert BotSku.BTC_SEED in skus
    assert BotSku.ETH_PERP in skus
    assert BotSku.SOL_PERP in skus
    assert BotSku.STAKING_SWEEP in skus
    # No MNQ_APEX on PORTFOLIO tier
    assert BotSku.MNQ_APEX not in skus


def test_plan_skips_sku_without_key() -> None:
    orch = MultiTenantOrchestrator()
    t = build_active_tenant(
        tenant_id="cust_2",
        email="c@example.com",
        tier_id=RentalTier.PRO,
        expires_utc=_future(30),
    )
    # Only attach one of the two SKUs PRO grants
    t.attach_key(BotSku.BTC_SEED, _key(key_id="btc"))
    specs = orch.plan(t)
    skus = {s.sku for s in specs}
    assert BotSku.BTC_SEED in skus
    assert BotSku.ETH_PERP not in skus


def test_plan_empty_for_paused_tenant() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    tenant.status = TenantStatus.PAUSED
    assert orch.plan(tenant) == []


def test_plan_empty_for_cancelled_tenant() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    tenant.status = TenantStatus.CANCELLED
    assert orch.plan(tenant) == []


def test_plan_empty_when_entitlement_missing() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    tenant.entitlement = None
    assert orch.plan(tenant) == []


# ---------------------------------------------------------------------------
# Isolation guarantees (the whole reason this module exists)
# ---------------------------------------------------------------------------


def test_spec_env_injects_tenant_and_sku() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    specs = orch.plan(tenant)
    spec = next(s for s in specs if s.sku is BotSku.BTC_SEED)
    assert spec.env["APEX_TENANT_ID"] == tenant.tenant_id
    assert spec.env["APEX_SKU"] == BotSku.BTC_SEED.value
    assert spec.env["APEX_TIER"] == RentalTier.PORTFOLIO.value
    assert spec.env["APEX_EXCHANGE"] == "bybit"


def test_spec_never_injects_raw_secret() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    specs = orch.plan(tenant)
    for spec in specs:
        for k, v in spec.env.items():
            # Secret byte "x" could appear in innocent places, but never as
            # a standalone secret env var.
            assert k != "APEX_SECRET"
            assert "SECRET" not in k
            assert not v.startswith("WITHDRAW")


def test_spec_mounts_strategy_readonly() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    specs = orch.plan(tenant)
    spec = specs[0]
    # Every mount must be read-only
    for mount in spec.mounts_read_only:
        assert mount.endswith(":ro"), f"mount not read-only: {mount}"
    mounted = {m.split(":")[0] for m in spec.mounts_read_only}
    assert "/opt/apex/brain" in mounted
    assert "/opt/apex/funnel" in mounted


def test_spec_network_allowlist_is_restricted() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    spec = orch.plan(tenant)[0]
    # Explicit exchange endpoints, nothing more
    allowed = set(spec.network_allowlist)
    assert "api.bybit.com" in allowed
    assert "api.okx.com" in allowed
    # No wildcards
    for host in allowed:
        assert "*" not in host


def test_specs_have_per_tier_resources() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    spec = orch.plan(tenant)[0]
    # PORTFOLIO: 2 cores / 2Gi
    assert spec.cpu_limit == "2.0"
    assert spec.memory_limit == "2Gi"


def test_specs_have_tenant_labels() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    spec = orch.plan(tenant)[0]
    assert spec.labels["apex.tenant"] == tenant.tenant_id
    assert spec.labels["apex.sku"] == spec.sku.value
    assert spec.labels["apex.tier"] == RentalTier.PORTFOLIO.value


# ---------------------------------------------------------------------------
# reconcile() + state tracking
# ---------------------------------------------------------------------------


def test_reconcile_stores_specs_and_seeds_planned_state() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    desired = orch.reconcile(tenant)
    assert len(desired) == len(tenant.entitlement.bot_skus)
    states = {(s.tenant_id, s.sku): s for s in orch.all_states()}
    for spec in desired:
        key = (spec.tenant_id, spec.sku)
        assert states[key].state is InstanceState.PLANNED


def test_reconcile_replaces_stored_specs_on_tier_downgrade() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    orch.reconcile(tenant)
    # Simulate downgrade -> drop entitlement to STARTER (only BTC_SEED)
    from eta_engine.rental.tenancy import entitlement_from_tier
    from eta_engine.rental.tiers import STARTER as STARTER_TIER

    tenant.tier = RentalTier.STARTER
    tenant.entitlement = entitlement_from_tier(
        tenant_id=tenant.tenant_id,
        tier=STARTER_TIER,
        expires_utc=_future(30),
    )
    orch.reconcile(tenant)
    # Only BTC_SEED spec remains for this tenant
    tenant_specs = [s for s in orch.all_specs() if s.tenant_id == tenant.tenant_id]
    assert {s.sku for s in tenant_specs} == {BotSku.BTC_SEED}


def test_mark_failure_records_error() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    orch.reconcile(tenant)
    rec = orch.mark(
        tenant_id=tenant.tenant_id,
        sku=BotSku.BTC_SEED,
        state=InstanceState.FAILED,
        note="bybit ws auth error",
    )
    assert rec.state is InstanceState.FAILED
    assert rec.last_error == "bybit ws auth error"


def test_instance_state_returns_none_for_unknown() -> None:
    orch = MultiTenantOrchestrator()
    assert orch.instance_state("ghost", BotSku.BTC_SEED) is None


# ---------------------------------------------------------------------------
# Spec dataclass
# ---------------------------------------------------------------------------


def test_container_spec_is_frozen() -> None:
    orch = MultiTenantOrchestrator()
    tenant = _portfolio_tenant()
    spec = orch.plan(tenant)[0]
    import pytest

    with pytest.raises((AttributeError, TypeError)):
        spec.image = "malicious/image:latest"  # type: ignore[misc]


def test_container_spec_defaults() -> None:
    spec = TenantContainerSpec(
        tenant_id="t",
        sku=BotSku.BTC_SEED,
        tier=RentalTier.STARTER,
    )
    assert spec.image == "evolutionarytradingalgo/bot:current"
    assert spec.restart_policy == "on-failure"
