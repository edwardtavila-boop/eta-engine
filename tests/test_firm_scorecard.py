from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.obs import firm_scorecard
from eta_engine.scripts import workspace_roots


def _write(path, text: str = "") -> None:  # type: ignore[no-untyped-def]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path, payload: dict) -> None:  # type: ignore[no-untyped-def]
    _write(path, json.dumps(payload))


def _write_jsonl(path, rows: list[dict]) -> None:  # type: ignore[no-untyped-def]
    _write(path, "\n".join(json.dumps(row) for row in rows) + "\n")


def _configure_workspace(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    root = tmp_path / "workspace"
    engine = root / "eta_engine"
    runtime = root / "var" / "eta_engine" / "state"
    backtest_runs = root / "mnq_backtest" / "runs"
    for path in (engine, runtime, backtest_runs):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(workspace_roots, "ETA_ENGINE_ROOT", engine)
    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_STATE_DIR", runtime)
    monkeypatch.setattr(workspace_roots, "BACKTEST_RUNS_ROOT", backtest_runs)
    return root, engine, runtime


def _seed_repo_shape(root, engine) -> None:  # type: ignore[no-untyped-def]
    for i in range(25):
        _write(engine / "strategies" / f"strategy_{i}.py", "VALUE = 1\n")

    _write(engine / "backtest" / "runner.py", "VALUE = 1\n")
    _write(engine / "backtest" / "walk_forward" / "runner.py", "VALUE = 1\n")

    for i in range(55):
        _write(engine / "brain" / "jarvis_v3" / f"module_{i}.py", "VALUE = 1\n")
    for i in range(6):
        _write(engine / "brain" / "jarvis_v3" / f"kaizen_{i}.py", "VALUE = 1\n")
    _write(engine / "brain" / "firm_board" / "board.py", "VALUE = 1\n")
    for name in (
        "regime_hmm.py",
        "online_learn.py",
        "corr_regime.py",
        "regime_stress.py",
        "kill_switch.py",
        "venue_failover.py",
    ):
        _write(engine / "brain" / name, "VALUE = 1\n")

    quantum_dir = engine / "brain" / "jarvis_v3" / "quantum"
    for name in (
        "dwave_solver.py",
        "qiskit_runner.py",
        "pennylane_bridge.py",
        "qubo_alpha.py",
        "qubo_beta.py",
        "qubo_gamma.py",
    ):
        _write(quantum_dir / name, "VALUE = 1\n")
    for i in range(12):
        _write(
            quantum_dir / f"test_quantum_{i}.py",
            "def test_placeholder():\n    assert True\n",
        )

    for i in range(32):
        _write(
            engine / "tests" / f"test_generated_{i}.py",
            "def test_placeholder():\n    assert True\n",
        )

    _write(root / "COMPLIANCE.md", "# compliance\n")
    _write(root / "AcmeLLC.md", "# llc\n")
    _write(engine / "deploy" / "vps_bootstrap.ps1", "Write-Output 'bootstrap'\n")
    (engine / "deploy" / "systemd").mkdir(parents=True, exist_ok=True)
    _write(engine / "deploy" / "Dockerfile", "FROM scratch\n")
    _write(engine / "deploy" / "configs" / "process-compose.yaml", "services: {}\n")
    _write(engine / "obs" / "prometheus_exporter.py", "VALUE = 1\n")
    _write(engine / "obs" / "grafana_dashboard.py", "VALUE = 1\n")
    _write(root / "scripts" / "vps_bootstrap.ps1", "Write-Output 'root bootstrap'\n")


def _equity_curve(samples: int = 25) -> dict[str, float]:
    values: dict[str, float] = {}
    equity = 1000.0
    for i in range(samples):
        equity += 12.0 + (3.0 if i % 2 == 0 else -1.5)
        values[str(i)] = round(equity, 2)
    return values


def _decision_rows(count: int, *, auto_approved: bool = True) -> list[dict]:
    return [
        {
            "auto_approved": auto_approved,
            "approved": auto_approved,
            "action": "trade_submit",
            "subsystem": "ibkr",
        }
        for _ in range(count)
    ]


def _write_runtime_audit_dir(path, rows: list[dict]) -> None:  # type: ignore[no-untyped-def]
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    _write_jsonl(path / "jarvis_audit" / f"{today}.jsonl", rows)


def _write_launch_readiness(
    path,
    *,
    verdict: str,
    no_go_gates: list[str] | None = None,
    hold_gates: list[str] | None = None,
):  # type: ignore[no-untyped-def]
    no_go_gates = no_go_gates or []
    hold_gates = hold_gates or []
    gates: list[dict[str, object]] = [
        {
            "name": "R0_LIVE_CAPITAL_CALENDAR",
            "status": "GO",
            "rationale": "calendar cleared",
            "detail": {"paper_live_required": False},
        },
        {
            "name": "R1_PROP_READY_DESIGNATED",
            "status": "GO",
            "rationale": "2 PROP_READY bots designated",
            "detail": {"n": 2},
        },
    ]
    gates.extend({"name": gate, "status": "NO_GO", "rationale": f"{gate} failing"} for gate in no_go_gates)
    gates.extend({"name": gate, "status": "HOLD", "rationale": f"{gate} warning"} for gate in hold_gates)
    _write_json(
        path,
        {
            "ts": "2026-07-08T00:00:00+00:00",
            "launch_date": "2026-07-08",
            "days_until_launch": 0,
            "overall_verdict": verdict,
            "summary": f"{verdict} launch readiness",
            "gates": gates,
        },
    )


def test_build_scorecard_defaults_to_canonical_runtime_state(monkeypatch, tmp_path) -> None:
    root, engine, runtime = _configure_workspace(monkeypatch, tmp_path)
    _seed_repo_shape(root, engine)

    legacy_state = engine / "state"
    _write_json(
        legacy_state / "dashboard_payload.json",
        {"equity": _equity_curve(), "stress": {"composite": 0.21}},
    )
    _write_jsonl(legacy_state / "jarvis_audit.jsonl", _decision_rows(40))

    scorecard = firm_scorecard.build_scorecard()

    sharpe = scorecard["categories"]["sharpe_calmar"]
    autonomy = scorecard["categories"]["autonomy_ratio"]

    assert sharpe["details"]["dashboard_source"] == "runtime"
    assert sharpe["details"]["equity_samples"] == 0
    assert sharpe["score"] <= 4.0

    assert autonomy["details"]["decision_source"] == "runtime"
    assert autonomy["details"]["total_decisions"] == 0
    assert autonomy["score"] <= 4.0

    assert scorecard["status"] == "degraded"
    assert scorecard["summary"]["runtime_evidence_categories"] == 0
    assert runtime.exists()


def test_repository_shape_alone_is_capped_without_live_evidence(monkeypatch, tmp_path) -> None:
    root, engine, _runtime = _configure_workspace(monkeypatch, tmp_path)
    _seed_repo_shape(root, engine)

    scorecard = firm_scorecard.build_scorecard()

    assert scorecard["status"] == "degraded"
    assert scorecard["summary"]["runtime_evidence_categories"] == 0
    assert scorecard["categories"]["sharpe_calmar"]["score"] <= 4.0
    assert scorecard["categories"]["autonomy_ratio"]["score"] <= 4.0
    assert scorecard["categories"]["kaizen_improvement"]["score"] <= 4.0
    assert scorecard["categories"]["quantum_edge"]["score"] <= 4.0
    assert scorecard["categories"]["ops_maturity"]["score"] <= 4.0
    assert scorecard["categories"]["regulatory_posture"]["score"] <= 4.0
    assert scorecard["categories"]["test_coverage"]["score"] <= 6.0
    assert scorecard["composite_score"] < 6.0


def test_live_runtime_evidence_unlocks_higher_scores(monkeypatch, tmp_path) -> None:
    root, engine, runtime = _configure_workspace(monkeypatch, tmp_path)
    _seed_repo_shape(root, engine)

    _write_json(
        root / "mnq_backtest" / "runs" / "walk_forward_summary.json",
        {"agg_oos_sharpe": 0.9, "dsr_pass_pct": 0.8, "total_bars": 5000},
    )
    _write_json(
        runtime / "dashboard_payload.json",
        {"equity": _equity_curve(), "stress": {"composite": 0.2}},
    )
    _write_runtime_audit_dir(runtime, _decision_rows(60))
    _write_jsonl(runtime / "decision_journal.jsonl", _decision_rows(60))
    _write_json(
        runtime / "kaizen_ledger.json",
        {
            "tickets": [
                {"status": "SHIPPED"},
                {"status": "SHIPPED"},
                {"status": "SHIPPED"},
                {"status": "SHIPPED"},
                {"status": "SHIPPED"},
                {"status": "SHIPPED"},
                {"status": "SHIPPED"},
                {"status": "OPEN"},
                {"status": "OPEN"},
                {"status": "OPEN"},
            ]
        },
    )
    _write_json(runtime / "avengers_heartbeat.json", {"health": "healthy"})
    _write_json(runtime / "breaker.json", {"active": False})
    _write(runtime / "operator.sentinel", "active\n")
    _write_json(runtime / "quantum_results.json", {"total_invocations": 120})
    _write_json(runtime / "bot_strategy_readiness_latest.json", {"rows": [{"bot_id": "mnq_futures"}]})
    _write_json(runtime / "fm_health.json", {"status": "ok", "service_count": 8})
    _write_json(runtime / "nightly_audit.json", {"completed": True})

    scorecard = firm_scorecard.build_scorecard()

    assert scorecard["status"] == "ready"
    assert scorecard["summary"]["runtime_evidence_categories"] >= 8
    assert scorecard["categories"]["sharpe_calmar"]["details"]["data_source"] == "runtime"
    assert scorecard["categories"]["sharpe_calmar"]["score"] > 4.0
    assert scorecard["categories"]["autonomy_ratio"]["score"] > 7.0
    assert scorecard["categories"]["kaizen_improvement"]["score"] > 6.0
    assert scorecard["categories"]["quantum_edge"]["score"] > 6.0
    assert scorecard["categories"]["ops_maturity"]["score"] > 6.0
    assert scorecard["categories"]["regulatory_posture"]["score"] > 7.0
    assert scorecard["composite_score"] > 7.0


def test_scorecard_reads_canonical_daily_audit_dir(monkeypatch, tmp_path) -> None:
    root, engine, runtime = _configure_workspace(monkeypatch, tmp_path)
    _seed_repo_shape(root, engine)
    _write_runtime_audit_dir(runtime, _decision_rows(12))

    scorecard = firm_scorecard.build_scorecard()

    autonomy = scorecard["categories"]["autonomy_ratio"]
    regulatory = scorecard["categories"]["regulatory_posture"]
    fault_tolerance = scorecard["categories"]["fault_tolerance"]

    assert autonomy["details"]["decision_source"] == "runtime"
    assert autonomy["details"]["total_decisions"] == 12
    assert regulatory["details"]["audit_log_source"] == "runtime"
    assert regulatory["details"]["audit_log_entries"] == 12
    assert fault_tolerance["details"]["decision_source"] == "runtime"
    assert fault_tolerance["details"]["decision_count"] == 12


def test_launch_readiness_no_go_caps_composite_and_status(monkeypatch, tmp_path) -> None:
    root, engine, runtime = _configure_workspace(monkeypatch, tmp_path)
    _seed_repo_shape(root, engine)

    _write_json(
        runtime / "dashboard_payload.json",
        {"equity": _equity_curve(), "stress": {"composite": 0.2}},
    )
    _write_runtime_audit_dir(runtime, _decision_rows(60))
    _write_jsonl(runtime / "decision_journal.jsonl", _decision_rows(60))
    _write_json(runtime / "kaizen_ledger.json", {"tickets": [{"status": "SHIPPED"} for _ in range(10)]})
    _write_json(runtime / "avengers_heartbeat.json", {"health": "healthy"})
    _write_json(runtime / "quantum_results.json", {"total_invocations": 120})
    _write_json(runtime / "bot_strategy_readiness_latest.json", {"rows": [{"bot_id": "mnq_futures"}]})
    _write_json(runtime / "fm_health.json", {"status": "ok", "service_count": 8})
    _write_json(runtime / "nightly_audit.json", {"completed": True})
    _write_launch_readiness(
        runtime / "diamond_prop_launch_readiness_latest.json",
        verdict="NO_GO",
        no_go_gates=["R4_SIZING_NOT_BREACHED"],
    )

    scorecard = firm_scorecard.build_scorecard()

    assert scorecard["status"] == "limited"
    assert scorecard["composite_score"] <= 5.5
    assert scorecard["summary"]["launch_readiness_verdict"] == "no_go"
    assert scorecard["summary"]["launch_readiness_non_calendar_no_go_gates"] == ["R4_SIZING_NOT_BREACHED"]
    assert scorecard["summary"]["composite_score_cap_reason"].startswith("Launch readiness still has hard")


def test_calendar_only_hold_does_not_cap_strong_scorecard(monkeypatch, tmp_path) -> None:
    root, engine, runtime = _configure_workspace(monkeypatch, tmp_path)
    _seed_repo_shape(root, engine)

    _write_json(
        runtime / "dashboard_payload.json",
        {"equity": _equity_curve(), "stress": {"composite": 0.2}},
    )
    _write_runtime_audit_dir(runtime, _decision_rows(60))
    _write_jsonl(runtime / "decision_journal.jsonl", _decision_rows(60))
    _write_json(runtime / "kaizen_ledger.json", {"tickets": [{"status": "SHIPPED"} for _ in range(10)]})
    _write_json(runtime / "avengers_heartbeat.json", {"health": "healthy"})
    _write_json(runtime / "quantum_results.json", {"total_invocations": 120})
    _write_json(runtime / "bot_strategy_readiness_latest.json", {"rows": [{"bot_id": "mnq_futures"}]})
    _write_json(runtime / "fm_health.json", {"status": "ok", "service_count": 8})
    _write_json(runtime / "nightly_audit.json", {"completed": True})
    _write_json(
        runtime / "diamond_prop_launch_readiness_latest.json",
        {
            "ts": "2026-07-07T00:00:00+00:00",
            "launch_date": "2026-07-08",
            "days_until_launch": 1,
            "overall_verdict": "HOLD",
            "summary": "calendar hold only",
            "gates": [
                {
                    "name": "R0_LIVE_CAPITAL_CALENDAR",
                    "status": "HOLD",
                    "rationale": "wait until tomorrow",
                    "detail": {"paper_live_required": True},
                },
                {
                    "name": "R1_PROP_READY_DESIGNATED",
                    "status": "GO",
                    "rationale": "2 PROP_READY bots designated",
                    "detail": {"n": 2},
                },
            ],
        },
    )

    scorecard = firm_scorecard.build_scorecard()

    assert scorecard["status"] == "ready"
    assert scorecard["composite_score"] > 7.0
    assert scorecard["summary"]["launch_readiness_calendar_hold"] is True
    assert scorecard["summary"]["launch_readiness_non_calendar_no_go_gates"] == []
