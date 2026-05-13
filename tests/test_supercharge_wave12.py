"""Tests for wave-12 (JARVIS consolidation as source of truth).

Covers:
  * intelligence.py     -- JarvisIntelligence wrapper around JarvisAdmin
  * feedback_loop.py    -- close_trade() multi-subsystem propagation
  * health_check.py     -- jarvis_health() self-diagnostic
  * admin_query.py      -- operator read queries
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from pathlib import Path


# ─── Helpers ──────────────────────────────────────────────────────


def _stub_action_request(**overrides):
    """Build a minimal stand-in for ActionRequest. We use MagicMock
    rather than the real pydantic model to avoid pulling the full
    JarvisAdmin chain into every test."""
    req = MagicMock()
    req.request_id = overrides.get("request_id", "req-1")
    req.subsystem = overrides.get("subsystem", "BOT")
    req.action_type = overrides.get("action_type", "ORDER_PLACE")
    req.payload = overrides.get(
        "payload",
        {
            "regime": "bullish_low_vol",
            "session": "rth",
            "stress": 0.3,
            "direction": "long",
            "sentiment": 0.4,
            "sage_score": 0.5,
            "slippage_bps_estimate": 2.0,
        },
    )
    return req


def _stub_action_response(verdict="APPROVED", reason_code="ok", size_cap_qty=None):
    resp = MagicMock()
    resp.verdict = verdict
    resp.reason_code = reason_code
    resp.size_cap_qty = size_cap_qty
    return resp


# ─── intelligence.py ──────────────────────────────────────────────


def test_intelligence_passthrough_when_disabled(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )

    admin = MagicMock()
    admin.request_approval.return_value = _stub_action_response("APPROVED")
    intel = JarvisIntelligence(
        admin=admin,
        memory=None,
        cfg=IntelligenceConfig(enable_intelligence=False),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    req = _stub_action_request()
    v = intel.consult(req)
    assert v.intelligence_enabled is False
    assert v.final_verdict == "APPROVED"
    assert v.final_size_multiplier == 1.0


def test_intelligence_appends_verdict_log(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )

    admin = MagicMock()
    admin.request_approval.return_value = _stub_action_response("APPROVED")
    log = tmp_path / "verdicts.jsonl"
    intel = JarvisIntelligence(
        admin=admin,
        memory=None,
        cfg=IntelligenceConfig(enable_intelligence=False),
        verdict_log=log,
    )
    intel.consult(_stub_action_request())
    intel.consult(_stub_action_request(request_id="req-2"))
    assert log.exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_intelligence_passes_through_denied_verdict(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )

    admin = MagicMock()
    admin.request_approval.return_value = _stub_action_response(
        "DENIED",
        reason_code="kill_switch_armed",
    )
    intel = JarvisIntelligence(
        admin=admin,
        memory=None,
        cfg=IntelligenceConfig(enable_intelligence=False),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    v = intel.consult(_stub_action_request())
    assert v.final_verdict == "DENIED"
    assert v.is_blocked() is True


def test_intelligence_consolidated_verdict_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )

    admin = MagicMock()
    admin.request_approval.return_value = _stub_action_response("APPROVED")
    intel = JarvisIntelligence(
        admin=admin,
        memory=None,
        cfg=IntelligenceConfig(enable_intelligence=False),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    v = intel.consult(_stub_action_request())
    rec = v.to_audit_record()
    s = json.dumps(rec, default=str)
    assert "final_verdict" in s
    assert "intelligence_enabled" in s


def test_intelligence_with_memory_runs_full_layers(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Seed some episodes so RAG / world model have data
    for r in [1.0, 1.5, 0.8, -0.3, 1.2]:
        mem.record_episode(
            signal_id=f"s{r}",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=r,
            narrative="EMA stack confluence",
        )
    admin = MagicMock()
    admin.request_approval.return_value = _stub_action_response("APPROVED")
    intel = JarvisIntelligence(
        admin=admin,
        memory=mem,
        cfg=IntelligenceConfig(enable_intelligence=True),
        verdict_log=tmp_path / "verdicts.jsonl",
    )
    v = intel.consult(
        _stub_action_request(),
        current_narrative="EMA stack aligned, sage approved",
    )
    assert v.intelligence_enabled is True
    # firm-board ran -> consensus is set
    assert 0.0 <= v.firm_board_consensus <= 1.0
    # RAG ran -> summary populated
    assert v.rag_summary != ""


def test_intelligence_causal_veto_only_downgrades_when_enabled(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.intelligence import (
        IntelligenceConfig,
        JarvisIntelligence,
    )
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Seed strong losers under approve_full -> intervention score low
    for _ in range(8):
        mem.record_episode(
            signal_id="bad",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=-1.5,
            narrative="loser",
            extra={"action": "approve_full"},
        )
    admin = MagicMock()
    admin.request_approval.return_value = _stub_action_response("APPROVED")

    # Default: causal veto annotates only
    intel_no_veto = JarvisIntelligence(
        admin=admin,
        memory=mem,
        cfg=IntelligenceConfig(
            enable_intelligence=True,
            causal_veto_can_downgrade=False,
        ),
        verdict_log=tmp_path / "v1.jsonl",
    )
    v1 = intel_no_veto.consult(_stub_action_request())
    assert v1.final_verdict == "APPROVED"  # not downgraded

    # Enabled: veto downgrades
    intel_veto = JarvisIntelligence(
        admin=admin,
        memory=mem,
        cfg=IntelligenceConfig(
            enable_intelligence=True,
            causal_veto_can_downgrade=True,
            causal_veto_threshold=-0.4,
        ),
        verdict_log=tmp_path / "v2.jsonl",
    )
    v2 = intel_veto.consult(_stub_action_request())
    # Causal score should be negative; with veto enabled, final downgrades
    if v2.causal_score < -0.4:
        assert v2.final_verdict in {"DEFERRED", "DENIED"}


# ─── feedback_loop.py ─────────────────────────────────────────────


def test_close_trade_records_episode(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.feedback_loop import close_trade
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    rec = close_trade(
        signal_id="s1",
        realized_r=1.5,
        regime="bullish_low_vol",
        session="rth",
        stress=0.3,
        direction="long",
        narrative="winner",
        action_taken="approve_full",
        bot_id="testbot",
        memory=mem,
        trade_log_path=tmp_path / "trades.jsonl",
    )
    assert "memory" in rec.layers_updated
    assert len(mem._episodes) == 1
    assert mem._episodes[0].signal_id == "s1"


def test_close_trade_updates_filter_bandit(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.feedback_loop import close_trade
    from eta_engine.brain.jarvis_v3.filter_bandit import FilterBandit

    fb = FilterBandit(state_path=tmp_path / "fb.json")
    fb.register("ema_filter", lambda **_: True)
    rec = close_trade(
        signal_id="s2",
        realized_r=2.0,
        regime="neutral",
        session="rth",
        stress=0.5,
        direction="long",
        action_taken="approve_full",
        filter_bandit=fb,
        filter_arm_used="ema_filter",
        trade_log_path=tmp_path / "trades.jsonl",
    )
    assert "filter_bandit" in rec.layers_updated
    report = fb.report()
    ema_arm = next(r for r in report if r["arm"] == "ema_filter")
    assert ema_arm["pulls"] == 1


def test_close_trade_appends_to_trade_log(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.feedback_loop import close_trade

    log_path = tmp_path / "trades.jsonl"
    close_trade(
        signal_id="s3",
        realized_r=0.5,
        regime="neutral",
        session="rth",
        stress=0.5,
        direction="long",
        action_taken="approve_half",
        trade_log_path=log_path,
    )
    assert log_path.exists()
    assert "s3" in log_path.read_text(encoding="utf-8")


def test_close_trade_default_path_is_canonical() -> None:
    from eta_engine.brain.jarvis_v3 import feedback_loop
    from eta_engine.scripts import workspace_roots

    assert feedback_loop.DEFAULT_TRADE_LOG == workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH


def test_replay_trade_closes_into_memory(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.feedback_loop import (
        close_trade,
        replay_trade_closes,
    )
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    log_path = tmp_path / "trades.jsonl"
    # First write 3 records via close_trade WITHOUT memory (just log)
    for i in range(3):
        close_trade(
            signal_id=f"s{i}",
            realized_r=1.0,
            regime="neutral",
            session="rth",
            stress=0.5,
            direction="long",
            action_taken="approve_full",
            trade_log_path=log_path,
            extra={"stress": 0.5},
        )
    # Verify log exists
    assert len(log_path.read_text(encoding="utf-8").splitlines()) >= 3
    # Verify each line is valid JSON
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)
    # Now replay into a fresh memory
    fresh_mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    n = replay_trade_closes(memory=fresh_mem, trade_log_path=log_path)
    assert n >= 3
    assert len(fresh_mem._episodes) >= 3


# ─── health_check.py ──────────────────────────────────────────────


def test_jarvis_health_returns_aggregate_report() -> None:
    from eta_engine.brain.jarvis_v3.health_check import jarvis_health

    rep = jarvis_health()
    assert rep.overall_status in {"OK", "DEGRADED", "CRITICAL"}
    assert len(rep.components) >= 5
    assert "JARVIS health" in rep.summary


def test_jarvis_health_to_dict_serializable() -> None:
    import json

    from eta_engine.brain.jarvis_v3.health_check import jarvis_health

    rep = jarvis_health()
    s = json.dumps(rep.to_dict())
    assert "overall_status" in s
    assert "components" in s


def test_jarvis_health_components_have_status_and_detail() -> None:
    from eta_engine.brain.jarvis_v3.health_check import jarvis_health

    rep = jarvis_health()
    for c in rep.components:
        assert c.name
        assert c.status in {"OK", "DEGRADED", "CRITICAL"}
        assert c.detail


# ─── admin_query.py ───────────────────────────────────────────────


def test_recent_verdicts_returns_zero_when_no_log(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.admin_query import recent_verdicts

    rep = recent_verdicts(
        n_hours=24,
        log_path=tmp_path / "missing.jsonl",
    )
    assert rep.n_total == 0


def test_recent_verdicts_aggregates_log(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.admin_query import recent_verdicts

    log = tmp_path / "v.jsonl"
    now = datetime.now(UTC).isoformat()
    rows = [
        {
            "ts": now,
            "final_verdict": "APPROVED",
            "subsystem": "MNQ_BOT",
            "confidence": 0.8,
            "rag_cautions": [],
            "firm_board_consensus": 0.8,
        },
        {
            "ts": now,
            "final_verdict": "DEFERRED",
            "subsystem": "BTC_BOT",
            "confidence": 0.5,
            "rag_cautions": ["lost analog"],
            "firm_board_consensus": 0.4,
        },
    ]
    log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    rep = recent_verdicts(n_hours=24, log_path=log)
    assert rep.n_total == 2
    assert rep.by_final_verdict["APPROVED"] == 1
    assert rep.n_with_cautions == 1
    assert rep.n_with_dissent == 1


def test_memory_regime_stats_aggregates(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.admin_query import memory_regime_stats
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    for r in [1.0, 2.0, -0.5]:
        mem.record_episode(
            signal_id=f"s{r}",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=r,
        )
    for r in [-1.0, 0.5]:
        mem.record_episode(
            signal_id=f"b{r}",
            regime="bearish_low_vol",
            session="rth",
            stress=0.5,
            direction="short",
            realized_r=r,
        )
    stats = memory_regime_stats(memory=mem)
    by_regime = {s.regime: s for s in stats}
    assert by_regime["bullish_low_vol"].n_episodes == 3
    assert by_regime["bearish_low_vol"].n_episodes == 2
    # bullish: 2 wins out of 3 -> ~0.667 (rounded by helper)
    assert abs(by_regime["bullish_low_vol"].win_rate - 2 / 3) < 1e-3


def test_top_analog_episodes_returns_sorted_results(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.admin_query import top_analog_episodes
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    mem.record_episode(
        signal_id="match",
        regime="neutral",
        session="rth",
        stress=0.5,
        direction="long",
        realized_r=1.0,
        narrative="liquidity sweep reclaim with order block",
    )
    mem.record_episode(
        signal_id="other",
        regime="neutral",
        session="rth",
        stress=0.5,
        direction="long",
        realized_r=-0.3,
        narrative="totally different setup keywords here",
    )
    out = top_analog_episodes(
        narrative="liquidity sweep reclaim",
        regime="neutral",
        session="rth",
        stress=0.5,
        memory=mem,
        k=2,
    )
    assert len(out) == 2
    assert out[0]["signal_id"] == "match"


def test_disagreement_hotspots_groups_by_pair(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.admin_query import disagreement_hotspots

    log = tmp_path / "v.jsonl"
    now = datetime.now(UTC).isoformat()
    # Same (subsystem, action) pair, low consensus repeatedly
    rows = [
        {"ts": now, "subsystem": "MNQ", "action": "ORDER", "firm_board_consensus": 0.3},
        {"ts": now, "subsystem": "MNQ", "action": "ORDER", "firm_board_consensus": 0.4},
        {"ts": now, "subsystem": "MNQ", "action": "ORDER", "firm_board_consensus": 0.35},
        {"ts": now, "subsystem": "BTC", "action": "ORDER", "firm_board_consensus": 0.9},
        {"ts": now, "subsystem": "BTC", "action": "ORDER", "firm_board_consensus": 0.95},
        {"ts": now, "subsystem": "BTC", "action": "ORDER", "firm_board_consensus": 0.85},
    ]
    log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    spots = disagreement_hotspots(
        n_hours=24,
        min_count=3,
        log_path=log,
    )
    # MNQ should rank first (lowest avg consensus)
    assert spots[0]["subsystem"] == "MNQ"
    assert spots[0]["avg_consensus"] < spots[1]["avg_consensus"]


def test_trade_close_stats_aggregates(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.admin_query import trade_close_stats

    log = tmp_path / "trades.jsonl"
    now = datetime.now(UTC).isoformat()
    rows = [
        {"ts": now, "realized_r": 1.5},
        {"ts": now, "realized_r": -0.5},
        {"ts": now, "realized_r": 2.0},
    ]
    log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    stats = trade_close_stats(n_hours=24, log_path=log)
    assert stats["n"] == 3
    assert abs(stats["avg_r"] - 1.0) < 1e-3
    assert abs(stats["win_rate"] - 2 / 3) < 1e-3
    assert stats["best_r"] == 2.0
    assert stats["worst_r"] == -0.5
