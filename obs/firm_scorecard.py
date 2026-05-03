from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from eta_engine.scripts import workspace_roots

SCORE_VERSION = "1.0.0"

SCORE_WEIGHTS: dict[str, float] = {
    "sharpe_calmar": 0.30,
    "autonomy_ratio": 0.15,
    "kaizen_improvement": 0.10,
    "regime_adaptation": 0.10,
    "fault_tolerance": 0.10,
    "quantum_edge": 0.05,
    "test_coverage": 0.05,
    "ops_maturity": 0.05,
    "regulatory_posture": 0.10,
}


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_jsonl_tail(path: Path, n: int = 200) -> list[dict]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        lines = [ln for ln in text.strip().split("\n") if ln.strip()]
        records = []
        for line in lines[-n:]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    except OSError:
        return []


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _py_module_count(root: Path | None, subdir: str) -> int:
    target = (root / subdir) if root else None
    if not target or not target.exists():
        return 0
    return len(list(target.rglob("*.py")))


def _test_count(root: Path | None) -> int:
    if not root or not root.exists():
        return 0
    return len(list(root.rglob("test_*.py"))) + len(list(root.rglob("*_test.py")))


def _compute_sharpe_calmar(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    strategies = _py_module_count(engine, "strategies")
    backtest_files = _py_module_count(engine / "backtest", "")
    walk_forward_files = _py_module_count(engine / "backtest" / "walk_forward", "")

    dash = _read_json(state_dir / "dashboard_payload.json")
    equity = dash.get("equity", {}) if dash else {}
    equity_curve = equity if isinstance(equity, dict) else {}
    pnl_values = [v for v in equity_curve.values() if isinstance(v, (int, float))]
    sharpe = calmar = 0.0
    if len(pnl_values) >= 20:
        returns = []
        for i in range(1, len(pnl_values)):
            prev = pnl_values[i - 1]
            if prev != 0:
                returns.append((pnl_values[i] - prev) / abs(prev))
        if returns:
            avg_r = sum(returns) / len(returns)
            std_r = (sum((r - avg_r) ** 2 for r in returns) / len(returns)) ** 0.5
            sharpe = (avg_r / std_r) * (252 ** 0.5) if std_r > 0 else 0.0
            peak = max(pnl_values)
            trough = min(pnl_values)
            drawdown = (peak - trough) / peak if peak > 0 else 0.0
            calmar = sharpe / drawdown if drawdown > 0 else sharpe

    details["strategies"] = strategies
    details["backtest_harness"] = backtest_files > 0
    details["walk_forward"] = walk_forward_files > 0
    details["equity_samples"] = len(pnl_values)
    if len(pnl_values) >= 20:
        details["sharpe_ratio"] = round(sharpe, 4)
        details["calmar_ratio"] = round(calmar, 4)
        details["data_source"] = "live"
    else:
        details["data_source"] = "declared_capability"

    wf_results_path = workspace_roots.BACKTEST_RUNS_ROOT / "walk_forward_summary.json"
    if wf_results_path.exists():
        wf = _read_json(wf_results_path)
        details["wf_aggregate_oos_sharpe"] = wf.get("agg_oos_sharpe")
        details["wf_dsr_pass_pct"] = wf.get("dsr_pass_pct")
        details["wf_bars"] = wf.get("total_bars")

    base = 0.0
    if strategies >= 20:
        base = 7.0
    elif strategies >= 10:
        base = 6.0
    elif strategies >= 5:
        base = 5.0
    else:
        base = 3.0
    if backtest_files > 0:
        base += 0.5
    if walk_forward_files > 0:
        base += 0.5
    if wf_results_path.exists() and wf.get("agg_oos_sharpe", 0) > 0.5:
        base += 1.0
    if len(pnl_values) >= 20:
        if sharpe > 1.5:
            base += 2.0
        elif sharpe > 1.0:
            base += 1.5
        elif sharpe > 0.5:
            base += 1.0
    score = min(base, 10.0)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["sharpe_calmar"], "details": details}


