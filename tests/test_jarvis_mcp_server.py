"""Tests for the JARVIS MCP server (Half 1 of the JARVIS-Hermes bridge).

The server exposes 11 JARVIS Supercharge tools behind a stdio MCP
transport, gated on ``JARVIS_MCP_TOKEN``. Every tool returns the
``{"ok": bool, "data": ..., "error": ...}`` envelope and never raises;
every call writes one JSONL line to the hermes audit log.

These tests exercise the in-process handler surface — they do NOT spin
up a stdio server. Spawning a real subprocess is reserved for the
integration suite Half 3 owns.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def patched_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    """Redirect every external state file the server touches into tmp_path."""
    audit_log = tmp_path / "hermes_actions.jsonl"
    kaizen_action_log = tmp_path / "kaizen_actions.jsonl"
    overrides_path = tmp_path / "kaizen_overrides.json"
    hermes_state_path = tmp_path / "jarvis_intel" / "hermes_state.json"

    from eta_engine.mcp_servers import jarvis_mcp_server

    monkeypatch.setattr(jarvis_mcp_server, "_AUDIT_LOG_PATH", audit_log)
    monkeypatch.setattr(jarvis_mcp_server, "_KAIZEN_ACTION_LOG_PATH", kaizen_action_log)
    monkeypatch.setattr(jarvis_mcp_server, "_KAIZEN_OVERRIDES_PATH", overrides_path)
    monkeypatch.setattr(jarvis_mcp_server, "_HERMES_STATE_PATH", hermes_state_path)
    monkeypatch.setenv("JARVIS_MCP_TOKEN", "test-token")

    return {
        "audit_log": audit_log,
        "kaizen_action_log": kaizen_action_log,
        "overrides": overrides_path,
        "hermes_state": hermes_state_path,
    }


@pytest.fixture()
def mock_underlying(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every external module call with deterministic stubs."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fleet_report = {
        "n_bots": 4,
        "tier_counts": {"ELITE": 1, "PRODUCER": 2, "DECAY": 1},
        "mc_counts": {"ROBUST": 2, "MIXED": 2},
        "action_counts": {"MONITOR": 3, "RETIRE": 1},
        "actions": [],
        "elite_summary": {
            "tier_counts": {"ELITE": 1, "PRODUCER": 2, "DECAY": 1},
            "top5_elite": [
                {"bot_id": "atr_breakout_mnq", "tier": "ELITE", "score": 1.81},
            ],
            "top5_dark": [
                {"bot_id": "rsi_mr_mnq", "tier": "DECAY", "score": -0.32},
            ],
        },
    }

    trace_records = [
        {"consult_id": "abc123", "bot_id": "atr_breakout_mnq", "verdict": {"final_verdict": "APPROVED"}},
        {"consult_id": "def456", "bot_id": "vp_mnq", "verdict": {"final_verdict": "DENIED"}},
    ]

    wiring_statuses = [
        type("S", (), {"module": "sage.bayes", "expected_to_fire": True, "dark_for_days": 0})(),
        type("S", (), {"module": "sage.dormant", "expected_to_fire": True, "dark_for_days": 9})(),
        type("S", (), {"module": "lab.unused", "expected_to_fire": False, "dark_for_days": 99})(),
    ]

    portfolio_verdict = type(
        "V", (), {"size_modifier": 0.7, "block_reason": None, "notes": ("test",)},
    )()

    hot_weights = {"momentum": 1.05, "mean_revert": 0.92}

    cal_event = type(
        "E", (), {"ts_utc": "2026-05-11T12:00:00Z", "kind": "CPI", "symbol": None, "severity": 3},
    )()

    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_kaizen_latest",
        lambda: fleet_report,
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_trace_tail",
        lambda n: trace_records[-n:],
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_wiring_audit",
        lambda: wiring_statuses,
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_portfolio_snapshot",
        lambda: object(),
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_portfolio_assess",
        lambda *a, **kw: portfolio_verdict,
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_hot_weights",
        lambda asset: hot_weights,
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_upcoming_events",
        lambda horizon_min: [cal_event],
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_kaizen_run",
        lambda bootstraps: {**fleet_report, "bootstraps": bootstraps},
    )
    monkeypatch.setattr(
        jarvis_mcp_server,
        "_call_verdict_to_narrative",
        lambda rec: f"narrative for consult {rec.get('consult_id')}",
    )

    return {
        "fleet_report": fleet_report,
        "trace_records": trace_records,
        "wiring_statuses": wiring_statuses,
        "portfolio_verdict": portfolio_verdict,
        "hot_weights": hot_weights,
        "cal_event": cal_event,
    }


def _call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a tool by name through the dispatch surface."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    return jarvis_mcp_server.dispatch_tool_call(name, args or {})


def _audit_lines(path: Path) -> list[dict[str, Any]]:
    """Read every audit-log JSONL line into dicts (skip blanks)."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# 1. Auth gate — token required
