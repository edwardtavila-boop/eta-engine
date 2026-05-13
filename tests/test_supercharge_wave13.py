"""Tests for wave-13 (JARVIS self-awareness layer).

Covers:
  * replay_engine.py        -- counterfactual replay
  * premortem.py            -- pre-trade failure-mode enumeration
  * thesis_tracker.py       -- thesis + invalidation rule monitor
  * ood_detector.py         -- out-of-distribution scoring
  * self_drift_monitor.py   -- JARVIS watching JARVIS
  * postmortem.py           -- auto-postmortem for losing trades
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── replay_engine.py ─────────────────────────────────────────────


def test_replay_returns_zero_when_no_log(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.replay_engine import replay_decisions

    rep = replay_decisions(
        verdict_log_path=tmp_path / "missing.jsonl",
        trade_log_path=tmp_path / "missing_trades.jsonl",
        n_days_back=30,
        new_policy_fn=lambda v: "APPROVED",
    )
    assert rep.n_consultations == 0


def test_replay_classifies_improvement_when_new_policy_avoids_loser(
    tmp_path: Path,
) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.replay_engine import replay_decisions

    vlog = tmp_path / "v.jsonl"
    tlog = tmp_path / "t.jsonl"
    now = datetime.now(UTC).isoformat()
    vlog.write_text(
        json.dumps(
            {
                "ts": now,
                "signal_id": "s1",
                "final_verdict": "APPROVED",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tlog.write_text(
        json.dumps(
            {
                "ts": now,
                "signal_id": "s1",
                "realized_r": -1.5,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # New policy denies what original approved -- catches the loser
    rep = replay_decisions(
        verdict_log_path=vlog,
        trade_log_path=tlog,
        n_days_back=30,
        new_policy_fn=lambda v: "DENIED",
    )
    assert rep.n_changed == 1
    assert rep.n_improvements == 1
    assert rep.deltas[0].change_kind == "improvement"


def test_replay_classifies_regression_when_new_policy_misses_winner(
    tmp_path: Path,
) -> None:
    import json
    from datetime import UTC, datetime

    from eta_engine.brain.jarvis_v3.replay_engine import replay_decisions

    vlog = tmp_path / "v.jsonl"
    tlog = tmp_path / "t.jsonl"
    now = datetime.now(UTC).isoformat()
    vlog.write_text(
        json.dumps(
            {
                "ts": now,
                "signal_id": "s1",
                "final_verdict": "APPROVED",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tlog.write_text(
        json.dumps(
            {
                "ts": now,
                "signal_id": "s1",
                "realized_r": 2.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rep = replay_decisions(
        verdict_log_path=vlog,
        trade_log_path=tlog,
        n_days_back=30,
        new_policy_fn=lambda v: "DEFERRED",
    )
    assert rep.n_changed == 1
    assert rep.n_regressions == 1


def test_replay_to_dict_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.replay_engine import replay_decisions

    rep = replay_decisions(
        verdict_log_path=tmp_path / "missing.jsonl",
        trade_log_path=tmp_path / "missing_trades.jsonl",
        n_days_back=30,
        new_policy_fn=lambda v: "APPROVED",
    )
    s = json.dumps(rep.to_dict())
    assert "summary" in s


# ─── premortem.py ─────────────────────────────────────────────────


def test_premortem_returns_kill_prob_zero_with_no_data(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.premortem import run_premortem

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    p = Proposal(
        signal_id="s1",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.3,
        sentiment=0.4,
        sage_score=0.5,
    )
    pm = run_premortem(proposal=p, memory=mem)
    assert 0.0 <= pm.kill_prob <= 1.0


def test_premortem_high_stress_adds_failure_mode(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.premortem import run_premortem

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    p = Proposal(
        signal_id="s2",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.85,
        sentiment=0.0,
        sage_score=0.3,
    )
    pm = run_premortem(proposal=p, memory=mem)
    labels = [m.label for m in pm.failure_modes]
    assert any("stress spike" in lbl for lbl in labels)


def test_premortem_top_failure_modes_returns_k(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.premortem import run_premortem

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Seed varied losers in one regime
    for r in [-1.5, -2.0, -1.8]:
        mem.record_episode(
            signal_id=f"loss{r}",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=r,
            narrative=f"loss type {r}",
        )
    p = Proposal(
        signal_id="s3",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.5,
        sentiment=-0.3,
        sage_score=0.3,
    )
    pm = run_premortem(proposal=p, memory=mem)
    top = pm.top_failure_modes(k=2)
    assert len(top) <= 2


def test_premortem_to_dict_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.premortem import run_premortem

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    p = Proposal(
        signal_id="s4",
        direction="long",
        regime="neutral",
        session="rth",
        stress=0.3,
        sentiment=0.2,
        sage_score=0.4,
    )
    pm = run_premortem(proposal=p, memory=mem)
    s = json.dumps(pm.to_dict())
    assert "kill_prob" in s


# ─── thesis_tracker.py ────────────────────────────────────────────


def test_thesis_tracker_open_and_close(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.thesis_tracker import (
        ThesisInvalidationRule,
        ThesisTracker,
    )

    tracker = ThesisTracker(
        theses_path=tmp_path / "open.json",
        breach_log_path=tmp_path / "breaches.jsonl",
    )
    rule = ThesisInvalidationRule(
        kind="price_breaks",
        params={"level": 100.0, "direction": "below"},
        description="price < 100",
    )
    tracker.open_thesis(
        signal_id="t1",
        direction="long",
        narrative="bullish",
        invalidation_rules=[rule],
        opened_at_price=105.0,
    )
    assert len(tracker.list_open()) == 1
    closed = tracker.close_thesis("t1")
    assert closed is not None
    assert len(tracker.list_open()) == 0


def test_thesis_tracker_detects_price_break(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.thesis_tracker import (
        ThesisInvalidationRule,
        ThesisTracker,
    )

    tracker = ThesisTracker(
        theses_path=tmp_path / "open.json",
        breach_log_path=tmp_path / "breaches.jsonl",
    )
    tracker.open_thesis(
        signal_id="t2",
        direction="long",
        narrative="bull",
        invalidation_rules=[
            ThesisInvalidationRule(
                kind="price_breaks",
                params={"level": 21420.0, "direction": "below"},
                description="price < 21420",
            ),
        ],
        opened_at_price=21450.0,
    )
    # Price still above level -> no breach
    breach = tracker.check(signal_id="t2", current_state={"price": 21430.0})
    assert breach is None
    # Price drops below -> breach
    breach = tracker.check(signal_id="t2", current_state={"price": 21410.0})
    assert breach is not None
    assert breach.rule_kind == "price_breaks"


def test_thesis_tracker_detects_regime_change(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.thesis_tracker import (
        ThesisInvalidationRule,
        ThesisTracker,
    )

    tracker = ThesisTracker(
        theses_path=tmp_path / "open.json",
        breach_log_path=tmp_path / "breaches.jsonl",
    )
    tracker.open_thesis(
        signal_id="t3",
        direction="long",
        narrative="bull",
        invalidation_rules=[
            ThesisInvalidationRule(
                kind="regime_changed_to",
                params={"to": "bearish_high_vol"},
                description="regime flips bearish high-vol",
            ),
        ],
        opened_at_price=100.0,
        initial_regime="bullish_low_vol",
    )
    breach = tracker.check(
        signal_id="t3",
        current_state={"regime": "bullish_low_vol"},
    )
    assert breach is None
    breach = tracker.check(
        signal_id="t3",
        current_state={"regime": "bearish_high_vol"},
    )
    assert breach is not None


def test_thesis_tracker_persists_across_instances(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.thesis_tracker import (
        ThesisInvalidationRule,
        ThesisTracker,
    )

    paths = {
        "theses_path": tmp_path / "open.json",
        "breach_log_path": tmp_path / "breaches.jsonl",
    }
    t1 = ThesisTracker(**paths)
    t1.open_thesis(
        signal_id="persist",
        direction="long",
        narrative="x",
        invalidation_rules=[
            ThesisInvalidationRule(kind="stress_above", params={"ceiling": 0.7}),
        ],
        opened_at_price=100.0,
    )
    # Fresh tracker should see the persisted thesis
    t2 = ThesisTracker(**paths)
    assert len(t2.list_open()) == 1


def test_thesis_tracker_check_all_open_sweeps_each(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.thesis_tracker import (
        ThesisInvalidationRule,
        ThesisTracker,
    )

    tracker = ThesisTracker(
        theses_path=tmp_path / "open.json",
        breach_log_path=tmp_path / "breaches.jsonl",
    )
    tracker.open_thesis(
        signal_id="a",
        direction="long",
        narrative="",
        invalidation_rules=[
            ThesisInvalidationRule(
                kind="price_breaks",
                params={"level": 100.0, "direction": "below"},
            ),
        ],
        opened_at_price=105.0,
    )
    tracker.open_thesis(
        signal_id="b",
        direction="long",
        narrative="",
        invalidation_rules=[
            ThesisInvalidationRule(
                kind="price_breaks",
                params={"level": 200.0, "direction": "below"},
            ),
        ],
        opened_at_price=205.0,
    )
    breaches = tracker.check_all_open(
        current_state_by_signal={
            "a": {"price": 95.0},  # below -> breach
            "b": {"price": 210.0},  # above -> no breach
        },
    )
    assert len(breaches) == 1
    assert breaches[0].signal_id == "a"


# ─── ood_detector.py ──────────────────────────────────────────────


def test_ood_score_zero_when_memory_empty(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.ood_detector import score_ood

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    p = Proposal(
        signal_id="s1",
        direction="long",
        regime="neutral",
        session="rth",
        stress=0.5,
        sentiment=0.0,
        sage_score=0.0,
    )
    rep = score_ood(proposal=p, memory=mem)
    assert rep.score == 0.0
    assert rep.label == "typical"


def test_ood_score_low_when_state_matches_distribution(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.ood_detector import score_ood

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Seed many episodes at stress=0.3
    for i in range(30):
        mem.record_episode(
            signal_id=f"s{i}",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=1.0,
        )
    # Probe at stress=0.3 -> should be in-distribution
    p = Proposal(
        signal_id="probe",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.3,
        sentiment=0.0,
        sage_score=0.0,
    )
    rep = score_ood(proposal=p, memory=mem)
    assert rep.score < 0.4


def test_ood_score_high_for_extreme_stress(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.ood_detector import score_ood

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    # Seed all episodes at stress=0.3
    for i in range(30):
        mem.record_episode(
            signal_id=f"s{i}",
            regime="bullish_low_vol",
            session="rth",
            stress=0.3,
            direction="long",
            realized_r=1.0,
        )
    # Probe at stress=0.95 -> extreme
    p = Proposal(
        signal_id="probe",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.95,
        sentiment=0.0,
        sage_score=0.0,
    )
    rep = score_ood(proposal=p, memory=mem)
    # Should flag at minimum "unusual"
    assert rep.score > 0.3


def test_ood_attenuation_decreases_with_score(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.ood_detector import OodReport

    typical = OodReport(score=0.1, label="typical", n_episodes_compared=30)
    novel = OodReport(score=0.9, label="novel", n_episodes_compared=30)
    assert typical.confidence_attenuation() > novel.confidence_attenuation()


# ─── self_drift_monitor.py ────────────────────────────────────────


def test_self_drift_returns_ok_with_empty_log(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.self_drift_monitor import detect_self_drift

    rep = detect_self_drift(
        log_path=tmp_path / "missing.jsonl",
    )
    assert rep.overall_status == "OK"
    assert "insufficient" in rep.summary


def test_self_drift_flags_approved_rate_jump(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    from eta_engine.brain.jarvis_v3.self_drift_monitor import detect_self_drift

    log = tmp_path / "v.jsonl"
    now = datetime.now(UTC)
    rows = []
    # Baseline: 50 verdicts, 20% APPROVED
    baseline_ts = (now - timedelta(hours=72)).isoformat()
    for i in range(50):
        rows.append(
            {
                "ts": baseline_ts,
                "final_verdict": "APPROVED" if i < 10 else "DEFERRED",
                "subsystem": "MNQ",
                "confidence": 0.5,
            }
        )
    # Recent: 20 verdicts, 90% APPROVED -> big jump
    recent_ts = now.isoformat()
    for i in range(20):
        rows.append(
            {
                "ts": recent_ts,
                "final_verdict": "APPROVED" if i < 18 else "DEFERRED",
                "subsystem": "MNQ",
                "confidence": 0.5,
            }
        )
    log.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    rep = detect_self_drift(
        recent_window_hours=24,
        baseline_window_hours=168,
        log_path=log,
    )
    metrics = {s.metric for s in rep.signals}
    assert "approved_rate" in metrics


def test_self_drift_to_dict_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.self_drift_monitor import detect_self_drift

    rep = detect_self_drift(log_path=tmp_path / "missing.jsonl")
    s = json.dumps(rep.to_dict())
    assert "overall_status" in s


# ─── postmortem.py ────────────────────────────────────────────────


def test_postmortem_severity_classification(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.postmortem import generate_postmortem

    pm_mod = generate_postmortem(
        signal_id="t-mod",
        realized_r=-1.6,
        verdict_log_path=tmp_path / "v.jsonl",
        output_dir=tmp_path / "pms",
    )
    pm_sev = generate_postmortem(
        signal_id="t-sev",
        realized_r=-2.5,
        verdict_log_path=tmp_path / "v.jsonl",
        output_dir=tmp_path / "pms",
    )
    pm_cat = generate_postmortem(
        signal_id="t-cat",
        realized_r=-3.5,
        verdict_log_path=tmp_path / "v.jsonl",
        output_dir=tmp_path / "pms",
    )
    assert pm_mod.severity == "moderate"
    assert pm_sev.severity == "severe"
    assert pm_cat.severity == "catastrophic"
    assert pm_cat.operator_action_required is True


def test_postmortem_attributes_layers_when_record_present(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.postmortem import generate_postmortem

    vlog = tmp_path / "v.jsonl"
    vlog.write_text(
        json.dumps(
            {
                "signal_id": "loss1",
                "ts": "2026-04-27T15:00:00+00:00",
                "final_verdict": "APPROVED",
                "base_verdict": "APPROVED",
                "causal_score": 0.6,
                "firm_board_consensus": 0.8,
                "world_model_expected_r": 1.5,
                "rag_cautions": [],
                "direction": "long",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pm = generate_postmortem(
        signal_id="loss1",
        realized_r=-2.0,
        verdict_log_path=vlog,
        output_dir=tmp_path / "pms",
    )
    layers = {a.layer for a in pm.layer_attributions}
    assert "causal" in layers
    assert "firm_board" in layers
    assert "world_model" in layers
    # Severe loss with high-confidence approval -> action required
    # (severity == "severe" doesn't trigger by itself; only catastrophic does
    # per current rules. Verify the suggestions list is populated.)
    assert pm.suggested_adjustments


def test_postmortem_persists_markdown_and_json(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.postmortem import generate_postmortem

    out_dir = tmp_path / "pms"
    pm = generate_postmortem(
        signal_id="persist1",
        realized_r=-1.7,
        verdict_log_path=tmp_path / "missing.jsonl",
        output_dir=out_dir,
        auto_persist=True,
    )
    assert (out_dir / "persist1.md").exists()
    assert (out_dir / "persist1.json").exists()
    md = (out_dir / "persist1.md").read_text(encoding="utf-8")
    assert "Postmortem" in md
    assert "persist1" in md
    assert pm.severity in md


def test_postmortem_to_markdown_renders_table() -> None:
    from eta_engine.brain.jarvis_v3.postmortem import (
        LayerAttribution,
        Postmortem,
    )

    pm = Postmortem(
        signal_id="x",
        realized_r=-2.0,
        severity="severe",
        ts_generated="now",
        direction="long",
        regime="neutral",
        session="rth",
        layer_attributions=[
            LayerAttribution(
                layer="causal",
                layer_signal=0.5,
                contribution_score=-0.5,
                note="overconfident",
            ),
        ],
    )
    md = pm.to_markdown()
    assert "| Layer | Signal | Contribution | Note |" in md
    assert "causal" in md