def _compute_autonomy_ratio(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    jarvis_modules = _py_module_count(engine / "brain" / "jarvis_v3", "")
    firm_board = _py_module_count(engine / "brain" / "firm_board", "")
    agent_modules = _py_module_count(engine / "brain", "") if (engine / "brain").exists() else 0
    decisions = _read_jsonl_tail(state_dir / "jarvis_audit.jsonl", 500)
    if not decisions:
        decisions = _read_jsonl_tail(runtime_state_dir / "decision_journal.jsonl", 500)

    total = len(decisions)
    auto_count = sum(1 for d in decisions if d.get("auto_approved") or d.get("approved"))
    ratio = auto_count / total if total > 0 else 0.0
    details["jarvis_modules"] = jarvis_modules
    details["firm_board_modules"] = firm_board
    details["agent_modules"] = agent_modules
    details["total_decisions"] = total
    details["auto_approved"] = auto_count
    if total > 0:
        details["autonomy_ratio"] = round(ratio, 4)
        details["data_source"] = "live"
    else:
        details["data_source"] = "declared_capability"

    base = 0.0
    if jarvis_modules >= 50:
        base = 8.0
    elif jarvis_modules >= 20:
        base = 7.0
    elif jarvis_modules >= 10:
        base = 6.0
    elif jarvis_modules >= 5:
        base = 5.0
    else:
        base = 3.0
    if firm_board > 0:
        base += 1.0
    if agent_modules > 30:
        base += 0.5
    if total > 0:
        if ratio >= 0.85:
            base += 1.5
        elif ratio >= 0.70:
            base += 1.0
        elif ratio >= 0.50:
            base += 0.5
    score = min(base, 10.0)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["autonomy_ratio"], "details": details}


def _compute_kaizen_improvement(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    kaizen_dir = engine / "brain" / "jarvis_v3"
    kaizen_files = len(list(kaizen_dir.rglob("kaizen*")))

    kaizen = _read_json(state_dir / "kaizen_ledger.json")
    if not kaizen:
        kp = runtime_state_dir / "kaizen_ledger.json"
        if kp.exists():
            kaizen = _read_json(kp)
    tickets = kaizen.get("tickets", []) if kaizen else []
    shipped = [t for t in tickets if t.get("status") == "SHIPPED"]
    open_tickets = [t for t in tickets if t.get("status") == "OPEN"]
    details["kaizen_modules"] = kaizen_files
    details["tickets_total"] = len(tickets)
    details["tickets_shipped"] = len(shipped)
    details["tickets_open"] = len(open_tickets)
    if tickets:
        details["shipping_rate"] = round(len(shipped) / len(tickets), 4)
        details["data_source"] = "live"
    else:
        details["data_source"] = "declared_capability"

    base = 0.0
    if kaizen_files >= 5:
        base = 7.0
    elif kaizen_files >= 3:
        base = 6.0
    elif kaizen_files >= 1:
        base = 5.0
    else:
        base = 2.0
    if tickets:
        rate = len(shipped) / len(tickets)
        if rate >= 0.6:
            base += 2.0
        elif rate >= 0.4:
            base += 1.5
        elif rate >= 0.2:
            base += 1.0
        elif rate > 0:
            base += 0.5
    score = min(base, 10.0)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["kaizen_improvement"], "details": details}


def _compute_regime_adaptation(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    ml_files = len(list(engine.rglob("*regime*"))) + len(list(engine.rglob("*hmm*")))
    hmm_exists = len(list(engine.rglob("*hmm*"))) > 0
    online_learn_exists = len(list(engine.rglob("*online_learn*"))) > 0
    corr_regime_exists = len(list(engine.rglob("*corr_regime*"))) > 0
    regime_stress_exists = len(list(engine.rglob("*regime_stress*"))) > 0

    hb = _read_json(state_dir / "avengers_heartbeat.json")
    hb_health = hb.get("health", "") if hb else ""
    dash = _read_json(state_dir / "dashboard_payload.json")
    stress = dash.get("stress", {}) if dash else {}
    composite = stress.get("composite", 0.5) if isinstance(stress, dict) else 0.5

    details["ml_source_files"] = ml_files
    details["hmm_exists"] = hmm_exists
    details["online_learning"] = online_learn_exists
    details["corr_regime_detector"] = corr_regime_exists
    details["regime_stress_tracker"] = regime_stress_exists
    details["heartbeat_health"] = hb_health or "unknown"
    details["stress_composite"] = composite

    base = 0.0
    if ml_files >= 8:
        base = 7.0
    elif ml_files >= 4:
        base = 6.0
    elif ml_files >= 1:
        base = 5.0
    else:
        base = 3.0
    if hmm_exists:
        base += 0.5
    if online_learn_exists:
        base += 0.5
    if corr_regime_exists:
        base += 0.5
    if regime_stress_exists:
        base += 0.5
    if composite < 0.3 and hb_health:
        base += 1.0
    elif composite < 0.5:
        base += 0.5
    score = min(base, 10.0)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["regime_adaptation"], "details": details}


