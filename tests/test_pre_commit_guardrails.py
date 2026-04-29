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