# ---------------------------------------------------------------------------


def test_token_required_for_read_only_tool(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """No ``_auth`` argument → envelope reports ``auth_failed``."""
    result = _call("jarvis_fleet_status")

    assert result == {"ok": False, "data": None, "error": "auth_failed"}
    # And one audit row written with auth=failed
    lines = _audit_lines(patched_paths["audit_log"])
    assert len(lines) == 1
    assert lines[0]["auth"] == "failed"
    assert lines[0]["tool"] == "jarvis_fleet_status"


# ---------------------------------------------------------------------------
# 2. Correct token passes auth
# ---------------------------------------------------------------------------


def test_correct_token_passes_auth(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """Matching ``_auth`` → ``ok=True`` and audit row marked ``auth=ok``."""
    result = _call("jarvis_fleet_status", {"_auth": "test-token"})

    assert result["ok"] is True
    assert result["error"] is None

    lines = _audit_lines(patched_paths["audit_log"])
    assert lines[-1]["auth"] == "ok"


# ---------------------------------------------------------------------------
# 3. Documented shape for fleet_status
# ---------------------------------------------------------------------------


def test_fleet_status_returns_documented_shape(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """Fleet status carries the keys the spec promises."""
    result = _call("jarvis_fleet_status", {"_auth": "test-token"})

    assert result["ok"] is True
    data = result["data"]
    for key in (
        "n_bots",
        "tier_counts",
        "mc_counts",
        "action_counts",
        "top5_elite",
        "top5_dark",
    ):
        assert key in data, f"missing key {key} in fleet_status payload"
    assert data["n_bots"] == 4
    assert isinstance(data["top5_elite"], list)


# ---------------------------------------------------------------------------
# 4. Trace tail returns list of dicts of length N
# ---------------------------------------------------------------------------


def test_trace_tail_returns_list(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """trace_tail honors ``n`` and returns a list of dict records."""
    result = _call("jarvis_trace_tail", {"_auth": "test-token", "n": 1})

    assert result["ok"] is True
    assert isinstance(result["data"], list)
    assert len(result["data"]) == 1
    assert isinstance(result["data"][0], dict)


# ---------------------------------------------------------------------------
# 5. Kill switch requires exact phrase
# ---------------------------------------------------------------------------


def test_kill_switch_requires_exact_phrase(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """``confirm_phrase="kill"`` → REJECTED; ``"kill all"`` → APPLIED."""
    rejected = _call(
        "jarvis_kill_switch",
        {"_auth": "test-token", "reason": "drill", "confirm_phrase": "kill"},
    )
    assert rejected["ok"] is True
    assert rejected["data"]["status"] == "REJECTED"
    assert rejected["data"]["reason"] == "confirm_phrase_mismatch"

    applied = _call(
        "jarvis_kill_switch",
        {"_auth": "test-token", "reason": "drill", "confirm_phrase": "kill all"},
    )
    assert applied["ok"] is True
    assert applied["data"]["status"] == "APPLIED"
    assert "killed_at" in applied["data"]


# ---------------------------------------------------------------------------
# 6. Kill switch writes hermes_state on APPLIED path
# ---------------------------------------------------------------------------


def test_kill_switch_writes_hermes_state(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """APPLIED kill → ``hermes_state.json`` exists with ``kill_all: true``."""
    result = _call(
        "jarvis_kill_switch",
        {"_auth": "test-token", "reason": "drill", "confirm_phrase": "kill all"},
    )
    assert result["ok"] is True
    assert result["data"]["status"] == "APPLIED"

    path = patched_paths["hermes_state"]
    assert path.exists(), "hermes_state.json should exist after APPLIED kill"
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["kill_all"] is True
    assert "killed_at" in body
    assert body["reason"] == "drill"


# ---------------------------------------------------------------------------
# 7. Retire-strategy 2-run gate holds on first call
# ---------------------------------------------------------------------------


def test_retire_strategy_2run_gate_holds_first_call(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """First RETIRE recommendation (no prior in kaizen_actions) → HELD."""
    result = _call(
        "jarvis_retire_strategy",
        {"_auth": "test-token", "bot_id": "vp_mnq", "reason": "tier=DECAY"},
    )
    assert result["ok"] is True
    assert result["data"]["status"] == "HELD"
    assert result["data"]["reason"] == "awaiting_confirmation"
    # No sidecar override should have been written
    assert not patched_paths["overrides"].exists()


# ---------------------------------------------------------------------------
# 8. Retire-strategy applies on second call
# ---------------------------------------------------------------------------


def test_retire_strategy_applies_on_second_call(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """Same bot_id present in kaizen_actions log → APPLIED + sidecar written."""
    # Seed a prior RETIRE record for vp_mnq
    log = patched_paths["kaizen_action_log"]
    log.write_text(
        json.dumps({"action": "RETIRE", "bot_id": "vp_mnq", "reason": "prior"}) + "\n",
        encoding="utf-8",
    )

    result = _call(
        "jarvis_retire_strategy",
        {"_auth": "test-token", "bot_id": "vp_mnq", "reason": "tier=DECAY mc=DEAD"},
    )
    assert result["ok"] is True
    assert result["data"]["status"] == "APPLIED"

    # Sidecar override now lists the bot
    assert patched_paths["overrides"].exists()
    sidecar = json.loads(patched_paths["overrides"].read_text(encoding="utf-8"))
    assert "vp_mnq" in sidecar.get("deactivated", {})


# ---------------------------------------------------------------------------
# 9. Audit log written on every call
# ---------------------------------------------------------------------------


def test_audit_log_written_on_every_call(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """A successful call writes exactly one JSONL row to the audit log."""
    _call("jarvis_fleet_status", {"_auth": "test-token"})

    lines = _audit_lines(patched_paths["audit_log"])
    assert len(lines) == 1
    row = lines[0]
    for key in ("ts", "tool", "args", "auth", "result_status", "elapsed_ms", "caller"):
        assert key in row, f"missing audit field {key}"
    assert row["caller"] == "hermes-mcp"
    assert row["tool"] == "jarvis_fleet_status"


# ---------------------------------------------------------------------------
# 10. Audit log scrubs _auth and _confirm_phrase
# ---------------------------------------------------------------------------


def test_audit_log_scrubs_auth_token(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """The args field never contains the raw token nor the confirm phrase."""
    _call(
        "jarvis_kill_switch",
        {"_auth": "test-token", "reason": "drill", "confirm_phrase": "kill all"},
    )
    _call(
        "jarvis_deploy_strategy",
        {"_auth": "test-token", "bot_id": "vp_mnq", "reason": "tier=ELITE", "_confirm_phrase": "yes"},
    )

    raw = patched_paths["audit_log"].read_text(encoding="utf-8")
    assert "test-token" not in raw
    # confirm_phrase value should be redacted/absent (not the literal "kill all")
    # The audit log MUST scrub _confirm_phrase. The user-visible "confirm_phrase"
    # for kill_switch is the operator's literal string; the scrub rule from the
    # brief targets the underscore-prefixed variants.
    for line in raw.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        args = rec.get("args", {})
        assert "_auth" not in args, "_auth must be stripped from audit args"
        assert "_confirm_phrase" not in args, "_confirm_phrase must be stripped from audit args"


# ---------------------------------------------------------------------------
# 11. Handler exception returns envelope, never raises
# ---------------------------------------------------------------------------


def test_handler_exception_returns_envelope_not_raise(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the underlying call blows up, the tool surface returns ``ok=False``."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    def _boom() -> dict[str, Any]:
        msg = "wiring not loaded"
        raise RuntimeError(msg)

    monkeypatch.setattr(jarvis_mcp_server, "_call_kaizen_latest", _boom)

    # Must NOT raise — must return an envelope.
    result = _call("jarvis_fleet_status", {"_auth": "test-token"})

    assert result["ok"] is False
    assert result["error"] is not None
    assert "wiring" in result["error"].lower() or "error" in result["error"].lower()


# ---------------------------------------------------------------------------
# 12. Tool registry has all 11 tools
# ---------------------------------------------------------------------------


def test_tool_registry_has_all_11_tools() -> None:
    """list_tools() exposes exactly the documented 11 tool names."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    expected = {
        "jarvis_fleet_status",
        "jarvis_trace_tail",
        "jarvis_wiring_audit",
        "jarvis_portfolio_assess",
        "jarvis_hot_weights",
        "jarvis_upcoming_events",
        "jarvis_kaizen_run",
        "jarvis_deploy_strategy",
        "jarvis_retire_strategy",
        "jarvis_kill_switch",
        "jarvis_explain_verdict",
    }
    declared = {t["name"] for t in jarvis_mcp_server.list_tools()}
    assert declared == expected, f"missing={expected - declared} extras={declared - expected}"
    assert len(declared) == 11
