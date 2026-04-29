"""Tests for Levers 1, 2, 7 (kaizen scaffolding activation, 2026-04-26).

Lever 2: policy_version on JournalEvent + JarvisAdmin
Lever 1: run_kaizen_close_cycle.synthesize_inputs / close_cycle integration
Lever 7: jarvis_denial_rate_alerter.compute_denial_stats / cooldown
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eta_engine.brain.jarvis_v3.kaizen import CycleKind, KaizenLedger, close_cycle
from eta_engine.obs.decision_journal import (
    Actor,
    JournalEvent,
    Outcome,
)

# ─── Lever 2: policy_version field + threading ──────────────────────────


def test_journal_event_has_policy_version_default_zero() -> None:
    ev = JournalEvent(actor=Actor.TRADE_ENGINE, intent="open_mnq_long")
    assert ev.policy_version == 0


def test_journal_event_accepts_explicit_policy_version() -> None:
    ev = JournalEvent(
        actor=Actor.TRADE_ENGINE,
        intent="open_mnq_long",
        policy_version=42,
    )
    assert ev.policy_version == 42


def test_journal_event_rejects_negative_policy_version() -> None:
    with pytest.raises(ValidationError):
        JournalEvent(actor=Actor.TRADE_ENGINE, intent="x", policy_version=-1)


def test_legacy_journal_rows_default_to_version_zero() -> None:
    """JSONL rows from before policy_version was added must still parse."""
    legacy_json = json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "actor": Actor.TRADE_ENGINE.value,
        "intent": "legacy_event",
    })
    ev = JournalEvent.model_validate(json.loads(legacy_json))
    assert ev.policy_version == 0


def test_jarvis_admin_exposes_policy_version() -> None:
    from eta_engine.brain.jarvis_admin import JarvisAdmin

    admin_default = JarvisAdmin()
    assert admin_default.policy_version == 0

    admin_v17 = JarvisAdmin(policy_version=17)
    assert admin_v17.policy_version == 17


def test_jarvis_admin_writes_policy_version_to_audit(tmp_path: Path) -> None:
    """An LLM-routing audit record must include the policy_version field."""
    from eta_engine.brain.jarvis_admin import JarvisAdmin, SubsystemId
    from eta_engine.brain.model_policy import TaskCategory

    audit_path = tmp_path / "audit.jsonl"
    admin = JarvisAdmin(audit_path=audit_path, policy_version=42)
    admin.select_llm_tier(
        subsystem=SubsystemId.BOT_MNQ,
        category=TaskCategory.RED_TEAM_SCORING,
        rationale="lever-2 audit smoke",
    )
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["policy_version"] == 42


# ─── Lever 1: kaizen close cycle ────────────────────────────────────────


def test_synthesize_inputs_buckets_outcomes_correctly() -> None:
    from eta_engine.scripts.run_kaizen_close_cycle import synthesize_inputs

    events = [
        JournalEvent(actor=Actor.TRADE_ENGINE, intent="open_mnq_long",
                     outcome=Outcome.NOTED),
        JournalEvent(actor=Actor.TRADE_ENGINE, intent="open_mnq_long",
                     outcome=Outcome.NOTED),
        JournalEvent(actor=Actor.RISK_GATE, intent="veto_high_vol",
                     outcome=Outcome.BLOCKED),
        JournalEvent(actor=Actor.RISK_GATE, intent="veto_high_vol",
                     outcome=Outcome.BLOCKED),
        JournalEvent(actor=Actor.RISK_GATE, intent="veto_high_vol",
                     outcome=Outcome.BLOCKED),
        JournalEvent(actor=Actor.KILL_SWITCH, intent="manual_override",
                     outcome=Outcome.OVERRIDDEN),
        JournalEvent(actor=Actor.TRADE_ENGINE, intent="rare_event",
                     outcome=Outcome.FAILED),
    ]
    out = synthesize_inputs(events)
    # went_well = NOTED outcomes
    assert any("open_mnq_long" in s for s in out["went_well"])
    # went_poorly = BLOCKED + FAILED + OVERRIDDEN
    assert any("veto_high_vol" in s for s in out["went_poorly"])
    # KPI math
    assert out["kpis"]["total_events"] == 7.0
    # override_rate = 1/7
    assert abs(out["kpis"]["override_rate"] - (1.0 / 7.0)) < 1e-6


def test_close_cycle_with_synthesized_inputs_produces_ticket(tmp_path: Path) -> None:
    """End-to-end: synthesize -> close_cycle -> KaizenLedger.save()."""
    from eta_engine.scripts.run_kaizen_close_cycle import synthesize_inputs

    events = [
        JournalEvent(actor=Actor.RISK_GATE, intent="veto_low_confluence",
                     outcome=Outcome.BLOCKED),
    ]
    inputs = synthesize_inputs(events)
    now = datetime.now(UTC)
    retro, ticket = close_cycle(
        cycle_kind=CycleKind.DAILY,
        window_start=now - timedelta(hours=24),
        window_end=now,
        went_well=inputs["went_well"],
        went_poorly=inputs["went_poorly"],
        surprises=inputs["surprises"],
        kpis=inputs["kpis"],
        now=now,
    )
    assert ticket.title  # MUST produce a +1 (Kaizen doctrine)
    assert ticket.id.startswith("KZN-")
    # And persist via save/load roundtrip.
    ledger_path = tmp_path / "ledger.json"
    ledger = KaizenLedger.load(ledger_path)
    ledger.add_retro(retro)
    ledger.add_ticket(ticket)
    ledger.save(ledger_path)
    reloaded = KaizenLedger.load(ledger_path)
    assert len(reloaded.retrospectives()) == 1
    assert len(reloaded.tickets()) == 1


def test_close_cycle_emits_ticket_even_with_no_events() -> None:
    """Doctrine: every cycle MUST emit at least one +1 ticket."""
    from eta_engine.scripts.run_kaizen_close_cycle import synthesize_inputs

    inputs = synthesize_inputs([])
    now = datetime.now(UTC)
    _, ticket = close_cycle(
        cycle_kind=CycleKind.DAILY,
        window_start=now - timedelta(hours=24),
        window_end=now,
        went_well=inputs["went_well"],
        went_poorly=inputs["went_poorly"],
        surprises=inputs["surprises"],
        kpis=inputs["kpis"],
        now=now,
    )
    assert ticket.title  # Even with zero events, a +1 must be produced


# ─── Lever 7: denial-rate alerter ───────────────────────────────────────


def test_compute_denial_stats_empty() -> None:
    from eta_engine.obs.jarvis_denial_rate_alerter import compute_denial_stats
    stats = compute_denial_stats([])
    assert stats["total"] == 0
    assert stats["denial_rate"] == 0.0


def test_compute_denial_stats_mixed_verdicts() -> None:
    from eta_engine.obs.jarvis_denial_rate_alerter import compute_denial_stats
    records = [
        {"response": {"verdict": "APPROVED"}},
        {"response": {"verdict": "APPROVED"}},
        {"response": {"verdict": "DENIED"}},
        {"response": {"verdict": "DEFERRED"}},
        {"response": {"verdict": "CONDITIONAL"}},
    ]
    stats = compute_denial_stats(records)
    assert stats["total"] == 5
    assert stats["rejections"] == 2  # DENIED + DEFERRED
    assert stats["denial_rate"] == 0.4
    assert stats["verdict_counts"] == {
        "APPROVED": 2, "DENIED": 1, "DEFERRED": 1, "CONDITIONAL": 1,
    }


def test_in_cooldown_returns_false_when_no_state(tmp_path: Path) -> None:
    from eta_engine.obs.jarvis_denial_rate_alerter import in_cooldown
    assert in_cooldown(tmp_path / "missing.json", cooldown_min=30) is False


def test_in_cooldown_returns_true_for_recent_state(tmp_path: Path) -> None:
    from eta_engine.obs.jarvis_denial_rate_alerter import in_cooldown, update_cooldown_state
    state_file = tmp_path / "state.json"
    update_cooldown_state(state_file, {"denial_rate": 0.7, "rejections": 7, "total": 10})
    # Just-fired state with 30 min cooldown => still cold
    assert in_cooldown(state_file, cooldown_min=30) is True
    # 0 cooldown => never cold
    assert in_cooldown(state_file, cooldown_min=0) is False


def test_parse_audit_lines_filters_by_window(tmp_path: Path) -> None:
    from eta_engine.obs.jarvis_denial_rate_alerter import parse_audit_lines
    audit_file = tmp_path / "audit.jsonl"
    now = datetime.now(UTC)
    old_ts = (now - timedelta(minutes=20)).isoformat()
    new_ts = (now - timedelta(minutes=2)).isoformat()
    audit_file.write_text(
        json.dumps({"ts": old_ts, "response": {"verdict": "APPROVED"}}) + "\n" +
        json.dumps({"ts": new_ts, "response": {"verdict": "DENIED"}}) + "\n",
        encoding="utf-8",
    )
    # 5-min window catches only the new record
    records = parse_audit_lines([audit_file], window_min=5)
    assert len(records) == 1
    assert records[0]["response"]["verdict"] == "DENIED"
    # 30-min window catches both
    records = parse_audit_lines([audit_file], window_min=30)
    assert len(records) == 2
