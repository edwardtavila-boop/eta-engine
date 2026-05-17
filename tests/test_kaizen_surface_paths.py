from __future__ import annotations

import inspect

from eta_engine.obs import jarvis_today_verdicts
from eta_engine.scripts import bandit_promotion_check
from eta_engine.scripts import export_to_notion
from eta_engine.scripts import generate_investor_dashboard
from eta_engine.scripts import run_critique_nightly
from eta_engine.scripts import workspace_roots


def test_generate_investor_dashboard_reads_canonical_kaizen_ledger() -> None:
    assert "workspace_roots.ETA_KAIZEN_LEDGER_PATH" in inspect.getsource(generate_investor_dashboard.gather_payload)
    assert generate_investor_dashboard.workspace_roots.ETA_KAIZEN_LEDGER_PATH == workspace_roots.ETA_KAIZEN_LEDGER_PATH
    assert generate_investor_dashboard.workspace_roots.ETA_INVESTOR_DASHBOARD_PATH == (
        workspace_roots.ETA_INVESTOR_DASHBOARD_PATH
    )


def test_export_to_notion_defaults_to_canonical_kaizen_ledger() -> None:
    assert export_to_notion.workspace_roots.ETA_KAIZEN_LEDGER_PATH == workspace_roots.ETA_KAIZEN_LEDGER_PATH
    assert export_to_notion.workspace_roots.ETA_NOTION_EXPORT_DIR == workspace_roots.ETA_NOTION_EXPORT_DIR


def test_daily_review_scripts_default_to_canonical_runtime_dirs() -> None:
    assert export_to_notion.workspace_roots.ETA_JARVIS_AUDIT_DIR == workspace_roots.ETA_JARVIS_AUDIT_DIR
    assert export_to_notion.workspace_roots.ETA_KAIZEN_CRITIQUE_DIR == workspace_roots.ETA_KAIZEN_CRITIQUE_DIR
    assert export_to_notion.workspace_roots.ETA_BANDIT_PROMOTION_DIR == workspace_roots.ETA_BANDIT_PROMOTION_DIR
    assert bandit_promotion_check.workspace_roots.ETA_JARVIS_AUDIT_DIR == workspace_roots.ETA_JARVIS_AUDIT_DIR
    assert bandit_promotion_check.workspace_roots.ETA_BANDIT_PROMOTION_DIR == (
        workspace_roots.ETA_BANDIT_PROMOTION_DIR
    )
    assert run_critique_nightly.workspace_roots.ETA_JARVIS_AUDIT_DIR == workspace_roots.ETA_JARVIS_AUDIT_DIR
    assert run_critique_nightly.workspace_roots.ETA_KAIZEN_CRITIQUE_DIR == workspace_roots.ETA_KAIZEN_CRITIQUE_DIR
    assert "workspace_roots.ETA_JARVIS_AUDIT_DIR" in inspect.getsource(jarvis_today_verdicts.aggregate_today)


def test_export_to_notion_build_digest_uses_selected_audit_dir(monkeypatch, tmp_path) -> None:
    seen: dict[str, object] = {}

    def _fake_aggregate_today(*, audit_globs=None, now=None):
        seen["audit_globs"] = audit_globs
        return {"totals": {}, "top_denial_reasons": [], "avg_conditional_cap": 1.0}

    monkeypatch.setattr("eta_engine.obs.jarvis_today_verdicts.aggregate_today", _fake_aggregate_today)

    export_to_notion._build_digest(
        audit_dir=tmp_path / "audit",
        kaizen_ledger=tmp_path / "kaizen_ledger.json",
        critique_dir=tmp_path / "critique",
        bandit_dir=tmp_path / "bandit",
    )

    assert seen["audit_globs"] == [str(tmp_path / "audit" / "*.jsonl")]
