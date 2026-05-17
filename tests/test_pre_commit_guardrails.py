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


def test_pre_commit_surfaces_broker_dormancy_advisory() -> None:
    specs = _pre_commit_check._advisory_audit_specs()

    assert (
        "broker-dormancy",
        "scripts/_audit_dormancy_consistency.py",
        ["--strict"],
    ) in specs


def test_pre_commit_blocks_timestamped_docs_runtime_snapshots() -> None:
    forbidden = _pre_commit_check._forbidden_staged_files_from_lines(
        [
            "docs/btc_live/btc_live_paperfallback_20260417T201221Z.json",
            "docs/btc_live/ecosystem/btc_audit_20260422T142220Z.json",
            "docs/broker_connections/preflight_20260422T142220Z.json",
            "docs/broker_connections/broker_connections_latest.json",
            "docs/broker_connections/preflight_venue_connections_latest.json",
            "docs/premarket_latest.json",
            "docs/premarket_latest.txt",
            "docs/premarket_log.jsonl",
            "docs/monthly_review_latest.json",
            "docs/monthly_review_latest.txt",
            "docs/monthly_review_2026_04.json",
            "docs/monthly_review_2026_04.txt",
            "docs/weekly_review_log.json",
            "docs/weekly_review_latest.json",
            "docs/weekly_review_latest.txt",
            "docs/weekly_checklist_latest.json",
            "docs/weekly_checklist_latest.txt",
            "docs/weekly_checklist_template.json",
            "docs/btc_paper/btc_paper_run_latest.json",
            "docs/btc_paper/btc_paper_journal.jsonl",
            "docs/btc_inventory/btc_artifact_inventory_20260422T142220Z.json",
            "docs/btc_inventory/btc_artifact_inventory_audit_20260424T041200Z.json",
            "docs/btc_live/btc_live_latest.json",
            "docs/btc_live/btc_live_gate_decision.json",
            "docs/btc_live/btc_live_decisions.jsonl",
            "docs/btc_live/broker_fleet/btc_broker_fleet_latest.json",
            "docs/btc_live/ecosystem/btc_dashboard_latest.json",
            "docs/btc_live/control/btc_command_center_control_latest.json",
            "docs/btc_live/control/btc_command_center_events.jsonl",
            "docs/btc_live/broker_connections/btc_launch_broker_connections_20260422T142220Z.json",
        ]
    )

    assert forbidden == [
        "docs/btc_live/btc_live_paperfallback_20260417T201221Z.json",
        "docs/btc_live/ecosystem/btc_audit_20260422T142220Z.json",
        "docs/broker_connections/preflight_20260422T142220Z.json",
        "docs/broker_connections/broker_connections_latest.json",
        "docs/broker_connections/preflight_venue_connections_latest.json",
        "docs/premarket_latest.json",
        "docs/premarket_latest.txt",
        "docs/premarket_log.jsonl",
        "docs/monthly_review_latest.json",
        "docs/monthly_review_latest.txt",
        "docs/monthly_review_2026_04.json",
        "docs/monthly_review_2026_04.txt",
        "docs/weekly_review_log.json",
        "docs/weekly_review_latest.json",
        "docs/weekly_review_latest.txt",
        "docs/weekly_checklist_latest.json",
        "docs/weekly_checklist_latest.txt",
        "docs/weekly_checklist_template.json",
        "docs/btc_paper/btc_paper_run_latest.json",
        "docs/btc_paper/btc_paper_journal.jsonl",
        "docs/btc_inventory/btc_artifact_inventory_20260422T142220Z.json",
        "docs/btc_inventory/btc_artifact_inventory_audit_20260424T041200Z.json",
        "docs/btc_live/btc_live_latest.json",
        "docs/btc_live/btc_live_gate_decision.json",
        "docs/btc_live/btc_live_decisions.jsonl",
        "docs/btc_live/broker_fleet/btc_broker_fleet_latest.json",
        "docs/btc_live/ecosystem/btc_dashboard_latest.json",
        "docs/btc_live/control/btc_command_center_control_latest.json",
        "docs/btc_live/control/btc_command_center_events.jsonl",
        "docs/btc_live/broker_connections/btc_launch_broker_connections_20260422T142220Z.json",
    ]


