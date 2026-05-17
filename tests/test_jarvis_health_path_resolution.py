from __future__ import annotations

from eta_engine.brain.jarvis_v3 import health_check


def test_check_calibrator_falls_back_to_legacy_model_dir(monkeypatch, tmp_path) -> None:
    canonical = tmp_path / "var" / "eta_engine" / "state" / "models"
    legacy = tmp_path / "eta_engine" / "state" / "models"
    artifact = legacy / "calibrator_2026-05-15.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(health_check.workspace_roots, "ETA_MODEL_ARTIFACT_DIR", canonical)
    monkeypatch.setattr(health_check.workspace_roots, "ETA_LEGACY_MODEL_ARTIFACT_DIR", legacy)

    component = health_check._check_calibrator()

    assert component.status == health_check.HealthStatus.OK
    assert component.metrics["path"] == artifact.name


def test_check_decision_journal_accepts_legacy_audit_dir(monkeypatch, tmp_path) -> None:
    canonical_journal = tmp_path / "var" / "eta_engine" / "state" / "decision_journal.jsonl"
    canonical_audit = tmp_path / "var" / "eta_engine" / "state" / "jarvis_audit"
    legacy_audit = tmp_path / "eta_engine" / "state" / "jarvis_audit"
    legacy_audit.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(health_check, "ETA_RUNTIME_DECISION_JOURNAL_PATH", canonical_journal)
    monkeypatch.setattr(health_check.workspace_roots, "ETA_JARVIS_AUDIT_DIR", canonical_audit)
    monkeypatch.setattr(health_check.workspace_roots, "ETA_LEGACY_JARVIS_AUDIT_DIR", legacy_audit)

    component = health_check._check_decision_journal()

    assert component.status == health_check.HealthStatus.OK
    assert component.metrics["path"] == str(legacy_audit)


def test_check_intel_verdict_log_falls_back_to_legacy_path(monkeypatch, tmp_path) -> None:
    canonical = tmp_path / "var" / "eta_engine" / "state" / "jarvis_intel" / "verdicts.jsonl"
    legacy = tmp_path / "eta_engine" / "state" / "jarvis_intel" / "verdicts.jsonl"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{"ts":"2026-05-15T12:00:00+00:00"}\n', encoding="utf-8")

    monkeypatch.setattr(health_check.workspace_roots, "ETA_JARVIS_VERDICTS_PATH", canonical)
    monkeypatch.setattr(health_check.workspace_roots, "ETA_LEGACY_JARVIS_VERDICTS_PATH", legacy)

    component = health_check._check_intel_verdict_log()

    assert component.status == health_check.HealthStatus.OK
    assert component.metrics["path"] == str(legacy)