def _compute_fault_tolerance(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    kill_switch_code = (
        len(list(engine.rglob("*kill*switch*"))) + len(list(engine.rglob("*circuit*breaker*"))) > 0
    )
    failover_code = len(list(engine.rglob("*failover*"))) > 0
    breaker_path = state_dir / "breaker.json"
    sentinel_path = state_dir / "operator.sentinel"
    has_breaker = breaker_path.exists()
    has_sentinel = sentinel_path.exists()

    decisions = _read_jsonl_tail(state_dir / "jarvis_audit.jsonl", 300)
    kill_switches = sum(1 for d in decisions if "kill" in str(d.get("action", "")).lower())
    venue_failovers = sum(
        1 for d in decisions
        if "failover" in str(d.get("action", "")).lower()
        or "venue" in str(d.get("subsystem", "")).lower()
    )

    details["kill_switch_code"] = kill_switch_code
    details["failover_code"] = failover_code
    details["breaker_active"] = has_breaker
    details["sentinel_active"] = has_sentinel
    details["kill_switch_triggers"] = kill_switches
    details["venue_failovers"] = venue_failovers

    base = 0.0
    if kill_switch_code and failover_code:
        base = 7.0
    elif kill_switch_code:
        base = 6.0
    else:
        base = 4.0
    if has_breaker:
        base += 0.5
    if has_sentinel:
        base += 0.5
    if kill_switches == 0 and not decisions:
        base += 1.0
    elif kill_switches == 0:
        base += 1.5
    if venue_failovers == 0:
        base += 0.5
    score = min(base, 10.0)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["fault_tolerance"], "details": details}


def _compute_quantum_edge(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    quantum_dir = engine / "brain" / "jarvis_v3" / "quantum"
    q_files = list(quantum_dir.rglob("*.py")) if quantum_dir.exists() else []
    q_test_files = _test_count(quantum_dir) if quantum_dir.exists() else 0
    has_dwave = len(list(quantum_dir.rglob("*dwave*"))) > 0 if quantum_dir.exists() else False
    has_qiskit = len(list(quantum_dir.rglob("*qiskit*"))) > 0 if quantum_dir.exists() else False
    has_pennylane = len(list(quantum_dir.rglob("*pennylane*"))) > 0 if quantum_dir.exists() else False
    qubo_types = len(list(quantum_dir.rglob("*qubo*"))) if quantum_dir.exists() else 0

    results_path = runtime_state_dir / "quantum_results.json"
    results = _read_json(results_path) if results_path.exists() else {}
    invocations = results.get("total_invocations", 0) if results else 0
    details["quantum_modules"] = len(q_files)
    details["quantum_tests"] = q_test_files
    details["dwave"] = has_dwave
    details["qiskit"] = has_qiskit
    details["pennylane"] = has_pennylane
    details["qubo_problem_types"] = qubo_types
    details["total_invocations"] = invocations

    if not q_files:
        details["score_10"] = 0.0
        return {"score": 0.0, "weight": SCORE_WEIGHTS["quantum_edge"], "details": details}

    base = 6.0
    if has_dwave:
        base += 0.5
    if has_qiskit:
        base += 0.5
    if has_pennylane:
        base += 0.5
    if qubo_types >= 3:
        base += 1.0
    if q_test_files >= 10:
        base += 1.0
    if invocations > 100:
        base += 1.0
    elif invocations > 10:
        base += 0.5
    score = min(base, 10.0)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["quantum_edge"], "details": details}


def _compute_test_coverage(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    test_files = _test_count(engine / "tests") if (engine / "tests").exists() else 0
    test_files += _test_count(runtime_state_dir.parent.parent / "tests")
    py_files = len(list(engine.rglob("*.py"))) if engine.exists() else 0
    details["test_files_found"] = test_files
    details["source_files"] = py_files
    ratio = test_files / max(py_files, 1)
    details["test_to_source_ratio"] = round(ratio, 4)

    if test_files >= 300:
        score = 9.0
    elif test_files >= 150:
        score = 8.0
    elif test_files >= 75:
        score = 7.0
    elif test_files >= 30:
        score = 6.0
    elif test_files >= 10:
        score = 5.0
    elif test_files >= 5:
        score = 4.0
    elif test_files >= 1:
        score = 3.0
    else:
        score = 1.0
    if ratio > 0.15:
        score = min(score + 1, 10)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["test_coverage"], "details": details}


