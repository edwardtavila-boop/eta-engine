from __future__ import annotations

from eta_engine.scripts import _pre_commit_check


def test_pre_commit_blocks_legacy_docs_decision_journal() -> None:
    forbidden = _pre_commit_check._forbidden_staged_files_from_lines(
        [
            "docs/decision_journal.jsonl",
            "docs/alerts_log.jsonl",
            "docs/runtime_log.jsonl",
            "docs/drift_watchdog.jsonl",
            "scripts/run_research_grid.py",
        ]
    )

    assert forbidden == [
        "docs/decision_journal.jsonl",
        "docs/alerts_log.jsonl",
        "docs/runtime_log.jsonl",
        "docs/drift_watchdog.jsonl",
    ]


def test_pre_commit_normalizes_windows_staged_paths() -> None:
    forbidden = _pre_commit_check._forbidden_staged_files_from_lines(
        [
            "docs\\decision_journal.jsonl",
            "docs\\live_data\\live_ticks_btc.jsonl",
        ]
    )

    assert forbidden == [
        "docs/decision_journal.jsonl",
        "docs/live_data/live_ticks_btc.jsonl",
    ]


def test_pre_commit_allows_source_files() -> None:
    forbidden = _pre_commit_check._forbidden_staged_files_from_lines(
        [
            "scripts/_pre_commit_check.py",
            "tests/test_pre_commit_guardrails.py",
        ]
    )

    assert forbidden == []


def test_pre_commit_selects_text_files_for_stale_path_lint() -> None:
    candidates = _pre_commit_check._stale_path_lint_candidates_from_lines(
        [
            "docs\\guide.md",
            "scripts/runtime.py",
            "deploy/run.ps1",
            "assets/chart.png",
            "state/cache.parquet",
        ]
    )

    assert candidates == [
        "docs/guide.md",
        "scripts/runtime.py",
        "deploy/run.ps1",
    ]


def test_pre_commit_secret_audit_scans_all_staged_paths() -> None:
    candidates = _pre_commit_check._secret_audit_candidates_from_lines(
        [
            "docs\\guide.md",
            "assets\\chart.png",
            "config\\settings.json",
        ]
    )

    assert candidates == [
        "docs/guide.md",
        "assets/chart.png",
        "config/settings.json",
    ]


def test_pre_commit_surfaces_docstring_ratchet_advisory() -> None:
    specs = _pre_commit_check._advisory_audit_specs()

    assert (
        "docstring-ratchet",
        "scripts/_docstring_audit.py",
        ["--no-update", "--max-show", "3"],
    ) in specs


def test_pre_commit_runs_deferral_advisory_in_strict_mode() -> None:
    specs = _pre_commit_check._advisory_audit_specs()

    assert (
        "deferral-criteria",
        "scripts/_audit_deferral_criteria.py",
        ["--strict"],
    ) in specs


def test_pre_commit_blocks_timestamped_docs_runtime_snapshots() -> None:
    forbidden = _pre_commit_check._forbidden_staged_files_from_lines(
        [
            "docs/btc_live/btc_live_paperfallback_20260417T201221Z.json",
            "docs/btc_live/ecosystem/btc_audit_20260422T142220Z.json",
            "docs/broker_connections/preflight_20260422T142220Z.json",
            "docs/btc_live/btc_live_latest.json",
        ]
    )

    assert forbidden == [
        "docs/btc_live/btc_live_paperfallback_20260417T201221Z.json",
        "docs/btc_live/ecosystem/btc_audit_20260422T142220Z.json",
        "docs/broker_connections/preflight_20260422T142220Z.json",
    ]
