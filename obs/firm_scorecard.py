from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from eta_engine.scripts import workspace_roots

SCORE_VERSION = "1.2.0"

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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_with_status(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, "invalid"
    if isinstance(payload, dict):
        return payload, "ok"
    return {}, "invalid"


def _read_jsonl_tail(path: Path, n: int = 200) -> list[dict[str, Any]]:
    records, _ = _read_jsonl_tail_with_status(path, n)
    return records


def _read_jsonl_tail_with_status(path: Path, n: int = 200) -> tuple[list[dict[str, Any]], str]:
    if not path.exists():
        return [], "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], "invalid"

    lines = [ln for ln in text.strip().split("\n") if ln.strip()]
    records: list[dict[str, Any]] = []
    for line in lines[-n:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records, "ok"


def _state_jsonl_candidate_paths(relative_path: str, base_dir: Path) -> list[Path]:
    if relative_path != "jarvis_audit.jsonl":
        return [base_dir / relative_path]

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return [
        base_dir / "jarvis_audit" / f"{today}.jsonl",
        base_dir / relative_path,
    ]


def _load_state_json(
    relative_path: str,
    state_dir: Path,
    runtime_state_dir: Path,
) -> tuple[dict[str, Any], str, str]:
    runtime_path = runtime_state_dir / relative_path
    runtime_payload, runtime_status = _read_json_with_status(runtime_path)
    if runtime_status != "missing" or state_dir == runtime_state_dir:
        return runtime_payload, "runtime", runtime_status

    override_path = state_dir / relative_path
    override_payload, override_status = _read_json_with_status(override_path)
    if override_status != "missing":
        return override_payload, "state_override", override_status
    return {}, "missing", "missing"


def _load_state_jsonl(
    relative_path: str,
    state_dir: Path,
    runtime_state_dir: Path,
    n: int = 200,
) -> tuple[list[dict[str, Any]], str, str]:
    for runtime_path in _state_jsonl_candidate_paths(relative_path, runtime_state_dir):
        runtime_payload, runtime_status = _read_jsonl_tail_with_status(runtime_path, n)
        if runtime_status != "missing":
            return runtime_payload, "runtime", runtime_status
    if state_dir == runtime_state_dir:
        return [], "runtime", "missing"

    for override_path in _state_jsonl_candidate_paths(relative_path, state_dir):
        override_payload, override_status = _read_jsonl_tail_with_status(override_path, n)
        if override_status != "missing":
            return override_payload, "state_override", override_status
    return [], "missing", "missing"


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


def _primary_source(*sources: str, fallback: str) -> str:
    for source in sources:
        if source != "missing":
            return source
    return fallback


def _grade_from_composite(composite: float) -> str:
    if composite >= 9.0:
        return "S"
    if composite >= 8.0:
        return "A"
    if composite >= 7.0:
        return "B"
    if composite >= 6.0:
        return "C"
    if composite >= 5.0:
        return "D"
    if composite >= 4.0:
        return "E"
    return "F"


def _launch_readiness_overlay(
    state_dir: Path,
    runtime_state_dir: Path,
) -> tuple[dict[str, Any], float | None, str | None, str | None]:
    """Return scorecard summary fields plus any launch-truth composite cap."""
    receipt, source, status = _load_state_json(
        "diamond_prop_launch_readiness_latest.json",
        state_dir,
        runtime_state_dir,
    )
    summary: dict[str, Any] = {
        "launch_readiness_source": source,
        "launch_readiness_status": status,
        "launch_readiness_verdict": "missing",
    }
    if not receipt:
        return summary, None, None, None

    verdict = str(receipt.get("overall_verdict") or "UNKNOWN").upper()
    gates = receipt.get("gates")
    if not isinstance(gates, list):
        gates = []

    non_calendar_no_go: list[str] = []
    non_calendar_hold: list[str] = []
    calendar_hold = False
    prop_ready_count: int | None = None
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        name = str(gate.get("name") or "")
        gate_status = str(gate.get("status") or "").upper()
        detail = gate.get("detail") if isinstance(gate.get("detail"), dict) else {}
        if name == "R1_PROP_READY_DESIGNATED" and prop_ready_count is None:
            n_value = detail.get("n")
            if isinstance(n_value, int):
                prop_ready_count = n_value
        if name == "R0_LIVE_CAPITAL_CALENDAR" and gate_status == "HOLD":
            calendar_hold = True
            continue
        if gate_status == "NO_GO":
            non_calendar_no_go.append(name)
        elif gate_status == "HOLD":
            non_calendar_hold.append(name)

    summary.update(
        {
            "launch_readiness_verdict": verdict.lower(),
            "launch_readiness_launch_date": receipt.get("launch_date"),
            "launch_readiness_days_until_launch": receipt.get("days_until_launch"),
            "launch_readiness_summary_line": receipt.get("summary", ""),
            "launch_readiness_non_calendar_no_go_gates": non_calendar_no_go,
            "launch_readiness_non_calendar_hold_gates": non_calendar_hold,
            "launch_readiness_calendar_hold": calendar_hold,
        }
    )
    if prop_ready_count is not None:
        summary["launch_readiness_prop_ready_count"] = prop_ready_count

    if non_calendar_no_go:
        return (
            summary,
            5.5,
            "Launch readiness still has hard non-calendar blockers; the firm is not launch-ready.",
            "limited",
        )
    if non_calendar_hold:
        return (
            summary,
            6.5,
            "Launch readiness still has soft non-calendar blockers; the firm should not score as fully ready.",
            "limited",
        )
    return summary, None, None, None


def _finalize_result(
    category: str,
    score: float,
    details: dict[str, Any],
    *,
    data_source: str,
    runtime_evidence: bool,
    score_cap: float | None = None,
    cap_reason: str | None = None,
) -> dict[str, Any]:
    final_score = min(max(score, 0.0), 10.0)
    if score_cap is not None and final_score > score_cap:
        details["uncapped_score_10"] = round(final_score, 2)
        details["score_cap_10"] = round(score_cap, 2)
        details["score_cap_reason"] = cap_reason or "Runtime evidence was insufficient for the uncapped score."
        final_score = score_cap
    details["data_source"] = data_source
    details["runtime_evidence"] = runtime_evidence
    details["score_10"] = round(final_score, 2)
    return {"score": round(final_score, 2), "weight": SCORE_WEIGHTS[category], "details": details}


def _compute_sharpe_calmar(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    strategies = _py_module_count(engine, "strategies")
    backtest_files = _py_module_count(engine / "backtest", "")
    walk_forward_files = _py_module_count(engine / "backtest" / "walk_forward", "")

    dash, dash_source, dash_status = _load_state_json("dashboard_payload.json", state_dir, runtime_state_dir)
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
            sharpe = (avg_r / std_r) * (252**0.5) if std_r > 0 else 0.0
            peak = max(pnl_values)
            trough = min(pnl_values)
            drawdown = (peak - trough) / peak if peak > 0 else 0.0
            calmar = sharpe / drawdown if drawdown > 0 else sharpe

    details["strategies"] = strategies
    details["backtest_harness"] = backtest_files > 0
    details["walk_forward"] = walk_forward_files > 0
    details["equity_samples"] = len(pnl_values)
    details["dashboard_source"] = dash_source
    details["dashboard_status"] = dash_status
    if len(pnl_values) >= 20:
        details["sharpe_ratio"] = round(sharpe, 4)
        details["calmar_ratio"] = round(calmar, 4)

    wf_results_path = workspace_roots.BACKTEST_RUNS_ROOT / "walk_forward_summary.json"
    wf: dict[str, Any] = {}
    if wf_results_path.exists():
        wf = _read_json(wf_results_path)
        details["wf_aggregate_oos_sharpe"] = wf.get("agg_oos_sharpe")
        details["wf_dsr_pass_pct"] = wf.get("dsr_pass_pct")
        details["wf_bars"] = wf.get("total_bars")

    score = 0.5
    if strategies >= 20:
        score = 2.5
    elif strategies >= 10:
        score = 2.0
    elif strategies >= 5:
        score = 1.5
    elif strategies > 0:
        score = 1.0
    if backtest_files > 0:
        score += 0.5
    if walk_forward_files > 0:
        score += 0.5
    if wf.get("agg_oos_sharpe", 0) > 0.5:
        score += 0.5

    runtime_evidence = len(pnl_values) >= 20
    score_cap = None
    cap_reason = None
    if runtime_evidence:
        if sharpe > 2.0:
            score += 4.0
        elif sharpe > 1.5:
            score += 3.5
        elif sharpe > 1.0:
            score += 3.0
        elif sharpe > 0.5:
            score += 2.0
        elif sharpe > 0.0:
            score += 1.0

        if calmar > 2.0:
            score += 2.0
        elif calmar > 1.0:
            score += 1.5
        elif calmar > 0.5:
            score += 1.0
        elif calmar > 0.0:
            score += 0.5
    else:
        score_cap = 4.0
        cap_reason = "Live equity history is missing or too sparse; repository structure cannot earn a strong performance grade."

    data_source = dash_source if dash_source != "missing" else ("repository" if strategies or backtest_files or walk_forward_files or wf else "missing")
    return _finalize_result(
        "sharpe_calmar",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_evidence,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


def _compute_autonomy_ratio(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    jarvis_modules = _py_module_count(engine / "brain" / "jarvis_v3", "")
    firm_board = _py_module_count(engine / "brain" / "firm_board", "")
    agent_modules = _py_module_count(engine / "brain", "") if (engine / "brain").exists() else 0
    decisions, decisions_source, decisions_status = _load_state_jsonl("jarvis_audit.jsonl", state_dir, runtime_state_dir, 500)
    decision_log_name = "jarvis_audit.jsonl"
    if not decisions:
        decisions, decisions_source, decisions_status = _load_state_jsonl(
            "decision_journal.jsonl",
            state_dir,
            runtime_state_dir,
            500,
        )
        decision_log_name = "decision_journal.jsonl"

    total = len(decisions)
    auto_count = sum(1 for d in decisions if d.get("auto_approved") or d.get("approved"))
    ratio = auto_count / total if total > 0 else 0.0
    details["jarvis_modules"] = jarvis_modules
    details["firm_board_modules"] = firm_board
    details["agent_modules"] = agent_modules
    details["decision_log"] = decision_log_name
    details["decision_source"] = decisions_source
    details["decision_status"] = decisions_status
    details["total_decisions"] = total
    details["auto_approved"] = auto_count
    if total > 0:
        details["autonomy_ratio"] = round(ratio, 4)

    score = 0.5
    if jarvis_modules >= 50:
        score = 3.5
    elif jarvis_modules >= 20:
        score = 3.0
    elif jarvis_modules >= 10:
        score = 2.5
    elif jarvis_modules >= 5:
        score = 2.0
    elif jarvis_modules > 0:
        score = 1.5
    if firm_board > 0:
        score += 0.5
    if agent_modules > 30:
        score += 0.5

    runtime_evidence = total > 0
    score_cap = None
    cap_reason = None
    if runtime_evidence:
        if ratio >= 0.85:
            score += 4.0
        elif ratio >= 0.70:
            score += 3.0
        elif ratio >= 0.50:
            score += 2.0
        elif ratio >= 0.30:
            score += 1.0
        if total >= 100:
            score += 2.0
        elif total >= 25:
            score += 1.0
        elif total >= 5:
            score += 0.5
        if total < 5:
            score_cap = 5.0
            cap_reason = "Autonomy evidence exists, but the decision sample is too small for a strong autonomy grade."
        elif total < 25:
            score_cap = 7.0
            cap_reason = "Autonomy decisions exist, but the journal is still too shallow for top-tier confidence."
    else:
        score_cap = 4.0
        cap_reason = "Declared autonomy modules are present, but there is no runtime decision evidence."

    data_source = decisions_source if decisions_source != "missing" else "repository"
    return _finalize_result(
        "autonomy_ratio",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_evidence,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


def _compute_kaizen_improvement(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    kaizen_dir = engine / "brain" / "jarvis_v3"
    kaizen_files = len(list(kaizen_dir.rglob("kaizen*")))

    kaizen, kaizen_source, kaizen_status = _load_state_json("kaizen_ledger.json", state_dir, runtime_state_dir)
    tickets = kaizen.get("tickets", []) if kaizen else []
    shipped = [t for t in tickets if t.get("status") == "SHIPPED"]
    open_tickets = [t for t in tickets if t.get("status") == "OPEN"]
    details["kaizen_modules"] = kaizen_files
    details["kaizen_source"] = kaizen_source
    details["kaizen_status"] = kaizen_status
    details["tickets_total"] = len(tickets)
    details["tickets_shipped"] = len(shipped)
    details["tickets_open"] = len(open_tickets)
    if tickets:
        details["shipping_rate"] = round(len(shipped) / len(tickets), 4)

    score = 0.5
    if kaizen_files >= 5:
        score = 3.0
    elif kaizen_files >= 3:
        score = 2.5
    elif kaizen_files >= 1:
        score = 2.0

    runtime_evidence = bool(tickets)
    score_cap = None
    cap_reason = None
    if runtime_evidence:
        rate = len(shipped) / max(len(tickets), 1)
        if rate >= 0.6:
            score += 4.0
        elif rate >= 0.4:
            score += 3.0
        elif rate >= 0.2:
            score += 2.0
        elif rate > 0:
            score += 1.0
        if len(tickets) >= 25:
            score += 1.5
        elif len(tickets) >= 10:
            score += 1.0
        elif len(tickets) >= 3:
            score += 0.5
        if len(tickets) < 3:
            score_cap = 5.5
            cap_reason = "Kaizen tickets exist, but the runtime sample is too small for a strong improvement grade."
        elif len(tickets) < 10:
            score_cap = 7.5
            cap_reason = "Kaizen evidence exists, but the ticket ledger is still shallow."
    else:
        score_cap = 4.0
        cap_reason = "Kaizen modules exist, but there is no runtime ticket evidence."

    data_source = kaizen_source if kaizen_source != "missing" else "repository"
    return _finalize_result(
        "kaizen_improvement",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_evidence,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


def _compute_regime_adaptation(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    ml_files = len(list(engine.rglob("*regime*"))) + len(list(engine.rglob("*hmm*")))
    hmm_exists = len(list(engine.rglob("*hmm*"))) > 0
    online_learn_exists = len(list(engine.rglob("*online_learn*"))) > 0
    corr_regime_exists = len(list(engine.rglob("*corr_regime*"))) > 0
    regime_stress_exists = len(list(engine.rglob("*regime_stress*"))) > 0

    hb, hb_source, hb_status = _load_state_json("avengers_heartbeat.json", state_dir, runtime_state_dir)
    hb_health = str(hb.get("health", "")) if hb else ""
    dash, dash_source, dash_status = _load_state_json("dashboard_payload.json", state_dir, runtime_state_dir)
    stress = dash.get("stress", {}) if dash else {}
    stress_present = isinstance(stress, dict) and "composite" in stress
    composite = float(stress.get("composite", 0.5)) if stress_present else 0.5

    details["ml_source_files"] = ml_files
    details["hmm_exists"] = hmm_exists
    details["online_learning"] = online_learn_exists
    details["corr_regime_detector"] = corr_regime_exists
    details["regime_stress_tracker"] = regime_stress_exists
    details["heartbeat_health"] = hb_health or "unknown"
    details["stress_composite"] = composite
    details["heartbeat_source"] = hb_source
    details["heartbeat_status"] = hb_status
    details["dashboard_source"] = dash_source
    details["dashboard_status"] = dash_status

    score = 0.5
    if ml_files >= 8:
        score = 2.5
    elif ml_files >= 4:
        score = 2.0
    elif ml_files >= 1:
        score = 1.5
    if hmm_exists:
        score += 0.5
    if online_learn_exists:
        score += 0.5
    if corr_regime_exists:
        score += 0.5
    if regime_stress_exists:
        score += 0.5

    runtime_signal_count = int(bool(hb)) + int(stress_present)
    runtime_evidence = runtime_signal_count > 0
    score_cap = None
    cap_reason = None
    if runtime_evidence:
        health_value = hb_health.lower()
        if health_value in {"healthy", "ok", "ready", "green"}:
            score += 1.0
        elif hb_health and health_value != "unknown":
            score += 0.5
        if stress_present:
            if composite < 0.3:
                score += 2.5
            elif composite < 0.5:
                score += 1.5
            elif composite < 0.7:
                score += 0.75
        if runtime_signal_count == 1:
            score_cap = 6.5
            cap_reason = "Only one live regime signal is present; a strong adaptation grade needs both heartbeat and stress context."
    else:
        score_cap = 4.0
        cap_reason = "Regime-related code exists, but there is no live heartbeat or stress evidence."

    data_source = _primary_source(hb_source, dash_source, fallback="repository")
    return _finalize_result(
        "regime_adaptation",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_evidence,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


def _compute_fault_tolerance(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    kill_switch_code = len(list(engine.rglob("*kill*switch*"))) + len(list(engine.rglob("*circuit*breaker*"))) > 0
    failover_code = len(list(engine.rglob("*failover*"))) > 0
    breaker_path = runtime_state_dir / "breaker.json"
    if not breaker_path.exists() and state_dir != runtime_state_dir:
        breaker_path = state_dir / "breaker.json"
    sentinel_path = runtime_state_dir / "operator.sentinel"
    if not sentinel_path.exists() and state_dir != runtime_state_dir:
        sentinel_path = state_dir / "operator.sentinel"
    has_breaker = breaker_path.exists()
    has_sentinel = sentinel_path.exists()

    decisions, decisions_source, decisions_status = _load_state_jsonl("jarvis_audit.jsonl", state_dir, runtime_state_dir, 300)
    decision_log_name = "jarvis_audit.jsonl"
    if not decisions:
        decisions, decisions_source, decisions_status = _load_state_jsonl(
            "decision_journal.jsonl",
            state_dir,
            runtime_state_dir,
            300,
        )
        decision_log_name = "decision_journal.jsonl"
    total_decisions = len(decisions)
    kill_switches = sum(1 for d in decisions if "kill" in str(d.get("action", "")).lower())
    venue_failovers = sum(
        1
        for d in decisions
        if "failover" in str(d.get("action", "")).lower() or "venue" in str(d.get("subsystem", "")).lower()
    )

    details["kill_switch_code"] = kill_switch_code
    details["failover_code"] = failover_code
    details["breaker_active"] = has_breaker
    details["sentinel_active"] = has_sentinel
    details["decision_log"] = decision_log_name
    details["decision_source"] = decisions_source
    details["decision_status"] = decisions_status
    details["decision_count"] = total_decisions
    details["kill_switch_triggers"] = kill_switches
    details["venue_failovers"] = venue_failovers

    score = 0.5
    if kill_switch_code and failover_code:
        score = 3.0
    elif kill_switch_code:
        score = 2.5

    runtime_evidence = has_breaker or has_sentinel or total_decisions > 0
    score_cap = None
    cap_reason = None
    if runtime_evidence:
        if has_breaker:
            score += 1.0
        if has_sentinel:
            score += 1.0
        if total_decisions >= 25 and kill_switches == 0:
            score += 2.0
        elif total_decisions >= 5 and kill_switches == 0:
            score += 1.5
        elif total_decisions > 0 and kill_switches == 0:
            score += 1.0
        elif kill_switches > 0:
            score += 0.5
        if venue_failovers > 0:
            score += 1.0
        elif failover_code and total_decisions > 0:
            score += 0.5
        if total_decisions == 0:
            score_cap = 6.0
            cap_reason = "Guard files are present, but there is no runtime journal showing fault-handling behavior."
        elif total_decisions < 10:
            score_cap = 7.0
            cap_reason = "Fault-tolerance evidence exists, but the journal is still too shallow for a top-tier grade."
    else:
        score_cap = 4.0
        cap_reason = "Fault-tolerance code exists, but there is no runtime guard or journal evidence."

    data_source = decisions_source if decisions_source != "missing" else "repository"
    return _finalize_result(
        "fault_tolerance",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_evidence,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


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

    results, results_source, results_status = _load_state_json("quantum_results.json", state_dir, runtime_state_dir)
    invocations = int(results.get("total_invocations", 0)) if results else 0
    details["quantum_modules"] = len(q_files)
    details["quantum_tests"] = q_test_files
    details["dwave"] = has_dwave
    details["qiskit"] = has_qiskit
    details["pennylane"] = has_pennylane
    details["qubo_problem_types"] = qubo_types
    details["results_source"] = results_source
    details["results_status"] = results_status
    details["total_invocations"] = invocations

    if not q_files:
        return _finalize_result(
            "quantum_edge",
            0.0,
            details,
            data_source="missing",
            runtime_evidence=False,
        )

    score = 1.5
    if has_dwave:
        score += 0.5
    if has_qiskit:
        score += 0.5
    if has_pennylane:
        score += 0.5
    if qubo_types >= 3:
        score += 0.75
    if q_test_files >= 10:
        score += 0.75

    runtime_evidence = invocations > 0
    score_cap = None
    cap_reason = None
    if runtime_evidence:
        if invocations > 100:
            score += 5.0
        elif invocations > 25:
            score += 4.0
        elif invocations > 10:
            score += 3.0
        else:
            score += 1.5
        if invocations < 5:
            score_cap = 6.0
            cap_reason = "Quantum execution evidence exists, but the invocation count is too low for a strong edge grade."
        elif invocations < 25:
            score_cap = 8.0
            cap_reason = "Quantum execution exists, but the runtime sample is still modest."
    else:
        if results_source != "missing" and results_status == "ok":
            score_cap = 5.0
            cap_reason = "Quantum runtime reporting exists, but it shows no executed work."
        else:
            score_cap = 4.0
            cap_reason = "Quantum code and tests exist, but there is no runtime execution evidence."

    data_source = results_source if results_source != "missing" else "repository"
    return _finalize_result(
        "quantum_edge",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_evidence,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


def _compute_test_coverage(state_dir: Path, runtime_state_dir: Path) -> dict:
    del state_dir, runtime_state_dir
    details: dict[str, Any] = {}
    engine = workspace_roots.ETA_ENGINE_ROOT
    test_files = _test_count(engine / "tests") if (engine / "tests").exists() else 0
    workspace_tests = workspace_roots.WORKSPACE_ROOT / "tests"
    if workspace_tests.exists() and workspace_tests != engine / "tests":
        test_files += _test_count(workspace_tests)
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
        score = min(score + 1.0, 10.0)

    return _finalize_result(
        "test_coverage",
        score,
        details,
        data_source="repository",
        runtime_evidence=False,
        score_cap=6.0,
        cap_reason="Test-file counts are informative, but they are not live runtime evidence.",
    )


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
    for name, path in marker_paths:
        if path is None or path.exists():
            found_names.append(name)
    details["deployment_markers_found"] = len(found_names)
    details["deployment_markers_total"] = len(marker_paths)
    details["markers"] = found_names

    readiness, readiness_source, readiness_status = _load_state_json(
        "bot_strategy_readiness_latest.json",
        state_dir,
        runtime_state_dir,
    )
    has_readiness = bool(readiness)
    details["strategy_readiness"] = has_readiness
    details["strategy_readiness_source"] = readiness_source
    details["strategy_readiness_status"] = readiness_status

    fm_health, fm_source, fm_status = _load_state_json("fm_health.json", state_dir, runtime_state_dir)
    has_fm_health = bool(fm_health)
    fm_status_value = str(fm_health.get("status", "")).lower() if fm_health else ""
    details["fm_health"] = has_fm_health
    details["fm_health_source"] = fm_source
    details["fm_health_status"] = fm_status
    details["fm_health_value"] = fm_status_value or "unknown"

    vps_script = root / "scripts" / "vps_bootstrap.ps1"
    has_vps = vps_script.exists()
    details["vps_bootstrap_script"] = has_vps

    score = len(found_names) * 0.35 + (0.5 if has_vps else 0.0)
    runtime_signal_count = int(has_readiness) + int(has_fm_health)
    if has_readiness:
        score += 2.0
    if has_fm_health:
        score += 2.0
    if fm_status_value in {"ok", "healthy", "ready"}:
        score += 1.0

    score_cap = None
    cap_reason = None
    if runtime_signal_count == 0:
        score_cap = 4.0
        cap_reason = "Deployment markers exist, but there is no runtime readiness or Force Multiplier health evidence."
    elif runtime_signal_count == 1:
        score_cap = 7.0
        cap_reason = "Only one live ops signal is present; strong ops maturity needs both readiness and health snapshots."

    data_source = _primary_source(readiness_source, fm_source, fallback="repository")
    return _finalize_result(
        "ops_maturity",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_signal_count > 0,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


def _compute_regulatory_posture(state_dir: Path, runtime_state_dir: Path) -> dict:
    details: dict[str, Any] = {}
    root = workspace_roots.WORKSPACE_ROOT
    audit, audit_source, audit_status = _load_state_json("nightly_audit.json", state_dir, runtime_state_dir)
    has_audit = bool(audit)
    decisions, decisions_source, decisions_status = _load_state_jsonl("jarvis_audit.jsonl", state_dir, runtime_state_dir, 100)
    decisions_count = len(decisions)
    journal, journal_source, journal_status = _load_state_jsonl("decision_journal.jsonl", state_dir, runtime_state_dir, 50)
    has_journal = bool(journal)

    compliance_docs = list(root.rglob("COMPLIANCE*")) + list(root.rglob("compliance*")) + list(root.rglob("*audit*"))
    llc_docs = list(root.rglob("*LLC*")) + list(root.rglob("*llc*"))
    details["nightly_audit_exists"] = has_audit
    details["nightly_audit_source"] = audit_source
    details["nightly_audit_status"] = audit_status
    details["audit_log_entries"] = decisions_count
    details["audit_log_source"] = decisions_source
    details["audit_log_status"] = decisions_status
    details["decision_journal"] = has_journal
    details["decision_journal_source"] = journal_source
    details["decision_journal_status"] = journal_status
    details["compliance_documents"] = len(compliance_docs)
    details["llc_references"] = len(llc_docs)

    score = 1.5
    if compliance_docs:
        score += 0.5
    if llc_docs:
        score += 0.5
    if has_audit:
        score += 2.5
    if has_journal:
        score += 1.5
    if decisions_count >= 50:
        score += 2.5
    elif decisions_count >= 10:
        score += 1.5
    elif decisions_count > 0:
        score += 0.5

    runtime_signal_count = int(has_audit) + int(has_journal) + int(decisions_count > 0)
    score_cap = None
    cap_reason = None
    if runtime_signal_count == 0:
        score_cap = 4.0
        cap_reason = "Compliance docs exist, but there is no runtime audit or decision-journal evidence."
    elif decisions_count < 10:
        score_cap = 7.0
        cap_reason = "Audit evidence exists, but the live journal is still too thin for a strong regulatory posture grade."

    data_source = _primary_source(audit_source, decisions_source, journal_source, fallback="repository")
    return _finalize_result(
        "regulatory_posture",
        score,
        details,
        data_source=data_source,
        runtime_evidence=runtime_signal_count > 0,
        score_cap=score_cap,
        cap_reason=cap_reason,
    )


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
    rdir = runtime_state_dir or workspace_roots.ETA_RUNTIME_STATE_DIR
    sdir = state_dir or rdir
    categories: dict[str, dict] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for name, computer in COMPUTERS.items():
        result = computer(sdir, rdir)
        categories[name] = result
        weighted_sum += result["score"] * result["weight"]
        weight_total += result["weight"]
    composite = round(weighted_sum / weight_total, 2) if weight_total > 0 else 0.0

    runtime_evidence_categories = [
        name for name, result in categories.items() if result["details"].get("runtime_evidence")
    ]
    capped_categories = [
        name for name, result in categories.items() if result["details"].get("score_cap_reason")
    ]
    launch_summary, launch_cap, launch_cap_reason, status_override = _launch_readiness_overlay(sdir, rdir)
    if launch_cap is not None and composite > launch_cap:
        launch_summary["uncapped_composite_score"] = composite
        launch_summary["composite_score_cap"] = launch_cap
        launch_summary["composite_score_cap_reason"] = launch_cap_reason
        composite = launch_cap

    grade = _grade_from_composite(composite)

    status = "ready"
    if not runtime_evidence_categories:
        status = "degraded"
    elif len(runtime_evidence_categories) < 4:
        status = "limited"
    elif status_override and status == "ready":
        status = status_override

    summary = {
        "composite_score": composite,
        "grade": grade,
        "category_count": len(categories),
        "runtime_evidence_categories": len(runtime_evidence_categories),
        "capped_categories": capped_categories,
        "top_strength": max(categories, key=lambda k: categories[k]["score"]),
        "top_weakness": min(categories, key=lambda k: categories[k]["score"]),
    }
    summary.update(launch_summary)

    return {
        "schema_version": SCORE_VERSION,
        "generated_at": _now_iso(),
        "source": "firm_scorecard",
        "status": status,
        "composite_score": composite,
        "grade": grade,
        "categories": categories,
        "weights": dict(SCORE_WEIGHTS),
        "summary": summary,
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
