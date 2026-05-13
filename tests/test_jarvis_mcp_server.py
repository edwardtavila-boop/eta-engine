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
        "V",
        (),
        {"size_modifier": 0.7, "block_reason": None, "notes": ("test",)},
    )()

    hot_weights = {"momentum": 1.05, "mean_revert": 0.92}

    cal_event = type(
        "E",
        (),
        {"ts_utc": "2026-05-11T12:00:00Z", "kind": "CPI", "symbol": None, "severity": 3},
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
    """Wrong ``_auth`` argument -> envelope reports ``auth_failed``.

    NOTE (2026-05-12): policy relaxed - env-token presence is sufficient
    auth for stdio MCP clients that don't pass arg-level auth on every
    tool call (Hermes Agent, Claude Desktop, etc). Missing ``_auth`` is
    now ACCEPTED when JARVIS_MCP_TOKEN env is set. A WRONG ``_auth``
    value still fails - that's what this test now exercises.
    """
    result = _call("jarvis_fleet_status", {"_auth": "WRONG-TOKEN-VALUE"})

    assert result == {"ok": False, "data": None, "error": "auth_failed"}
    # And one audit row written with auth=failed
    lines = _audit_lines(patched_paths["audit_log"])
    assert len(lines) == 1
    assert lines[0]["auth"] == "failed"
    assert lines[0]["tool"] == "jarvis_fleet_status"


def test_missing_auth_uses_env_token_for_stdio_clients(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
) -> None:
    """Missing ``_auth`` is accepted when the MCP process has an env token."""
    result = _call("jarvis_fleet_status")

    assert result["ok"] is True
    assert result["error"] is None
    lines = _audit_lines(patched_paths["audit_log"])
    assert lines[-1]["auth"] == "env"
    assert lines[-1]["tool"] == "jarvis_fleet_status"


def test_missing_env_token_fails_closed(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured ``JARVIS_MCP_TOKEN`` rejects all tool calls."""
    monkeypatch.delenv("JARVIS_MCP_TOKEN", raising=False)

    result = _call("jarvis_fleet_status")

    assert result == {"ok": False, "data": None, "error": "auth_no_token_configured"}
    lines = _audit_lines(patched_paths["audit_log"])
    assert lines[-1]["auth"] == "failed"
    assert lines[-1]["result_status"] == "auth_no_token_configured"


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
# 12. Tool registry has all 12 tools
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Audit-log rotation hardening
# ---------------------------------------------------------------------------


def test_audit_log_rotates_when_threshold_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the audit log crosses the rotation threshold the file is gzipped
    to a stamped sibling and a fresh active file is started.

    Hardening contract: rotation happens BEFORE the next append, so the
    write that triggers rotation sees an empty active file and the rotated
    copy preserves all prior history. No data loss.
    """
    import gzip

    from eta_engine.mcp_servers import jarvis_mcp_server

    audit_log = tmp_path / "hermes_actions.jsonl"
    monkeypatch.setattr(jarvis_mcp_server, "_AUDIT_LOG_PATH", audit_log)
    # Shrink threshold so we can exercise rotation in-test without writing 10MB.
    monkeypatch.setattr(jarvis_mcp_server, "_AUDIT_LOG_MAX_BYTES", 200)
    monkeypatch.setenv("JARVIS_MCP_TOKEN", "test-token")

    # Write enough entries to cross 200B. Each json.dumps row is ~80B,
    # so 5 rows ≈ 400B → guaranteed rotation by the 5th append.
    for i in range(8):
        jarvis_mcp_server._append_audit(
            {
                "tool": "smoke",
                "i": i,
                "padding": "x" * 40,
            }
        )

    # Rotated file(s) present
    rotated = list(tmp_path.glob("hermes_actions_*.jsonl.gz"))
    assert len(rotated) >= 1, f"expected rotated .gz file(s), got: {list(tmp_path.iterdir())}"

    # Each rotated file is valid gzip and contains valid JSON lines
    for r in rotated:
        with gzip.open(r, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    json.loads(line)  # must parse

    # Active file is smaller than threshold (it was reset after rotation)
    if audit_log.exists():
        assert audit_log.stat().st_size <= jarvis_mcp_server._AUDIT_LOG_MAX_BYTES


def test_audit_log_rotation_failure_does_not_break_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if rotation itself fails (gzip import error, disk error, etc),
    the next append still writes successfully.

    Contract: audit logging is best-effort; rotation is best-effort on
    top of that. A consult MUST NOT fail because the audit log got too big.
    """
    from eta_engine.mcp_servers import jarvis_mcp_server

    audit_log = tmp_path / "hermes_actions.jsonl"
    monkeypatch.setattr(jarvis_mcp_server, "_AUDIT_LOG_PATH", audit_log)
    monkeypatch.setattr(jarvis_mcp_server, "_AUDIT_LOG_MAX_BYTES", 100)
    monkeypatch.setenv("JARVIS_MCP_TOKEN", "test-token")

    # Seed an over-threshold log
    audit_log.write_text("x" * 500 + "\n", encoding="utf-8")

    # Sabotage the rotation helper so it raises OSError
    def explode():
        raise OSError("simulated disk-full during gzip rotation")

    monkeypatch.setattr(jarvis_mcp_server, "_rotate_audit_log_if_needed", explode)

    # Append should still succeed — _append_audit catches the rotation crash
    # via its outer OSError handler.
    jarvis_mcp_server._append_audit({"tool": "smoke", "after_rot_fail": True})

    # The post-failure append should have landed somewhere
    final_contents = audit_log.read_text(encoding="utf-8")
    assert "after_rot_fail" in final_contents


def test_tool_registry_has_all_42_tools() -> None:
    """list_tools() exposes exactly the documented 42 tool names.

    History:
      * 11 → 12 (2026-05-12): jarvis_subscribe_events (Track 1).
      * 12 → 16 (2026-05-12): write-back tools (Track 2).
      * 16 → 17 (2026-05-12): jarvis_topology (Track 17).
      * 17 → 21 (2026-05-12): inter-agent bus (Track 14).
      * 21 → 24 (2026-05-12): consult replay/debug (T6, T7).
      * 24 → 29 (2026-05-12): attribution + regime + Kelly (T12, T8, T13).
      * 29 → 30 (2026-05-12): jarvis_zeus (Zeus Supercharge).
      * 30 → 33 (2026-05-12): cost_summary + cost_today + cost_anomaly.
      * 33 → 36 (2026-05-12): pnl_summary + pnl_multi_window + material_events_since.
      * 36 → 38 (2026-05-12): anomaly_scan + anomaly_recent (operator-friendly
        Telegram replacements for noisy watchdog auto-heal pings).
      * 38 → 39 (2026-05-12): jarvis_preflight (live-cutover Go/No-Go).
      * 39 → 42 (2026-05-12): prop_firm_status + prop_firm_evaluate +
        prop_firm_killall (elite-level prop firm rule enforcement gate).
    """
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
        "jarvis_subscribe_events",
        "jarvis_set_size_modifier",
        "jarvis_pin_school_weight",
        "jarvis_active_overrides",
        "jarvis_clear_override",
        "jarvis_topology",
        "jarvis_register_agent",
        "jarvis_list_agents",
        "jarvis_acquire_lock",
        "jarvis_release_lock",
        "jarvis_replay_consult",
        "jarvis_explain_consult_causal",
        "jarvis_counterfactual",
        "jarvis_attribution_cube",
        "jarvis_current_regime",
        "jarvis_list_regime_packs",
        "jarvis_apply_regime_pack",
        "jarvis_kelly_recommend",
        "jarvis_zeus",
        "jarvis_cost_summary",
        "jarvis_cost_today",
        "jarvis_cost_anomaly",
        "jarvis_pnl_summary",
        "jarvis_pnl_multi_window",
        "jarvis_material_events_since",
        "jarvis_anomaly_scan",
        "jarvis_anomaly_recent",
        "jarvis_preflight",
        "jarvis_prop_firm_status",
        "jarvis_prop_firm_evaluate",
        "jarvis_prop_firm_killall",
    }
    declared = {t["name"] for t in jarvis_mcp_server.list_tools()}
    assert declared == expected, f"missing={expected - declared} extras={declared - expected}"
    assert len(declared) == 42


# ---------------------------------------------------------------------------
# 13. jarvis_subscribe_events (Track 1: real-time event stream)
# ---------------------------------------------------------------------------


def test_subscribe_events_polls_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two-poll cursor pattern: first sees existing, second sees only new."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_trace = tmp_path / "jarvis_trace.jsonl"
    streams = {**jarvis_mcp_server._EVENT_STREAMS, "trace": fake_trace}
    monkeypatch.setattr(jarvis_mcp_server, "_EVENT_STREAMS", streams)

    with fake_trace.open("w", encoding="utf-8") as fh:
        fh.write('{"consult_id":"a","bot_id":"bot1"}\n')
        fh.write('{"consult_id":"b","bot_id":"bot2"}\n')

    r1 = jarvis_mcp_server._tool_subscribe_events(
        {"stream": "trace", "since_offset": 0},
    )
    assert [e["consult_id"] for e in r1["events"]] == ["a", "b"]
    assert r1["exhausted"] is True
    assert r1["next_offset"] == r1["file_size"]

    # Append a third record after first poll.
    with fake_trace.open("a", encoding="utf-8") as fh:
        fh.write('{"consult_id":"c","bot_id":"bot1"}\n')

    r2 = jarvis_mcp_server._tool_subscribe_events(
        {"stream": "trace", "since_offset": r1["next_offset"]},
    )
    assert [e["consult_id"] for e in r2["events"]] == ["c"]


def test_subscribe_events_applies_bot_id_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """filters.bot_id keeps only matching records; cursor still advances past all."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_trace = tmp_path / "jarvis_trace.jsonl"
    streams = {**jarvis_mcp_server._EVENT_STREAMS, "trace": fake_trace}
    monkeypatch.setattr(jarvis_mcp_server, "_EVENT_STREAMS", streams)

    with fake_trace.open("w", encoding="utf-8") as fh:
        fh.write('{"consult_id":"a","bot_id":"alpha"}\n')
        fh.write('{"consult_id":"b","bot_id":"beta"}\n')
        fh.write('{"consult_id":"c","bot_id":"alpha"}\n')

    out = jarvis_mcp_server._tool_subscribe_events(
        {
            "stream": "trace",
            "since_offset": 0,
            "filters": {"bot_id": "alpha"},
        },
    )
    assert [e["consult_id"] for e in out["events"]] == ["a", "c"]


def test_subscribe_events_limit_does_not_skip_inline_stream_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-trace streams keep the cursor after the last returned record."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_dashboard = tmp_path / "dashboard_events.jsonl"
    streams = {**jarvis_mcp_server._EVENT_STREAMS, "dashboard": fake_dashboard}
    monkeypatch.setattr(jarvis_mcp_server, "_EVENT_STREAMS", streams)

    with fake_dashboard.open("w", encoding="utf-8") as fh:
        fh.write('{"event":"a"}\n')
        fh.write('{"event":"b"}\n')
        fh.write('{"event":"c"}\n')

    first = jarvis_mcp_server._tool_subscribe_events(
        {"stream": "dashboard", "since_offset": 0, "limit": 2},
    )
    assert [e["event"] for e in first["events"]] == ["a", "b"]
    assert first["next_offset"] < first["file_size"]

    second = jarvis_mcp_server._tool_subscribe_events(
        {
            "stream": "dashboard",
            "since_offset": first["next_offset"],
            "limit": 10,
        },
    )
    assert [e["event"] for e in second["events"]] == ["c"]
    assert second["next_offset"] == second["file_size"]


def test_subscribe_events_unknown_stream_returns_error_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown stream name returns an error field but does not raise."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    monkeypatch.setattr(jarvis_mcp_server, "_EVENT_STREAMS", {"trace": tmp_path / "t.jsonl"})

    out = jarvis_mcp_server._tool_subscribe_events(
        {"stream": "not_a_real_stream", "since_offset": 0},
    )
    assert out["events"] == []
    assert "error" in out
    assert "unknown_stream" in out["error"]


# ---------------------------------------------------------------------------
# 14. Hermes override write-back tools
# ---------------------------------------------------------------------------


def test_size_modifier_tool_validates_and_dispatches(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jarvis_set_size_modifier rejects bad input and dispatches valid pins."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    calls: list[dict[str, Any]] = []

    def fake_apply(
        bot_id: str,
        modifier: float,
        reason: str,
        ttl_minutes: int,
    ) -> dict[str, Any]:
        calls.append(
            {
                "bot_id": bot_id,
                "modifier": modifier,
                "reason": reason,
                "ttl_minutes": ttl_minutes,
            },
        )
        return {"status": "APPLIED", "bot_id": bot_id, "modifier": modifier}

    monkeypatch.setattr(jarvis_mcp_server, "_call_apply_size_modifier", fake_apply)

    rejected = _call(
        "jarvis_set_size_modifier",
        {"_auth": "test-token", "modifier": 0.5, "reason": "missing bot"},
    )
    assert rejected["ok"] is True
    assert rejected["data"]["status"] == "REJECTED"
    assert rejected["data"]["reason"] == "missing_bot_id"

    accepted = _call(
        "jarvis_set_size_modifier",
        {
            "_auth": "test-token",
            "bot_id": "mnq_floor",
            "modifier": 0.3,
            "reason": "drawdown response",
            "ttl_minutes": 60,
        },
    )
    assert accepted["ok"] is True
    assert accepted["data"]["status"] == "APPLIED"
    assert calls == [
        {
            "bot_id": "mnq_floor",
            "modifier": 0.3,
            "reason": "drawdown response",
            "ttl_minutes": 60,
        },
    ]


def test_clear_override_tool_dispatches_without_side_effects(
    patched_paths: dict[str, Path],
    mock_underlying: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jarvis_clear_override passes one operator clear request to the override API."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    calls: list[dict[str, Any]] = []

    def fake_clear(
        bot_id: str | None,
        asset: str | None,
        school: str | None,
    ) -> dict[str, Any]:
        calls.append({"bot_id": bot_id, "asset": asset, "school": school})
        return {"status": "REMOVED", "kind": "size_modifier", "bot_id": bot_id}

    monkeypatch.setattr(jarvis_mcp_server, "_call_clear_override", fake_clear)

    result = _call(
        "jarvis_clear_override",
        {"_auth": "test-token", "bot_id": "mnq_floor"},
    )
    assert result["ok"] is True
    assert result["data"]["status"] == "REMOVED"
    assert calls == [{"bot_id": "mnq_floor", "asset": None, "school": None}]


# ---------------------------------------------------------------------------
# anomaly_watcher MCP tools — operator-friendly Telegram replacements
# ---------------------------------------------------------------------------


def test_anomaly_scan_tool_returns_envelope(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jarvis_anomaly_scan wraps the watcher's hit list in {n_new, hits, asof}."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_hits = [
        {
            "asof": "2026-05-12T13:00:00+00:00",
            "pattern": "loss_streak",
            "key": "loss_streak:bleeder:4",
            "bot_id": "bleeder",
            "severity": "warn",
            "detail": "bleeder has 4 consecutive losses",
            "suggested_skill": "jarvis-anomaly-investigator",
            "extras": {"streak": 4, "last_n_trades": []},
        },
    ]
    monkeypatch.setattr(jarvis_mcp_server, "_call_anomaly_scan", lambda: fake_hits)

    result = _call("jarvis_anomaly_scan", {"_auth": "test-token"})
    assert result["ok"] is True
    assert result["data"]["n_new"] == 1
    assert result["data"]["hits"] == fake_hits
    assert "asof" in result["data"]


def test_anomaly_scan_tool_empty_when_no_hits(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty hit list still returns a clean envelope."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    monkeypatch.setattr(jarvis_mcp_server, "_call_anomaly_scan", lambda: [])

    result = _call("jarvis_anomaly_scan", {"_auth": "test-token"})
    assert result["ok"] is True
    assert result["data"]["n_new"] == 0
    assert result["data"]["hits"] == []


def test_anomaly_recent_tool_defaults_to_24h(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jarvis_anomaly_recent defaults since_hours=24 when omitted."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    received: list[int] = []

    def fake_recent(since_hours: int) -> list[dict[str, Any]]:
        received.append(since_hours)
        return [{"pattern": "loss_streak", "bot_id": "x"}]

    monkeypatch.setattr(jarvis_mcp_server, "_call_anomaly_recent", fake_recent)

    result = _call("jarvis_anomaly_recent", {"_auth": "test-token"})
    assert result["ok"] is True
    assert result["data"]["since_hours"] == 24
    assert result["data"]["n"] == 1
    assert received == [24]


def test_anomaly_recent_tool_passes_since_hours(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom since_hours propagates to the underlying call."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    received: list[int] = []

    def fake_recent(since_hours: int) -> list[dict[str, Any]]:
        received.append(since_hours)
        return []

    monkeypatch.setattr(jarvis_mcp_server, "_call_anomaly_recent", fake_recent)

    result = _call(
        "jarvis_anomaly_recent",
        {"_auth": "test-token", "since_hours": 72},
    )
    assert result["ok"] is True
    assert result["data"]["since_hours"] == 72
    assert received == [72]


def test_anomaly_recent_tool_rejects_garbage_since(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid since_hours coerces to the 24h default, doesn't crash."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    monkeypatch.setattr(jarvis_mcp_server, "_call_anomaly_recent", lambda since_hours: [])

    result = _call(
        "jarvis_anomaly_recent",
        {"_auth": "test-token", "since_hours": "not-a-number"},
    )
    assert result["ok"] is True
    assert result["data"]["since_hours"] == 24


# ---------------------------------------------------------------------------
# jarvis_preflight — live-cutover Go/No-Go
# ---------------------------------------------------------------------------


def test_preflight_tool_passes_through_full_report(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jarvis_preflight returns the verdict + per-check breakdown verbatim."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_report = {
        "asof": "2026-05-12T23:00:00+00:00",
        "verdict": "READY",
        "n_pass": 12,
        "n_warn": 0,
        "n_fail": 0,
        "checks": [
            {"name": "workspace_writable", "status": "PASS", "detail": "ok", "extras": {}},
        ],
    }
    monkeypatch.setattr(jarvis_mcp_server, "_call_preflight", lambda: fake_report)

    result = _call("jarvis_preflight", {"_auth": "test-token"})
    assert result["ok"] is True
    assert result["data"]["verdict"] == "READY"
    assert result["data"]["n_pass"] == 12
    assert result["data"]["checks"][0]["name"] == "workspace_writable"


def test_preflight_tool_returns_not_ready_envelope(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the underlying preflight reports NOT READY, the tool passes it on."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_report = {
        "asof": "2026-05-12T23:00:00+00:00",
        "verdict": "NOT READY",
        "n_pass": 10,
        "n_warn": 1,
        "n_fail": 1,
        "checks": [
            {
                "name": "kill_switch_disengaged",
                "status": "FAIL",
                "detail": "kill_all engaged",
                "extras": {},
            }
        ],
    }
    monkeypatch.setattr(jarvis_mcp_server, "_call_preflight", lambda: fake_report)

    result = _call("jarvis_preflight", {"_auth": "test-token"})
    assert result["ok"] is True
    assert result["data"]["verdict"] == "NOT READY"
    assert result["data"]["n_fail"] == 1


# ---------------------------------------------------------------------------
# Prop firm guardrail tools — elite-level rule enforcement
# ---------------------------------------------------------------------------


def test_prop_firm_status_envelope(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jarvis_prop_firm_status returns aggregate counts + per-account snapshots."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_snaps = [
        {"severity": "blown", "rules": {"account_id": "x"}, "state": {}},
        {"severity": "warn", "rules": {"account_id": "y"}, "state": {}},
        {"severity": "ok", "rules": {"account_id": "z"}, "state": {}},
    ]
    monkeypatch.setattr(jarvis_mcp_server, "_call_prop_firm_status", lambda: fake_snaps)

    result = _call("jarvis_prop_firm_status", {"_auth": "test-token"})
    assert result["ok"] is True
    assert result["data"]["n_accounts"] == 3
    assert result["data"]["n_critical_or_blown"] == 1
    assert result["data"]["n_warn"] == 1


def test_prop_firm_evaluate_rejects_missing_account(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No account_id → default-deny verdict."""

    result = _call("jarvis_prop_firm_evaluate", {"_auth": "test-token", "signal": {}})
    assert result["ok"] is True
    assert result["data"]["allowed"] is False
    assert "missing" in result["data"]["reason"].lower()


def test_prop_firm_evaluate_rejects_garbage_signal(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal must be a dict; non-dict input is default-denied."""

    result = _call(
        "jarvis_prop_firm_evaluate",
        {"_auth": "test-token", "account_id": "x", "signal": "not-a-dict"},
    )
    assert result["ok"] is True
    assert result["data"]["allowed"] is False
    assert "dict" in result["data"]["reason"].lower()


def test_prop_firm_evaluate_passes_through_underlying_verdict(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid input → call goes to underlying evaluate(), result envelope returned."""
    from eta_engine.mcp_servers import jarvis_mcp_server

    fake_verdict = {
        "allowed": True,
        "reason": "all rules pass",
        "blockers": [],
        "headroom": {"daily_loss_remaining_usd": 1500.0},
        "worst_case_loss_usd": 40.0,
        "asof": "2026-05-12T23:00:00+00:00",
    }

    received: list[tuple[str, dict]] = []

    def fake_eval(account_id: str, signal: dict) -> dict:
        received.append((account_id, signal))
        return fake_verdict

    monkeypatch.setattr(jarvis_mcp_server, "_call_prop_firm_evaluate", fake_eval)

    result = _call(
        "jarvis_prop_firm_evaluate",
        {
            "_auth": "test-token",
            "account_id": "blusky-50K-launch",
            "signal": {"symbol": "MNQ", "stop_r": 1.0, "size": 2},
        },
    )
    assert result["ok"] is True
    assert result["data"]["allowed"] is True
    assert received == [("blusky-50K-launch", {"symbol": "MNQ", "stop_r": 1.0, "size": 2})]


def test_prop_firm_killall_requires_reason(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Killall must include a reason — audit trail is non-optional."""

    result = _call("jarvis_prop_firm_killall", {"_auth": "test-token"})
    assert result["ok"] is True
    assert result["data"]["status"] == "REJECTED"
    assert "reason" in result["data"]["error"].lower()


def test_prop_firm_killall_writes_kill_state(
    patched_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a reason, killall flips hermes_state.kill_all → True."""

    result = _call(
        "jarvis_prop_firm_killall",
        {"_auth": "test-token", "reason": "approaching daily loss limit"},
    )
    assert result["ok"] is True
    assert result["data"]["status"] == "KILL_SWITCH_ENGAGED"
    state_file = patched_paths["hermes_state"]
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["kill_all"] is True
    assert "approaching daily loss" in state["reason"]
    assert state["source"] == "prop_firm_guardrails"
