"""Tests for kaizen_loop's supercharge hooks: hot_learner decay + wiring audit."""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from eta_engine.scripts import kaizen_loop


@contextlib.contextmanager
def _mock_kaizen_deps(
    *,
    elite=None,
    mc=None,
    audit=None,
    decay_side_effect=None,
    audit_side_effect=None,
):
    """Bundle the three external dependencies kaizen_loop calls each pass."""
    elite = elite or {"bots": {}, "tier_counts": {}}
    mc = mc or {"bots": {}, "verdict_counts": {}}
    audit = audit if audit is not None else []
    audit_kw = {"side_effect": audit_side_effect} if audit_side_effect is not None else {"return_value": audit}
    decay_kw = {"side_effect": decay_side_effect} if decay_side_effect is not None else {}
    with (
        patch("eta_engine.scripts.elite_scoreboard.analyze", return_value=elite),
        patch("eta_engine.scripts.monte_carlo_validator.analyze", return_value=mc),
        patch("eta_engine.scripts.jarvis_wiring_audit.audit", **audit_kw) as m_audit,
        patch(
            "eta_engine.brain.jarvis_v3.hot_learner.decay_overnight",
            **decay_kw,
        ) as m_decay,
    ):
        yield m_audit, m_decay


def test_run_loop_calls_hot_learner_decay():
    """Each kaizen pass triggers overnight decay so per-school session weights
    don't accumulate noise across days."""
    with _mock_kaizen_deps() as (_audit, m_decay):
        kaizen_loop.run_loop(since_iso=None, bootstraps=10, apply_actions=False)
        assert m_decay.called, "hot_learner.decay_overnight was not called"


def test_run_loop_invokes_wiring_audit():
    """Wiring audit fires every pass so dark modules surface in the report."""
    with _mock_kaizen_deps() as (m_audit, _decay):
        kaizen_loop.run_loop(since_iso=None, bootstraps=10, apply_actions=False)
        assert m_audit.called, "jarvis_wiring_audit.audit was not called"


def test_dark_module_appears_in_report():
    """A module that's been dark for >=7 days surfaces in report['wiring']."""
    from eta_engine.scripts.jarvis_wiring_audit import ModuleStatus

    fake_status = [
        ModuleStatus(
            module="portfolio_brain",
            expected_to_fire=True,
            fires_per_consult_empirical=0.0,
            dark_for_days=10,
        ),
        ModuleStatus(
            module="trace_emitter",
            expected_to_fire=True,
            fires_per_consult_empirical=1.0,
            dark_for_days=0,
        ),
        # Research-only modules are never reported as dark
        ModuleStatus(
            module="some_research_module",
            expected_to_fire=False,
            fires_per_consult_empirical=0.0,
            dark_for_days=999,
        ),
    ]
    with _mock_kaizen_deps(audit=fake_status):
        report = kaizen_loop.run_loop(
            since_iso=None,
            bootstraps=10,
            apply_actions=False,
        )
    wiring = report["wiring"]
    assert wiring["n_dark_modules"] == 1
    assert "portfolio_brain" in wiring["dark_modules"]
    assert "trace_emitter" not in wiring["dark_modules"]
    # Research-only is excluded from dark count
    assert "some_research_module" not in wiring["dark_modules"]
    assert wiring["n_total_expected_to_fire"] == 2


def test_wiring_summary_present_even_when_audit_returns_empty():
    """Empty audit shouldn't crash report assembly — wiring section still
    appears with zero counts."""
    with _mock_kaizen_deps():
        report = kaizen_loop.run_loop(
            since_iso=None,
            bootstraps=10,
            apply_actions=False,
        )
    assert "wiring" in report
    assert report["wiring"]["n_dark_modules"] == 0
    assert report["wiring"]["n_total_modules"] == 0


def test_hot_learner_decay_failure_does_not_block_pass():
    """If hot_learner explodes, the kaizen pass must still complete."""
    with _mock_kaizen_deps(decay_side_effect=RuntimeError("learner is dead")):
        report = kaizen_loop.run_loop(
            since_iso=None,
            bootstraps=10,
            apply_actions=False,
        )
    assert report is not None
    assert "wiring" in report


def test_wiring_audit_failure_does_not_block_pass():
    """If wiring audit raises, the kaizen pass must still produce a report
    with an empty wiring summary."""
    with _mock_kaizen_deps(audit_side_effect=RuntimeError("audit on fire")):
        report = kaizen_loop.run_loop(
            since_iso=None,
            bootstraps=10,
            apply_actions=False,
        )
    assert report["wiring"]["n_total_modules"] == 0


# pytest import is unused in this file at the moment but reserved for
# fixture-based extensions (e.g., tmp_path-backed report dir tests)
_unused_pytest = pytest
