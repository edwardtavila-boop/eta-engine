"""Provision a new rental tenant and emit a client-config bundle.

This script does two things:

  1. Creates a Tenant + Entitlement + Subscription in the in-memory registry
     and writes a JSON record under ``docs/rental/tenants/<tenant_id>.json``.
  2. Emits a ``client_bundle.json`` the Electron/Tauri desktop client consumes
     at first launch (contains WS URL, session token template, tier info,
     supported command kinds). This bundle is SAFE to ship to the customer --
     it contains no strategy code, no reward weights, no secrets.

Usage
-----
    python scripts/rental_provision.py \\
        --tenant-id cust_42 \\
        --email alice@example.com \\
        --tier PRO \\
        --cycle monthly
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.rental import (  # noqa: E402
    BillingCycle,
    EventKind,
    MultiTenantOrchestrator,
    RentalTier,
    Subscription,
    TierCatalog,
)
from eta_engine.rental.billing import SubscriptionStatus  # noqa: E402
from eta_engine.rental.client_contract import (  # noqa: E402
    ClientCommandKind,
    ServerMessageKind,
)
from eta_engine.rental.tenancy import (  # noqa: E402
    build_active_tenant,
)
from eta_engine.rental.tiers import price_for  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Provision a rental tenant.")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--email", required=True)
    p.add_argument(
        "--tier",
        required=True,
        choices=[t.value for t in RentalTier],
    )
    p.add_argument(
        "--cycle",
        default="monthly",
        choices=["monthly", "quarterly", "annual"],
    )
    p.add_argument(
        "--out-dir",
        default=str(ROOT / "docs" / "rental"),
    )
    p.add_argument("--ws-url", default="wss://api.evolutionarytradingalgo.local/ws")
    p.add_argument("--client-version", default="0.1.0")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    catalog = TierCatalog()
    tier_id = RentalTier(args.tier)
    tier = catalog.by_id(tier_id)

    # -- tenant + entitlement ----------------------------------------------
    cycle_days = {"monthly": 30, "quarterly": 91, "annual": 365}[args.cycle]
    expires = datetime.now(UTC) + timedelta(days=cycle_days)
    tenant = build_active_tenant(
        tenant_id=args.tenant_id,
        email=args.email,
        tier_id=tier_id,
        expires_utc=expires,
        catalog=catalog,
    )

    # -- subscription state machine ----------------------------------------
    billing_cycle = BillingCycle[args.cycle.upper()]
    sub = Subscription(
        tenant_id=args.tenant_id,
        tier=tier_id,
        cycle=billing_cycle,
        status=SubscriptionStatus.TRIAL,
        current_period_end=datetime.now(UTC) + timedelta(days=7),
    )
    sub.apply_event(EventKind.START_TRIAL, note="trial issued at provision")
    sub.apply_event(EventKind.ACTIVATE, note="first period paid")

    # -- orchestrator planning (tenant has no keys yet, so this is empty) --
    orch = MultiTenantOrchestrator()
    specs = orch.reconcile(tenant)

    # -- output artifacts ---------------------------------------------------
    out_root = Path(args.out_dir)
    tenants_dir = out_root / "tenants"
    tenants_dir.mkdir(parents=True, exist_ok=True)

    tenant_record = {
        "tenant_id": tenant.tenant_id,
        "email": tenant.email,
        "tier": tenant.tier.value,
        "status": tenant.status.value,
        "created_utc": tenant.created_utc.isoformat(),
        "entitlement": {
            "bot_skus": sorted(s.value for s in tenant.entitlement.bot_skus),
            "expires_utc": tenant.entitlement.expires_utc.isoformat(),
            "max_concurrent_positions": tenant.entitlement.max_concurrent_positions,
            "max_equity_managed_usd": tenant.entitlement.max_equity_managed_usd,
            "includes_custom_tweaks": tenant.entitlement.includes_custom_tweaks,
        },
        "subscription": {
            "cycle": sub.cycle.value,
            "status": sub.status.value,
            "current_period_end": sub.current_period_end.isoformat(),
            "price_paid_usd": price_for(tier, args.cycle),
        },
        "planned_containers": [{"sku": s.sku.value, "tier": s.tier.value, "cpu_limit": s.cpu_limit} for s in specs],
    }
    (tenants_dir / f"{tenant.tenant_id}.json").write_text(
        json.dumps(tenant_record, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    bundle = {
        "tenant_id": tenant.tenant_id,
        "tier": tenant.tier.value,
        "ws_url": args.ws_url,
        "client_version_min": args.client_version,
        "supported_client_commands": sorted(k.value for k in ClientCommandKind),
        "supported_server_messages": sorted(k.value for k in ServerMessageKind),
        "docs_url": "https://evolutionarytradingalgo.local/docs/desktop-client",
        "disclaimer": (
            "Educational automation tool. Not financial advice. You control "
            "your own funds and risk. Past performance is not indicative of "
            "future results. Trading involves substantial risk of loss."
        ),
    }
    bundle_path = tenants_dir / f"{tenant.tenant_id}_client_bundle.json"
    bundle_path.write_text(
        json.dumps(bundle, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    # -- catalog snapshot (idempotent -- overwritten each run) -------------
    (out_root / "tier_catalog.json").write_text(
        json.dumps({"tiers": catalog.public_price_list()}, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"provisioned tenant {tenant.tenant_id} on tier {tenant.tier.value}")
    print(f"  expires_utc:     {tenant.entitlement.expires_utc.isoformat()}")
    print(f"  price_paid_usd:  ${price_for(tier, args.cycle):.2f}")
    print(f"  planned pods:    {len(specs)} (awaiting customer API keys)")
    print(f"  tenant record:   {tenants_dir / f'{tenant.tenant_id}.json'}")
    print(f"  client bundle:   {bundle_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