def test_pre_commit_allows_btc_historical_warning_readmes() -> None:
    forbidden = _pre_commit_check._forbidden_staged_files_from_lines(
        [
            "docs/btc_live/README.md",
            "docs/btc_live/broker_connections/README.md",
            "docs/btc_live/broker_fleet/README.md",
            "docs/btc_live/control/README.md",
            "docs/btc_live/ecosystem/README.md",
            "docs/btc_inventory/README.md",
            "docs/btc_paper/README.md",
            "docs/broker_connections/README.md",
        ]
    )

    assert forbidden == []


def test_pre_commit_guidance_calls_out_historical_btc_docs_snapshots() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/btc_live/btc_live_latest.json",
            "docs/btc_live/broker_fleet/btc_broker_fleet_latest.json",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
        "[pre-commit]   BTC docs snapshots are historical only; keep README warning markers, but write live BTC state under var/eta_engine/state/btc_live or var/eta_engine/state/broker_fleet.",
    ]


def test_pre_commit_guidance_calls_out_historical_btc_paper_docs_snapshots() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/btc_paper/btc_paper_run_20260422T013645Z.json",
            "docs/btc_paper/btc_paper_journal.jsonl",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
        "[pre-commit]   BTC paper docs snapshots are historical only; keep README warning markers, but write live BTC paper state under var/eta_engine/state/btc_paper or var/eta_engine/state/broker_fleet.",
    ]


def test_pre_commit_guidance_skips_btc_note_for_general_runtime_artifacts() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/decision_journal.jsonl",
            "docs/runtime_log.jsonl",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
    ]


def test_pre_commit_guidance_calls_out_generated_broker_connection_docs_snapshots() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/broker_connections/broker_connections_latest.json",
            "docs/broker_connections/preflight_venue_connections_20260424T024606Z.json",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
        "[pre-commit]   Broker connection docs snapshots are generated probe artifacts; keep README/docs changes only, leave *_latest.json or timestamped probe JSON unstaged, and write fresh reports under var/eta_engine/state/broker_connections.",
    ]


def test_pre_commit_guidance_calls_out_historical_premarket_docs_snapshots() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/premarket_latest.json",
            "docs/premarket_log.jsonl",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
        "[pre-commit]   Premarket docs snapshots are historical only; keep operator inputs under docs/premarket_inputs.json when needed, but write fresh premarket outputs under var/eta_engine/state/premarket.",
    ]


def test_pre_commit_guidance_calls_out_historical_monthly_review_docs_snapshots() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/monthly_review_latest.json",
            "docs/monthly_review_2026_04.txt",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
        "[pre-commit]   Monthly review docs snapshots are historical only; write fresh monthly review outputs under var/eta_engine/state/monthly_review.",
    ]


def test_pre_commit_guidance_calls_out_historical_weekly_review_docs_snapshots() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/weekly_review_latest.json",
            "docs/weekly_checklist_template.json",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
        "[pre-commit]   Weekly review and checklist docs snapshots are historical only; keep explicit operator answers JSON where you manage it, but write fresh weekly review outputs under var/eta_engine/state/weekly_review.",
    ]


def test_pre_commit_guidance_calls_out_historical_btc_inventory_docs_snapshots() -> None:
    guidance = _pre_commit_check._forbidden_staged_guidance_lines(
        [
            "docs/btc_inventory/btc_artifact_inventory_20260422T142220Z.json",
            "docs/btc_inventory/btc_artifact_inventory_audit_20260424T041200Z.json",
        ]
    )

    assert guidance == [
        "[pre-commit]   leave runtime journal/state files unstaged; canonical live writes belong under var/eta_engine/state or logs/eta_engine.",
        "[pre-commit]   BTC inventory docs are historical artifact catalogs; keep README/docs changes only, and leave checked-in inventory JSON or JSONL snapshots unstaged.",
    ]
