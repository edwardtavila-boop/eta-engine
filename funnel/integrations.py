"""
EVOLUTIONARY TRADING ALGO  //  funnel.integrations
======================================
Integration topology snapshot -- what plugs into what, and where the
money flows through the funnel.

Why this exists
---------------
Every trading venue, bot, onramp provider, staking adapter, and
observability hook eventually feeds into the Profit Funnel
(``funnel.waterfall``). The Command Center tab that shows "what's
wired to what" needs a single, self-describing JSON source that
stays current with the code. This module produces it.

It is read-only and has no I/O of its own. Callers pass in (or omit,
for stub snapshots) live status dicts; the module serializes the
canonical topology + the live status into a
``IntegrationsReport`` pydantic model and emits JSON / text renders.

Produced by ``scripts.build_integrations_report``, consumed by the
firm-tracker Command Center artifact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class VenueIntegration(BaseModel):
    """A single trading / onramp / custody venue."""

    name: str
    kind: str  # "futures" | "perps_cex" | "spot_cex" | "onramp" | "cold_wallet"
    module: str  # dotted python path
    asset_classes: list[str] = Field(default_factory=list)
    status: str = "READY"  # READY | STUB | NEEDS_FUNDING | LIVE | DORMANT
    notes: str = ""


class BotIntegration(BaseModel):
    """A bot + its wiring into venues + the funnel layer it feeds."""

    name: str
    module: str
    venue: str  # VenueIntegration.name
    funnel_layer: str  # LAYER_1_MNQ | LAYER_2_BTC | LAYER_3_PERPS | LAYER_4_STAKING
    risk_tier: str  # A | B | CASINO | SEED
    status: str = "PAPER"  # PAPER | LIVE | BLOCKED
    notes: str = ""


class FunnelLayer(BaseModel):
    """One waterfall layer.

    Mirrors ``funnel.waterfall.LayerRiskTier`` so the dashboard doesn't
    need to import runtime classes.
    """

    layer_id: str  # LAYER_1_MNQ etc.
    label: str
    sweep_out_pct: float = Field(ge=0.0, le=1.0)
    min_outgoing_usd: float = Field(ge=0.0)
    min_incoming_usd: float = Field(default=0.0, ge=0.0)
    max_position_pct_per_trade: float = Field(ge=0.0, le=1.0)
    daily_loss_cap_pct: float = Field(ge=0.0, le=1.0)
    drawdown_kill_pct: float = Field(ge=0.0, le=1.0)
    leverage_cap: float = Field(gt=0.0)
    notes: str = ""


class OnrampRoute(BaseModel):
    """An (fiat, provider, crypto_target) triple from the onramp policy."""

    fiat_source: str
    provider: str
    crypto_target: str
    per_txn_limit_usd: float = Field(gt=0.0)
    monthly_limit_usd: float = Field(gt=0.0)


class StakingIntegration(BaseModel):
    """A yield adapter under staking/*."""

    protocol: str
    module: str
    chain: str
    asset_in: str
    asset_out: str
    target_apy_pct: float = Field(ge=0.0)
    notes: str = ""


class ObservabilityIntegration(BaseModel):
    """One observability surface (alerters, supervisor, telemetry)."""

    name: str
    module: str
    kind: str  # alerter | supervisor | telemetry | journal
    status: str = "ACTIVE"  # ACTIVE | DRY_RUN | DISABLED
    notes: str = ""


class IntegrationsReport(BaseModel):
    """Full integration topology + live-status overlay."""

    timestamp_utc: str
    schema_version: str = "1.0"
    venues: list[VenueIntegration] = Field(default_factory=list)
    bots: list[BotIntegration] = Field(default_factory=list)
    funnel_layers: list[FunnelLayer] = Field(default_factory=list)
    onramp_routes: list[OnrampRoute] = Field(default_factory=list)
    staking: list[StakingIntegration] = Field(default_factory=list)
    observability: list[ObservabilityIntegration] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Canonical topology (kept in sync with the code layout)
# ---------------------------------------------------------------------------


def canonical_venues() -> list[VenueIntegration]:
    return [
        VenueIntegration(
            name="ibkr",
            kind="futures",
            module="eta_engine.venues.ibkr",
            asset_classes=["MNQ", "NQ", "ES", "MES", "RTY"],
            status="READY",
            notes=(
                "Active futures primary per operator mandate 2026-04-24. "
                "IBKR Client Portal adapter, paper by default; flip to live "
                "via IBKR session config."
            ),
        ),
        VenueIntegration(
            name="tastytrade",
            kind="futures",
            module="eta_engine.venues.tastytrade",
            asset_classes=["MNQ", "NQ", "ES", "MES", "RTY"],
            status="READY",
            notes=("Active futures fallback per operator mandate 2026-04-24. Tastytrade adapter, paper by default."),
        ),
        VenueIntegration(
            name="tradovate",
            kind="futures",
            module="eta_engine.venues.tradovate",
            asset_classes=["MNQ", "NQ"],
            status="DORMANT",
            notes=(
                "DORMANT per operator mandate 2026-04-24 -- funding-blocked, "
                "weeks-out ETA. Adapter stays importable; flip "
                "venues.router.DORMANT_BROKERS to frozenset() when funding "
                "clears."
            ),
        ),
        VenueIntegration(
            name="bybit",
            kind="perps_cex",
            module="eta_engine.venues.bybit",
            asset_classes=["ETH-PERP", "SOL-PERP", "XRP-PERP"],
            status="READY",
        ),
        VenueIntegration(
            name="okx",
            kind="spot_cex",
            module="eta_engine.venues.okx",
            asset_classes=["spot"],
            status="READY",
        ),
        VenueIntegration(
            name="coinbase",
            kind="onramp",
            module="eta_engine.funnel.fiat_to_crypto",
            asset_classes=["BTC", "ETH", "USDC"],
            status="READY",
            notes="ACH onramp; stub executor used in tests.",
        ),
        VenueIntegration(
            name="kraken",
            kind="onramp",
            module="eta_engine.funnel.fiat_to_crypto",
            asset_classes=["USDC", "USDT"],
            status="READY",
            notes="Bank-wire onramp for stables.",
        ),
        VenueIntegration(
            name="ledger_cold",
            kind="cold_wallet",
            module="eta_engine.funnel.cold_wallet_sweep",
            asset_classes=["BTC", "ETH", "SOL", "stablecoins"],
            status="READY",
            notes="Air-gapped custody sink for waterfall sweeps.",
        ),
    ]


def canonical_bots() -> list[BotIntegration]:
    return [
        BotIntegration(
            name="mnq",
            module="eta_engine.bots.mnq.mnq_bot",
            venue="ibkr",
            funnel_layer="LAYER_1_MNQ",
            risk_tier="A",
            status="PAPER",
            notes=(
                "Router-wired (IBKR primary, Tastytrade fallback; Tradovate "
                "DORMANT 2026-04-24); 168 trades +0.473R paper; live-tiny "
                "blocked on broker funding."
            ),
        ),
        BotIntegration(
            name="nq",
            module="eta_engine.bots.nq.nq_bot",
            venue="ibkr",
            funnel_layer="LAYER_1_MNQ",
            risk_tier="A",
            status="PAPER",
            notes=(
                "Inherits mnq; $20 point value; 140 trades +0.607R paper. "
                "Routes via IBKR primary + Tastytrade fallback."
            ),
        ),
        BotIntegration(
            name="crypto_seed",
            module="eta_engine.bots.crypto_seed.crypto_seed_bot",
            venue="bybit",
            funnel_layer="LAYER_2_BTC",
            risk_tier="SEED",
            status="PAPER",
            notes="Grid + directional overlay; 161 trades +0.149R, gate FAIL pending real bars.",
        ),
        BotIntegration(
            name="eth_perp",
            module="eta_engine.bots.eth_perp.eth_perp_bot",
            venue="bybit",
            funnel_layer="LAYER_3_PERPS",
            risk_tier="CASINO",
            status="PAPER",
            notes="Router-wired; paper +0.161R, gate FAIL pending real bars.",
        ),
        BotIntegration(
            name="sol_perp",
            module="eta_engine.bots.sol_perp.sol_perp_bot",
            venue="bybit",
            funnel_layer="LAYER_3_PERPS",
            risk_tier="CASINO",
            status="PAPER",
            notes="Inherits eth_perp; paper +0.146R.",
        ),
        BotIntegration(
            name="xrp_perp",
            module="eta_engine.bots.xrp_perp.xrp_perp_bot",
            venue="bybit",
            funnel_layer="LAYER_3_PERPS",
            risk_tier="CASINO",
            status="PAPER",
            notes="Inherits eth_perp; paper +0.176R, 15.55% DD.",
        ),
    ]


def canonical_funnel_layers() -> list[FunnelLayer]:
    """Mirror of ``funnel.waterfall`` default tier defaults.

    Values picked to match the design notes in that module's docstring:
    tight per-trade / daily-loss caps on L1, loosening casino tier on L3.
    """
    return [
        FunnelLayer(
            layer_id="LAYER_1_MNQ",
            label="MNQ futures compounder",
            sweep_out_pct=0.50,
            min_outgoing_usd=100.0,
            min_incoming_usd=0.0,
            max_position_pct_per_trade=0.01,
            daily_loss_cap_pct=0.03,
            drawdown_kill_pct=0.08,
            leverage_cap=10.0,
            notes="Tier A; 60% of profit stack.",
        ),
        FunnelLayer(
            layer_id="LAYER_2_BTC",
            label="BTC spot / grid seed",
            sweep_out_pct=0.40,
            min_outgoing_usd=200.0,
            min_incoming_usd=50.0,
            max_position_pct_per_trade=0.02,
            daily_loss_cap_pct=0.04,
            drawdown_kill_pct=0.10,
            leverage_cap=3.0,
            notes="Tier B/seed; 10% of stack.",
        ),
        FunnelLayer(
            layer_id="LAYER_3_PERPS",
            label="ETH/SOL/XRP perps (casino tier)",
            sweep_out_pct=0.30,
            min_outgoing_usd=500.0,
            min_incoming_usd=100.0,
            max_position_pct_per_trade=0.03,
            daily_loss_cap_pct=0.05,
            drawdown_kill_pct=0.15,
            leverage_cap=5.0,
            notes="Casino tier; 30% of stack; high DD tolerance.",
        ),
        FunnelLayer(
            layer_id="LAYER_4_STAKING",
            label="Staking compound (terminal yield)",
            sweep_out_pct=0.0,
            min_outgoing_usd=1_000_000.0,
            min_incoming_usd=250.0,
            max_position_pct_per_trade=1.0,
            daily_loss_cap_pct=1.0,
            drawdown_kill_pct=1.0,
            leverage_cap=1.0,
            notes="Terminal layer; no outflow except manual withdrawal.",
        ),
    ]


def canonical_onramp_routes(
    *,
    per_txn_limit_usd: float = 10_000.0,
    monthly_limit_usd: float = 50_000.0,
) -> list[OnrampRoute]:
    """Mirror of the default ``OnrampPolicy.allowed_triples`` in tests."""
    return [
        OnrampRoute(
            fiat_source="ACH",
            provider="COINBASE",
            crypto_target="BTC",
            per_txn_limit_usd=per_txn_limit_usd,
            monthly_limit_usd=monthly_limit_usd,
        ),
        OnrampRoute(
            fiat_source="ACH",
            provider="COINBASE",
            crypto_target="ETH",
            per_txn_limit_usd=per_txn_limit_usd,
            monthly_limit_usd=monthly_limit_usd,
        ),
        OnrampRoute(
            fiat_source="BANK_WIRE",
            provider="KRAKEN",
            crypto_target="USDC",
            per_txn_limit_usd=per_txn_limit_usd,
            monthly_limit_usd=monthly_limit_usd,
        ),
    ]


def canonical_staking() -> list[StakingIntegration]:
    return [
        StakingIntegration(
            protocol="Lido",
            module="eta_engine.staking.lido",
            chain="ethereum",
            asset_in="ETH",
            asset_out="wstETH",
            target_apy_pct=3.2,
            notes="Optional EigenLayer restake adds ~+1.5% APY.",
        ),
        StakingIntegration(
            protocol="Jito",
            module="eta_engine.staking.jito",
            chain="solana",
            asset_in="SOL",
            asset_out="JitoSOL",
            target_apy_pct=7.5,
        ),
        StakingIntegration(
            protocol="Flare",
            module="eta_engine.staking.flare",
            chain="flare",
            asset_in="FLR",
            asset_out="sFLR",
            target_apy_pct=4.1,
            notes="FTSO delegation optional for +reward share.",
        ),
        StakingIntegration(
            protocol="Ethena",
            module="eta_engine.staking.ethena",
            chain="ethereum",
            asset_in="USDT",
            asset_out="sUSDe",
            target_apy_pct=12.0,
            notes="7-day cooldown on unstake.",
        ),
    ]


def canonical_observability() -> list[ObservabilityIntegration]:
    return [
        ObservabilityIntegration(
            name="JarvisSupervisor",
            module="eta_engine.obs.jarvis_supervisor",
            kind="supervisor",
            status="ACTIVE",
            notes=("Health watchdog around JarvisContextEngine. Runs at 60s cadence via scripts.jarvis_live."),
        ),
        ObservabilityIntegration(
            name="TelegramAlerter",
            module="eta_engine.obs.alerts.TelegramAlerter",
            kind="alerter",
            status="DRY_RUN",
            notes="Becomes ACTIVE when TELEGRAM_BOT_TOKEN+CHAT_ID are set.",
        ),
        ObservabilityIntegration(
            name="DiscordAlerter",
            module="eta_engine.obs.alerts.DiscordAlerter",
            kind="alerter",
            status="DRY_RUN",
            notes="Becomes ACTIVE when DISCORD_WEBHOOK_URL is set.",
        ),
        ObservabilityIntegration(
            name="SlackAlerter",
            module="eta_engine.obs.alerts.SlackAlerter",
            kind="alerter",
            status="DRY_RUN",
            notes="Becomes ACTIVE when SLACK_WEBHOOK_URL is set.",
        ),
        ObservabilityIntegration(
            name="GateOverrideTelemetry",
            module="eta_engine.obs.gate_override_telemetry",
            kind="telemetry",
            status="ACTIVE",
            notes="Prometheus counters: blocks / overrides / override_rate.",
        ),
        ObservabilityIntegration(
            name="DecisionJournal",
            module="eta_engine.obs.decision_journal",
            kind="journal",
            status="ACTIVE",
            notes="Append-only JSONL decision log under docs/decision_journal.jsonl.",
        ),
        ObservabilityIntegration(
            name="AutopilotWatchdog",
            module="eta_engine.obs.autopilot_watchdog",
            kind="supervisor",
            status="ACTIVE",
            notes="REQUIRE_ACK -> TIGHTEN_STOP -> FORCE_FLATTEN ladder.",
        ),
    ]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_integrations_report(
    *,
    now: datetime | None = None,
    live_status: dict[str, Any] | None = None,
    onramp_per_txn_limit_usd: float = 10_000.0,
    onramp_monthly_limit_usd: float = 50_000.0,
) -> IntegrationsReport:
    """Produce an IntegrationsReport from the canonical topology.

    Parameters
    ----------
    now:
        Override timestamp (UTC). Defaults to ``datetime.now(UTC)``.
    live_status:
        Optional live-status overlay. Known keys:

        * ``bots``: mapping bot-name -> {"status": "...", "notes": "..."}
          (merged on top of the canonical PAPER defaults)
        * ``venues``: mapping venue-name -> {"status": "...", "notes": "..."}
        * ``observability``: mapping alerter-name -> {"status": "..."}
        * ``summary``: dict merged into report.summary

    All overlays are optional; no live status is required.
    """
    now = now or datetime.now(UTC)
    live_status = live_status or {}

    venues = canonical_venues()
    bots = canonical_bots()
    layers = canonical_funnel_layers()
    routes = canonical_onramp_routes(
        per_txn_limit_usd=onramp_per_txn_limit_usd,
        monthly_limit_usd=onramp_monthly_limit_usd,
    )
    staking = canonical_staking()
    obs = canonical_observability()

    # -- live overlays ---------------------------------------------------
    ls_bots = live_status.get("bots") or {}
    for b in bots:
        patch = ls_bots.get(b.name) or {}
        if patch.get("status"):
            b.status = str(patch["status"])
        if patch.get("notes"):
            b.notes = str(patch["notes"])
    ls_venues = live_status.get("venues") or {}
    for v in venues:
        patch = ls_venues.get(v.name) or {}
        if patch.get("status"):
            v.status = str(patch["status"])
        if patch.get("notes"):
            v.notes = str(patch["notes"])
    ls_obs = live_status.get("observability") or {}
    for o in obs:
        patch = ls_obs.get(o.name) or {}
        if patch.get("status"):
            o.status = str(patch["status"])
        if patch.get("notes"):
            o.notes = str(patch["notes"])

    # -- summary rollup --------------------------------------------------
    summary: dict[str, Any] = {
        "venues_total": len(venues),
        "bots_total": len(bots),
        "bots_paper": sum(1 for b in bots if b.status == "PAPER"),
        "bots_live": sum(1 for b in bots if b.status == "LIVE"),
        "bots_blocked": sum(1 for b in bots if b.status == "BLOCKED"),
        "funnel_layers": len(layers),
        "onramp_routes": len(routes),
        "staking_protocols": len(staking),
        "observability_surfaces": len(obs),
        "alerters_active": sum(1 for o in obs if o.kind == "alerter" and o.status == "ACTIVE"),
    }
    if "summary" in live_status and isinstance(live_status["summary"], dict):
        summary.update(live_status["summary"])

    return IntegrationsReport(
        timestamp_utc=now.isoformat(),
        venues=venues,
        bots=bots,
        funnel_layers=layers,
        onramp_routes=routes,
        staking=staking,
        observability=obs,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Text renderer
# ---------------------------------------------------------------------------


def render_text(report: IntegrationsReport) -> str:
    lines: list[str] = []
    bar = "=" * 72
    lines.append(bar)
    lines.append(f"EVOLUTIONARY TRADING ALGO  //  INTEGRATIONS MAP  (schema v{report.schema_version})")
    lines.append(f"ts: {report.timestamp_utc}")
    lines.append(bar)
    lines.append("")
    # Summary
    s = report.summary
    lines.append("SUMMARY")
    lines.append(
        f"  venues={s.get('venues_total', 0)}  "
        f"bots={s.get('bots_total', 0)} "
        f"(paper={s.get('bots_paper', 0)}/"
        f"live={s.get('bots_live', 0)}/"
        f"blocked={s.get('bots_blocked', 0)})  "
        f"layers={s.get('funnel_layers', 0)}  "
        f"staking={s.get('staking_protocols', 0)}"
    )
    lines.append("")
    lines.append("VENUES")
    for v in report.venues:
        lines.append(f"  {v.name:<12} {v.kind:<12} [{v.status:<13}] assets={','.join(v.asset_classes)}")
        if v.notes:
            lines.append(f"    - {v.notes}")
    lines.append("")
    lines.append("BOTS  (venue -> funnel layer, risk tier)")
    for b in report.bots:
        lines.append(f"  {b.name:<12} -> {b.venue:<10} {b.funnel_layer:<16} tier={b.risk_tier:<6} [{b.status}]")
        if b.notes:
            lines.append(f"    - {b.notes}")
    lines.append("")
    lines.append("FUNNEL LAYERS  (sweep_out_pct, min_outgoing, kill_dd)")
    for layer in report.funnel_layers:
        lines.append(
            f"  {layer.layer_id:<16} sweep={layer.sweep_out_pct:>4.0%}  "
            f"min_out=${layer.min_outgoing_usd:>10,.0f}  "
            f"kill_dd={layer.drawdown_kill_pct:>5.0%}  "
            f"lev={layer.leverage_cap:>4.1f}x   {layer.label}"
        )
    lines.append("")
    lines.append("ONRAMP ROUTES  (fiat -> provider -> crypto)")
    for r in report.onramp_routes:
        lines.append(
            f"  {r.fiat_source:<10} -> {r.provider:<10} -> {r.crypto_target:<6}  "
            f"per-txn=${r.per_txn_limit_usd:>8,.0f}  "
            f"monthly=${r.monthly_limit_usd:>10,.0f}"
        )
    lines.append("")
    lines.append("STAKING  (protocol -> asset, target APY)")
    for st in report.staking:
        lines.append(
            f"  {st.protocol:<8} {st.chain:<10} {st.asset_in:<6} -> {st.asset_out:<10} apy={st.target_apy_pct:>5.2f}%"
        )
    lines.append("")
    lines.append("OBSERVABILITY")
    for o in report.observability:
        lines.append(f"  {o.name:<24} {o.kind:<12} [{o.status}]")
    lines.append("")
    return "\n".join(lines)
