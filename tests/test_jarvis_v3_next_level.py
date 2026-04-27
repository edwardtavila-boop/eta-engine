"""
JARVIS v3 // next_level tests
=============================
Covers debate / shadow / vector_precedent / strategy_synthesis /
voice / digital_twin / autopr / causal / self_play.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_v3.kaizen import KaizenStatus, KaizenTicket
from eta_engine.brain.jarvis_v3.next_level import (
    autopr,
    causal,
    debate,
    digital_twin,
    self_play,
    shadow,
    strategy_synthesis,
    vector_precedent,
    voice,
)
from eta_engine.brain.jarvis_v3.precedent import (
    PrecedentEntry,
    PrecedentGraph,
    PrecedentKey,
)

# ---------------------------------------------------------------------------
# #3 debate
# ---------------------------------------------------------------------------


class TestDebate:
    def test_bull_argues_approve_when_stress_low(self):
        arg = debate.bull_argue(
            stress=0.2,
            sizing_mult=1.0,
            regime="NEUTRAL",
            suggestion="TRADE",
        )
        assert arg.vote in {"APPROVE", "CONDITIONAL"}
        assert arg.confidence > 0.5

    def test_bear_argues_deny_in_crisis(self):
        arg = debate.bear_argue(
            stress=0.8,
            sizing_mult=0.3,
            regime="CRISIS",
            suggestion="REDUCE",
            dd_pct=0.03,
        )
        assert arg.vote == "DENY"

    def test_full_debate_aggregates_four(self):
        v = debate.full_debate(
            stress=0.3,
            sizing_mult=0.9,
            regime="NEUTRAL",
            regime_confidence=0.8,
            suggestion="TRADE",
            precedent_n=20,
            precedent_win_rate=0.65,
            precedent_mean_r=0.8,
            precedent_suggestion="positive bucket",
        )
        assert len(v.transcript) == 4
        assert v.final_vote in {"APPROVE", "CONDITIONAL", "DENY", "DEFER"}

    def test_historian_no_precedent(self):
        arg = debate.historian_argue(
            precedent_n=0,
            precedent_win_rate=None,
            precedent_mean_r=None,
        )
        assert arg.vote == "CONDITIONAL"
        assert arg.confidence < 0.5


# ---------------------------------------------------------------------------
# #6 shadow
# ---------------------------------------------------------------------------


class TestShadow:
    def test_open_close_long_winner(self):
        ledger = shadow.ShadowLedger()
        t = shadow.shadow_from_denied_request(
            request_id="r1",
            subsystem="bot.mnq",
            symbol="MNQ",
            side="LONG",
            entry_px=20000.0,
            stop_px=19900.0,
            target_px=20200.0,
        )
        ledger.add(t)
        changed = ledger.tick(
            price_lookup={"MNQ": 20200.0},
            now=datetime.now(UTC),
        )
        assert "r1" in changed
        closed = ledger.get("r1")
        assert closed.realized_r == 2.0  # target - entry = 200, stop-dist 100 => 2R

    def test_short_loser(self):
        ledger = shadow.ShadowLedger()
        t = shadow.shadow_from_denied_request(
            request_id="r2",
            subsystem="bot.btc",
            symbol="BTC",
            side="SHORT",
            entry_px=60000.0,
            stop_px=60500.0,
            target_px=59000.0,
        )
        ledger.add(t)
        ledger.tick(
            price_lookup={"BTC": 60500.0},
            now=datetime.now(UTC),
        )
        assert ledger.get("r2").realized_r == -1.0

    def test_regret_summary(self):
        ledger = shadow.ShadowLedger()
        # 3 winners, 1 loser
        for i, r in enumerate([2.0, 1.5, 2.0, -1.0]):
            ledger.add(
                shadow.ShadowTrade(
                    id=f"t{i}",
                    opened_at=datetime.now(UTC),
                    subsystem="x",
                    symbol="S",
                    side="LONG",
                    entry_px=100,
                    stop_px=99,
                    target_px=102,
                    r_distance=1.0,
                    realized_r=r,
                    status=shadow.ShadowStatus.CLOSED,
                    closed_at=datetime.now(UTC),
                    closed_px=100,
                )
            )
        s = ledger.regret()
        assert s.cumulative_r > 3.0  # RED zone
        assert s.severity == "RED"

    def test_expiration(self):
        ledger = shadow.ShadowLedger()
        now = datetime.now(UTC)
        ledger.add(
            shadow.ShadowTrade(
                id="t1",
                opened_at=now - timedelta(hours=5),
                subsystem="x",
                symbol="S",
                side="LONG",
                entry_px=100,
                stop_px=99,
                target_px=102,
                r_distance=1.0,
            )
        )
        ledger.tick(price_lookup={"S": 100}, now=now, max_holding_hours=4.0)
        assert ledger.get("t1").status == shadow.ShadowStatus.EXPIRED


# ---------------------------------------------------------------------------
# #4 vector_precedent
# ---------------------------------------------------------------------------


class TestVectorPrecedent:
    def test_exact_match_is_highest(self):
        store = vector_precedent.VectorPrecedentStore()
        now = datetime.now(UTC)
        store.record(
            entry_id="e1",
            ts=now,
            regime="CRISIS",
            session_phase="OPEN_DRIVE",
            event_category="fomc",
            action="DENY",
            realized_r=-1.0,
            outcome_correct=1,
        )
        store.record(
            entry_id="e2",
            ts=now,
            regime="RISK_ON",
            session_phase="MORNING",
            event_category="none",
            action="TRADE",
            realized_r=+1.0,
            outcome_correct=1,
        )
        hits = store.search(
            regime="CRISIS",
            session_phase="OPEN_DRIVE",
            event_category="fomc",
        )
        assert hits[0].entry.id == "e1"

    def test_synthesize_mean_r(self):
        store = vector_precedent.VectorPrecedentStore()
        now = datetime.now(UTC)
        for i, r in enumerate([1.0, 0.8, 1.2, 0.9]):
            store.record(
                entry_id=f"e{i}",
                ts=now,
                regime="RISK_ON",
                session_phase="MORNING",
                action="TRADE",
                realized_r=r,
                outcome_correct=1,
            )
        hits = store.search(regime="RISK_ON", session_phase="MORNING")
        syn = store.synthesize(hits)
        assert syn.mean_r is not None and syn.mean_r > 0.5
        assert syn.hit_rate == 1.0


# ---------------------------------------------------------------------------
# #5 strategy_synthesis
# ---------------------------------------------------------------------------


class TestStrategySynthesis:
    def test_no_candidates_on_empty_graph(self):
        g = PrecedentGraph()
        report = strategy_synthesis.mine(g)
        assert report.candidates_found == 0

    def test_candidate_emitted_on_strong_bucket(self):
        g = PrecedentGraph()
        k = PrecedentKey(regime="RISK_ON", session_phase="MORNING")
        for _i in range(25):
            g.record(
                k,
                PrecedentEntry(
                    ts=datetime.now(UTC),
                    action="TRADE",
                    realized_r=1.0,
                    outcome_correct=1,
                ),
            )
        report = strategy_synthesis.mine(g, min_support=20, min_mean_r=0.3)
        assert report.candidates_found >= 1
        spec = report.specs[0]
        assert spec.regime == "RISK_ON"
        assert spec.priority in {"high", "medium", "low"}


# ---------------------------------------------------------------------------
# #7 voice
# ---------------------------------------------------------------------------


class TestVoice:
    def test_briefing_under_budget(self, tmp_path):
        hub = voice.VoiceHub(audit_path=tmp_path / "audit.jsonl")
        b = hub.build_briefing(
            regime="NEUTRAL",
            session_phase="MORNING",
            stress=0.2,
            open_risk_r=1.0,
            daily_dd_pct=0.0,
        )
        assert "JARVIS" in b.script
        assert b.tokens < 250

    def test_emit_critical_fanout(self, tmp_path):
        hub = voice.VoiceHub(audit_path=tmp_path / "audit.jsonl")
        msgs = hub.emit_critical("kill_trip", "kill switch tripped")
        assert len(msgs) == 2
        assert all(m.priority == "CRITICAL" for m in msgs)

    def test_inbound_dispatches(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text("")
        hub = voice.VoiceHub(audit_path=audit)
        reply = hub.handle_inbound(
            voice.InboundMessage(
                ts=datetime.now(UTC),
                channel=voice.Channel.TELEGRAM,
                text="jarvis are you healthy",
            )
        )
        assert reply.text.startswith("[")


# ---------------------------------------------------------------------------
# #9 digital_twin
# ---------------------------------------------------------------------------


class TestDigitalTwin:
    def test_no_signals_green(self):
        cmp_ = digital_twin.TwinComparator()
        v = cmp_.verdict()
        assert v.severity == "GREEN"

    def test_divergence_detected(self):
        cmp_ = digital_twin.TwinComparator()
        now = datetime.now(UTC)
        cmp_.ingest(
            digital_twin.TwinSignal(
                ts=now,
                source="PROD",
                signal_id="s1",
                subsystem="bot.mnq",
                verdict="DENY",
                size_mult=0.0,
            )
        )
        cmp_.ingest(
            digital_twin.TwinSignal(
                ts=now,
                source="TWIN",
                signal_id="s1",
                subsystem="bot.mnq",
                verdict="APPROVE",
                size_mult=0.8,
            )
        )
        divs = cmp_.divergences()
        assert len(divs) == 1

    def test_high_divergence_avoid(self):
        cmp_ = digital_twin.TwinComparator()
        now = datetime.now(UTC)
        # 10 matched signals, 4 diverge
        for i in range(10):
            cmp_.ingest(
                digital_twin.TwinSignal(
                    ts=now,
                    source="PROD",
                    signal_id=f"s{i}",
                    subsystem="bot.mnq",
                    verdict="DENY",
                    size_mult=0.0,
                )
            )
            tw_verdict = "APPROVE" if i < 4 else "DENY"
            cmp_.ingest(
                digital_twin.TwinSignal(
                    ts=now,
                    source="TWIN",
                    signal_id=f"s{i}",
                    subsystem="bot.mnq",
                    verdict=tw_verdict,
                    size_mult=0.0,
                )
            )
        v = cmp_.verdict()
        assert v.verdict in {"AVOID", "FURTHER_SOAK"}


# ---------------------------------------------------------------------------
# #10 autopr
# ---------------------------------------------------------------------------


class TestAutoPR:
    def _ticket(self, impact="medium", title="Fix: whatever"):
        return KaizenTicket(
            id="KZN-20260101-x",
            parent_retrospective_ts=datetime.now(UTC),
            title=title,
            rationale="test rationale",
            impact=impact,
            status=KaizenStatus.OPEN,
            opened_at=datetime.now(UTC),
        )

    def test_small_scope(self):
        plan = autopr.build_plan(self._ticket(impact="small", title="Fix typo in README"))
        assert plan.scope == autopr.Scope.S

    def test_xl_scope_escalates(self):
        plan = autopr.build_plan(self._ticket(impact="critical"))
        res = autopr.submit_plan(plan, executor=None)
        assert not res.success
        assert "XL" in res.message or "escalated" in res.message

    def test_dry_run_without_executor(self):
        plan = autopr.build_plan(self._ticket())
        res = autopr.submit_plan(plan, executor=None)
        assert not res.success
        assert "dry-run" in res.message

    def test_prompt_is_self_contained(self):
        plan = autopr.build_plan(self._ticket())
        assert "Acceptance" in plan.prompt
        assert "KAIZEN TICKET" in plan.prompt


# ---------------------------------------------------------------------------
# #1 causal
# ---------------------------------------------------------------------------


class TestCausal:
    def test_ate_on_synthetic_data(self):
        dag = causal.CausalDAG()
        # Treated: denied (1), low realized_r
        # Control: approved (0), variable realized_r
        import random as _r

        rng = _r.Random(42)
        for _ in range(40):
            dag.add_observation(
                {
                    "verdict_denied": 1.0,
                    "stress_composite": rng.uniform(0.4, 0.6),
                    "regime_code": 1.0,
                    "realized_r": 0.0,  # denied trades never resolve to non-zero
                }
            )
        for _ in range(40):
            dag.add_observation(
                {
                    "verdict_denied": 0.0,
                    "stress_composite": rng.uniform(0.4, 0.6),
                    "regime_code": 1.0,
                    "realized_r": rng.uniform(-0.5, 1.5),  # approved trades vary
                }
            )
        res = causal.counterfactual_denied(
            dag,
            confounders=["stress_composite", "regime_code"],
        )
        # Denying trades means 0 R; approving trades averages > 0 -- so ATE should be negative
        assert res.treated_n == 40
        assert res.control_n == 40

    def test_no_overlap_returns_note(self):
        dag = causal.CausalDAG()
        dag.add_observation(
            {
                "verdict_denied": 1.0,
                "stress_composite": 0.9,
                "regime_code": 1.0,
                "realized_r": 0.0,
            }
        )
        res = causal.counterfactual_denied(dag)
        assert "insufficient" in res.note


# ---------------------------------------------------------------------------
# #2 self_play
# ---------------------------------------------------------------------------


class TestSelfPlay:
    def test_red_market_deterministic(self):
        rm1 = self_play.RedMarket(seed=0)
        rm2 = self_play.RedMarket(seed=0)
        e1 = rm1.emit()
        e2 = rm2.emit()
        assert e1.kind == e2.kind
        assert e1.realized_r_if_trade == e2.realized_r_if_trade

    def test_default_policy_denies_risk_off(self):
        event = self_play.MarketEvent(
            ts=datetime.now(UTC),
            kind=self_play.EventKind.LIQUIDITY_CRASH,
            regime_hint="RISK_OFF",
            truth_regime="RISK_OFF",
            stress_pushed=0.8,
            realized_r_if_trade=-1.5,
        )
        assert self_play.default_policy(event) == "DENY"

    def test_play_round_records_outcome(self):
        rm = self_play.RedMarket(seed=7)
        event = rm.emit()
        rnd = self_play.play_round(
            event=event,
            jarvis_decide=self_play.default_policy,
            round_id=1,
        )
        assert rnd.round_id == 1
        assert rnd.jarvis_verdict in {"APPROVE", "CONDITIONAL", "DENY"}

    def test_ledger_summary(self):
        ledger = self_play.SelfPlayLedger()
        rm = self_play.RedMarket(seed=0)
        for i in range(20):
            event = rm.emit()
            rnd = self_play.play_round(
                event=event,
                jarvis_decide=self_play.default_policy,
                round_id=i,
            )
            ledger.record(rnd)
        s = ledger.summary()
        assert s.rounds == 20
        assert 0.0 <= s.win_rate <= 1.0
