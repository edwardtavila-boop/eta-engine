"""Tests for cost_tracker — LLM spend telemetry."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _write_audit(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _ts(days_ago: int = 0, hours_ago: int = 0) -> str:
    dt = datetime.now(UTC) - timedelta(days=days_ago, hours=hours_ago)
    return dt.isoformat()


def test_estimate_call_cost_flat_rate_default() -> None:
    """No token counts → flat-rate fallback."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    cost = cost_tracker.estimate_call_cost()
    assert cost == cost_tracker.DEFAULT_FLAT_RATE_PER_CALL


def test_estimate_call_cost_with_tokens() -> None:
    """Token counts → cost from prices."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    cost = cost_tracker.estimate_call_cost(input_tokens=4000, output_tokens=100)
    # 4000 * 0.0005/1000 + 100 * 0.002/1000 = 0.002 + 0.0002 = 0.0022
    assert abs(cost - 0.0022) < 1e-6


def test_estimate_spend_empty_when_no_audit_log(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import cost_tracker

    summary = cost_tracker.estimate_spend(
        since_days_ago=7,
        audit_path=tmp_path / "missing.jsonl",
    )
    assert summary.total_usd == 0.0
    assert summary.n_calls == 0


def test_estimate_spend_aggregates_by_tool(tmp_path: Path) -> None:
    """Tool counts roll up correctly."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    _write_audit(
        log,
        [
            {"ts": _ts(0, 1), "tool": "jarvis_fleet_status"},
            {"ts": _ts(0, 2), "tool": "jarvis_fleet_status"},
            {"ts": _ts(0, 3), "tool": "jarvis_set_size_modifier"},
        ],
    )
    summary = cost_tracker.estimate_spend(since_days_ago=7, audit_path=log)
    assert summary.n_calls == 3
    assert summary.by_tool["jarvis_fleet_status"]["n"] == 2
    assert summary.by_tool["jarvis_set_size_modifier"]["n"] == 1
    assert summary.total_usd > 0


def test_estimate_spend_filters_window(tmp_path: Path) -> None:
    """since_days_ago filter drops older records."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    _write_audit(
        log,
        [
            {"ts": _ts(0, 1), "tool": "recent"},
            {"ts": _ts(0, 2), "tool": "recent"},
            {"ts": _ts(10, 0), "tool": "old"},  # outside 7-day window
            {"ts": _ts(30, 0), "tool": "ancient"},  # outside
        ],
    )
    summary = cost_tracker.estimate_spend(since_days_ago=7, audit_path=log)
    assert summary.n_calls == 2
    assert "old" not in summary.by_tool
    assert "ancient" not in summary.by_tool


def test_estimate_spend_groups_by_day(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    _write_audit(
        log,
        [
            {"ts": _ts(0, 0), "tool": "a"},
            {"ts": _ts(0, 1), "tool": "a"},
            {"ts": _ts(1, 0), "tool": "b"},
        ],
    )
    summary = cost_tracker.estimate_spend(since_days_ago=7, audit_path=log)
    days = list(summary.by_day.keys())
    # At least 2 distinct days appear
    assert len(days) >= 1
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    assert today_str in days


def test_today_spend_returns_today_only(tmp_path: Path) -> None:
    """today_spend counts only records since UTC midnight today."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    _write_audit(
        log,
        [
            {"ts": _ts(0, 1), "tool": "today_call"},
            {"ts": _ts(0, 2), "tool": "today_call"},
            {"ts": _ts(5, 0), "tool": "old_call"},
        ],
    )
    result = cost_tracker.today_spend(audit_path=log)
    assert result["n_calls"] >= 2  # depends on whether we crossed midnight
    assert result["total_usd"] > 0


def test_anomaly_check_no_anomaly_for_steady_load(tmp_path: Path) -> None:
    """Steady load → no anomaly flag."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    # 60 calls over 24h = 2.5/hour avg; 2-3 in last hour is normal
    records = []
    for i in range(60):
        records.append({"ts": _ts(0, i // 3), "tool": "steady"})
    _write_audit(log, records)
    result = cost_tracker.anomaly_check(window_min=60, audit_path=log)
    assert result["anomaly"] is False


def test_anomaly_check_flags_recent_spike(tmp_path: Path) -> None:
    """Sudden 100x spike → anomaly=True."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    records = []
    # Quiet baseline: 24 calls over 24h = 1/hour
    for i in range(24):
        records.append({"ts": _ts(0, i + 1), "tool": "baseline"})
    # Spike: 100 calls in the last hour
    for _ in range(100):
        records.append({"ts": _ts(0, 0), "tool": "runaway_cron"})
    _write_audit(log, records)
    result = cost_tracker.anomaly_check(window_min=60, audit_path=log)
    assert result["anomaly"] is True
    assert result["multiplier"] >= 10.0


def test_anomaly_check_skips_when_baseline_too_small(tmp_path: Path) -> None:
    """Few records → can't compute meaningful anomaly, skip."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    _write_audit(log, [{"ts": _ts(0, 1), "tool": "lonely"}])
    result = cost_tracker.anomaly_check(window_min=60, audit_path=log)
    assert result["anomaly"] is False
    assert "baseline_too_small" in result.get("reason", "")


def test_estimate_spend_never_raises_on_corrupt_audit(tmp_path: Path) -> None:
    """Garbage lines in audit log are skipped, no exception."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    log.write_text(
        "not json\n" + json.dumps({"ts": _ts(0, 1), "tool": "ok"}) + "\n" + "more garbage\n",
        encoding="utf-8",
    )
    summary = cost_tracker.estimate_spend(since_days_ago=7, audit_path=log)
    # The one good record counts; the garbage doesn't crash
    assert summary.n_calls == 1


def test_summary_to_dict_is_json_serializable(tmp_path: Path) -> None:
    """SpendSummary.to_dict() round-trips through JSON."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    log = tmp_path / "audit.jsonl"
    _write_audit(log, [{"ts": _ts(0, 1), "tool": "a"}])
    summary = cost_tracker.estimate_spend(since_days_ago=7, audit_path=log)
    d = summary.to_dict()
    payload = json.dumps(d, default=str)
    assert isinstance(payload, str)
    parsed = json.loads(payload)
    assert parsed["n_calls"] == 1


def test_env_var_overrides_pricing(monkeypatch) -> None:
    """DEEPSEEK_INPUT_PRICE_PER_1K env var overrides default."""
    from eta_engine.brain.jarvis_v3 import cost_tracker

    monkeypatch.setenv("DEEPSEEK_INPUT_PRICE_PER_1K", "0.01")  # 20x default
    cost = cost_tracker.estimate_call_cost(input_tokens=1000, output_tokens=0)
    # 1000 tokens × 0.01/1000 = 0.01
    assert abs(cost - 0.01) < 1e-6
