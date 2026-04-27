"""
JARVIS v3 // supercharge tests
==============================
Covers philosophy, vps, skills_registry, mcp_registry, kaizen, unleashed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionType,
    SubsystemId,
)
from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    EquitySnapshot,
    JarvisContext,
    JarvisSuggestion,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    SessionPhase,
    StressComponent,
    StressScore,
)
from eta_engine.brain.jarvis_v3 import (
    kaizen,
    mcp_registry,
    philosophy,
    skills_registry,
    unleashed,
    vps,
)

# ---------------------------------------------------------------------------
# philosophy
# ---------------------------------------------------------------------------


class TestPhilosophy:
    def test_all_tenets_have_specs(self):
        for t in philosophy.Tenet:
            assert t in philosophy.DOCTRINE

    def test_apply_doctrine_downgrades_risky_action(self):
        v = philosophy.apply_doctrine(
            proposed_verdict="APPROVED",
            subsystem="bot.mnq",
            action="ORDER_PLACE",
        )
        # Should at least apply tenets; net_bias negative because CAPITAL_FIRST
        # + NEVER_ON_AUTOPILOT both bias negative for bot.mnq.
        assert v.net_bias < 0.0
        assert "CAPITAL_FIRST" in v.tenets_applied

    def test_kaizen_precondition(self):
        ok, _ = philosophy.kaizen_pre_condition(7)
        assert ok
        bad, _ = philosophy.kaizen_pre_condition(2)
        assert not bad

    def test_summarize_doctrine_nonempty(self):
        s = philosophy.summarize_doctrine()
        assert "EVOLUTIONARY TRADING ALGO" in s
        assert "CAPITAL_FIRST" in s


# ---------------------------------------------------------------------------
# vps
# ---------------------------------------------------------------------------


class TestVPS:
    def _snap(self, cpu=30, mem=40, disk=50):
        return vps.VPSSnapshot(
            ts=datetime.now(UTC),
            cpu_pct=cpu,
            mem_pct=mem,
            disk_pct=disk,
            load_1m=0.5,
            load_5m=0.7,
            load_15m=0.9,
        )

    def test_all_healthy_green(self):
        rep = vps.assess_vps(
            self._snap(),
            services={"mnq-bot.service": vps.ServiceState.RUNNING},
            specs=vps.DEFAULT_CATALOG,
        )
        assert rep.overall == "GREEN"

    def test_failed_service_red(self):
        rep = vps.assess_vps(
            self._snap(),
            services={"mnq-bot.service": vps.ServiceState.FAILED},
            specs=vps.DEFAULT_CATALOG,
        )
        assert rep.overall == "RED"
        assert any(a.action == vps.VPSActionType.RESTART for a in rep.proposed_actions)

    def test_high_disk_proposes_prune(self):
        rep = vps.assess_vps(
            self._snap(disk=96),
            services={"mnq-bot.service": vps.ServiceState.RUNNING},
            specs=vps.DEFAULT_CATALOG,
        )
        assert any(a.action == vps.VPSActionType.DISK_PRUNE for a in rep.proposed_actions)

    def test_action_to_shell(self):
        req = vps.VPSActionRequest(
            action=vps.VPSActionType.RESTART,
            service="mnq-bot.service",
            rationale="test",
        )
        cmd = vps.vps_action_to_shell(req)
        assert cmd[:2] == ["systemctl", "restart"]


# ---------------------------------------------------------------------------
# skills_registry
# ---------------------------------------------------------------------------


class TestSkillsRegistry:
    def test_default_registry_has_core_skills(self):
        reg = skills_registry.default_registry()
        assert reg.get("bot-status") is not None
        assert reg.get("firm:the-firm") is not None

    def test_operator_can_invoke(self):
        reg = skills_registry.default_registry()
        r = reg.can_invoke("bot-status", "operator.edward")
        assert r.allowed

    def test_unknown_subsystem_blocked(self):
        reg = skills_registry.default_registry()
        r = reg.can_invoke("firm:the-firm", "bot.mnq")  # bot not on allowlist
        assert not r.allowed

    def test_save_load(self, tmp_path):
        reg = skills_registry.default_registry()
        path = tmp_path / "skills.json"
        reg.save(path)
        reg2 = skills_registry.SkillRegistry.load(path)
        assert reg2.get("bot-status") is not None


# ---------------------------------------------------------------------------
# mcp_registry
# ---------------------------------------------------------------------------


class TestMCPRegistry:
    def test_tradingview_read_tools_registered(self):
        reg = mcp_registry.default_registry()
        assert reg.get("tradingview", "chart_get_state") is not None

    def test_bot_mnq_can_read_tradingview(self):
        reg = mcp_registry.default_registry()
        ok, _ = reg.can_use("tradingview", "chart_get_state", "bot.mnq")
        assert ok

    def test_bot_mnq_cannot_create_alert(self):
        reg = mcp_registry.default_registry()
        ok, _ = reg.can_use("tradingview", "alert_create", "bot.mnq")
        assert not ok

    def test_admin_tier_restricted_to_operator(self):
        reg = mcp_registry.default_registry()
        ok, _ = reg.can_use("Desktop_Commander", "write_file", "operator.edward")
        assert ok
        ok2, _ = reg.can_use("Desktop_Commander", "write_file", "bot.mnq")
        assert not ok2


# ---------------------------------------------------------------------------
# kaizen
# ---------------------------------------------------------------------------


class TestKaizen:
    def test_close_cycle_emits_plus_one(self):
        retro, ticket = kaizen.close_cycle(
            cycle_kind=kaizen.CycleKind.DAILY,
            window_start=datetime.now(UTC) - timedelta(days=1),
            window_end=datetime.now(UTC),
            went_well=["fills were clean"],
            went_poorly=["missed 09:30 open by 20s due to cold start"],
        )
        assert ticket.title.startswith("Fix:")
        assert ticket.status == kaizen.KaizenStatus.OPEN

    def test_ledger_summary_green_when_daily(self):
        ledger = kaizen.KaizenLedger()
        now = datetime.now(UTC)
        for i in range(7):
            retro, ticket = kaizen.close_cycle(
                cycle_kind=kaizen.CycleKind.DAILY,
                window_start=now - timedelta(days=i + 1),
                window_end=now - timedelta(days=i),
                went_well=["x"],
                went_poorly=[],
                now=now - timedelta(days=i),
            )
            ledger.add_retro(retro)
            ledger.add_ticket(ticket)
        s = ledger.summary(window_days=7, now=now)
        assert s.severity in {"GREEN", "YELLOW"}
        assert s.retrospectives == 7

    def test_ledger_red_when_missed(self):
        ledger = kaizen.KaizenLedger()
        s = ledger.summary(window_days=7)
        assert s.severity == "RED"

    def test_ship_ticket(self):
        ledger = kaizen.KaizenLedger()
        _, ticket = kaizen.close_cycle(
            cycle_kind=kaizen.CycleKind.DAILY,
            window_start=datetime.now(UTC) - timedelta(days=1),
            window_end=datetime.now(UTC),
            went_well=[],
            went_poorly=["x"],
        )
        ledger.add_ticket(ticket)
        ledger.ship_ticket(ticket.id)
        assert ticket.status == kaizen.KaizenStatus.SHIPPED


# ---------------------------------------------------------------------------
# unleashed (meta-controller integration)
# ---------------------------------------------------------------------------


class TestUnleashed:
    def _ctx(self):
        now = datetime.now(UTC)
        stress = StressScore(
            composite=0.3,
            components=[
                StressComponent(name="equity_dd", value=0.3, weight=1.0, note=""),
            ],
            binding_constraint="equity_dd",
        )
        return JarvisContext(
            ts=now,
            macro=MacroSnapshot(vix_level=18.0, macro_bias="neutral"),
            equity=EquitySnapshot(
                account_equity=50000,
                daily_pnl=0.0,
                daily_drawdown_pct=0.01,
                open_positions=1,
                open_risk_r=1.0,
            ),
            regime=RegimeSnapshot(regime="NEUTRAL", confidence=0.7),
            journal=JournalSnapshot(),
            suggestion=JarvisSuggestion(
                action=ActionSuggestion.TRADE,
                reason="all gates green",
                confidence=0.8,
            ),
            stress_score=stress,
            session_phase=SessionPhase.MORNING,
        )

    def test_decide_produces_envelope(self):
        core = unleashed.ApexPredatorCore()
        ctx = self._ctx()
        req = ActionRequest(
            subsystem=SubsystemId.BOT_MNQ,
            action=ActionType.ORDER_PLACE,
            rationale="momentum long",
        )
        dec = core.decide(req, ctx)
        assert dec.request_id == req.request_id
        assert dec.final_verdict in {"APPROVED", "CONDITIONAL", "DENIED", "DEFERRED"}
        assert dec.doctrine is not None
        assert dec.horizons is not None
        assert dec.projection is not None

    def test_portfolio_breach_downgrades(self):
        core = unleashed.ApexPredatorCore()
        ctx = self._ctx()
        req = ActionRequest(
            subsystem=SubsystemId.BOT_BTC_PERP,
            action=ActionType.ORDER_PLACE,
            rationale="long btc",
        )
        from eta_engine.brain.jarvis_v3.portfolio import Exposure

        exps = [
            Exposure(subsystem="bot.btc_perp", symbol="BTC", r_at_risk=2.0),
            Exposure(subsystem="bot.eth_perp", symbol="ETH", r_at_risk=2.0),
            Exposure(subsystem="bot.sol_perp", symbol="SOL", r_at_risk=2.0),
        ]
        corr = {
            ("BTC", "ETH"): 0.85,
            ("BTC", "SOL"): 0.80,
            ("ETH", "SOL"): 0.82,
        }
        dec = core.decide(req, ctx, exposures=exps, corr_matrix=corr)
        assert dec.portfolio is not None
        assert dec.portfolio.cluster_breach

    def test_dashboard_snapshot(self):
        core = unleashed.ApexPredatorCore()
        ctx = self._ctx()
        snap = core.dashboard_snapshot(ctx)
        assert snap["regime"] == "NEUTRAL"
        assert "stress" in snap
        assert "kaizen" in snap