def _compute_ops_maturity(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    root = workspace_roots.WORKSPACE_ROOT
    marker_paths = [
        ("vps_bootstrap", root / "eta_engine" / "deploy" / "vps_bootstrap.ps1"),
        ("systemd", root / "eta_engine" / "deploy" / "systemd"),
        ("dockerfile", root / "eta_engine" / "deploy" / "Dockerfile"),
        ("prometheus", root / "eta_engine" / "obs" / "prometheus_exporter.py"),
        ("cloudflare_tunnel", root / "eta_engine" / "deploy" / "configs" / "process-compose.yaml"),
        ("grafana", root / "eta_engine" / "obs" / "grafana_dashboard.py"),
        ("health_endpoint", None),
        ("metrics_endpoint", None),
    ]
    found_names = []
    for name, p in marker_paths:
        if p is None or p.exists():
            found_names.append(name)
    found = len(found_names)
    total = len(marker_paths)
    details["deployment_markers_found"] = found
    details["deployment_markers_total"] = total
    details["markers"] = found_names

    readiness = _read_json(runtime_state_dir / "bot_strategy_readiness_latest.json")
    has_readiness = bool(readiness)
    details["strategy_readiness"] = has_readiness

    vps_script = root / "scripts" / "vps_bootstrap.ps1"
    has_vps = vps_script.exists()
    details["vps_bootstrap_script"] = has_vps

    score = min(found * 1.2 + (1 if has_readiness else 0) + (1 if has_vps else 0), 10)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["ops_maturity"], "details": details}


def _compute_regulatory_posture(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    root = workspace_roots.WORKSPACE_ROOT
    audit = _read_json(state_dir / "nightly_audit.json")
    has_audit = bool(audit)
    decisions_count = len(_read_jsonl_tail(state_dir / "jarvis_audit.jsonl", 100))
    journal = _read_jsonl_tail(state_dir / "decision_journal.jsonl", 50)
    has_journal = bool(journal) or (state_dir / "decision_journal.jsonl").exists()

    compliance_docs = (
        list(root.rglob("COMPLIANCE*"))
        + list(root.rglob("compliance*"))
        + list(root.rglob("*audit*"))
    )
    llc_docs = list(root.rglob("*LLC*")) + list(root.rglob("*llc*"))
    details["nightly_audit_exists"] = has_audit
    details["audit_log_entries"] = decisions_count
    details["decision_journal"] = has_journal
    details["compliance_documents"] = len(compliance_docs)
    details["llc_references"] = len(llc_docs)

    base = 5.0
    if has_audit:
        base += 1.0
    if has_journal:
        base += 1.0
    if len(compliance_docs) > 0:
        base += 1.0
    if len(llc_docs) > 0:
        base += 1.0
    if decisions_count >= 50:
        base += 1.0
    elif decisions_count >= 10:
        base += 0.5
    score = min(base, 10.0)
    details["score_10"] = round(score, 2)
    return {"score": round(score, 2), "weight": SCORE_WEIGHTS["regulatory_posture"], "details": details}


COMPUTERS: dict[str, callable] = {
    "sharpe_calmar": _compute_sharpe_calmar,
    "autonomy_ratio": _compute_autonomy_ratio,
    "kaizen_improvement": _compute_kaizen_improvement,
    "regime_adaptation": _compute_regime_adaptation,
    "fault_tolerance": _compute_fault_tolerance,
    "quantum_edge": _compute_quantum_edge,
    "test_coverage": _compute_test_coverage,
    "ops_maturity": _compute_ops_maturity,
    "regulatory_posture": _compute_regulatory_posture,
}


def build_scorecard(
    state_dir: Path | None = None,
    runtime_state_dir: Path | None = None,
) -> dict:
    sdir = state_dir or workspace_roots.ETA_ENGINE_ROOT / "state"
    rdir = runtime_state_dir or workspace_roots.ETA_RUNTIME_STATE_DIR
    categories: dict[str, dict] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for name, computer in COMPUTERS.items():
        result = computer(sdir, rdir)
        categories[name] = result
        weighted_sum += result["score"] * result["weight"]
        weight_total += result["weight"]
    composite = round(weighted_sum / weight_total, 2) if weight_total > 0 else 0.0
    grade = "F"
    if composite >= 9.0:
        grade = "S"
    elif composite >= 8.0:
        grade = "A"
    elif composite >= 7.0:
        grade = "B"
    elif composite >= 6.0:
        grade = "C"
    elif composite >= 5.0:
        grade = "D"
    elif composite >= 4.0:
        grade = "E"
    return {
        "schema_version": SCORE_VERSION,
        "generated_at": _now_iso(),
        "source": "firm_scorecard",
        "status": "ready",
        "composite_score": composite,
        "grade": grade,
        "categories": categories,
        "weights": dict(SCORE_WEIGHTS),
        "summary": {
            "composite_score": composite,
            "grade": grade,
            "category_count": len(categories),
            "top_strength": max(categories, key=lambda k: categories[k]["score"]),
            "top_weakness": min(categories, key=lambda k: categories[k]["score"]),
        },
    }


def write_scorecard(
    scorecard: dict,
    path: Path = workspace_roots.ETA_RUNTIME_STATE_DIR / "firm_scorecard_latest.json",
) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(scorecard, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
