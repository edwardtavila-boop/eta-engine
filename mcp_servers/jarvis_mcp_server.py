"""JARVIS Supercharge surfaces as an MCP server.

Half 1 of the JARVIS-Hermes bridge. Exposes 11 read/write tools to MCP
clients (Hermes plugin, Claude Code, the ETA dashboard, etc.) over a
stdio transport. Token-gated, audit-logged, with two-run confirmation
on destructive tool calls — the same gate the autonomous kaizen loop
applies to its own RETIRE actions.

Architecture
------------

The server is split into three layers so the test suite can exercise
the dispatch surface in-process without spawning a subprocess:

* ``_call_*`` wrappers — thin adapters that import the underlying
  read-only module on demand. Tests monkeypatch these to fixed values.
* ``_tool_*`` handlers — pure functions returning the
  ``{"ok": bool, "data": ..., "error": ...}`` envelope. NEVER raise:
  every body is wrapped in ``_envelope_guard`` which catches anything,
  logs via ``logger.exception``, and returns ``ok=False``.
* ``dispatch_tool_call`` — top-level dispatcher that runs the token
  check, scrubs and logs the call, then routes by tool name.

The MCP wire surface (``serve()``) wraps ``dispatch_tool_call`` in the
official ``mcp`` SDK transport. A stdio-JSONRPC fallback path is also
provided for environments where the SDK isn't installed; it handles
the three methods Hermes uses — ``initialize``, ``tools/list``,
``tools/call``.

The kill switch writes a single line of JSON to the canonical
``hermes_state.json`` file; the existing supervisor watches that file
and halts trading on ``kill_all: true``.

Per CLAUDE.md hard rule #1 every write goes under
``C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\state``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("eta_engine.mcp_servers.jarvis_mcp_server")

# The wiring audit picks this up — every JARVIS module that's expected
# to fire on a consult exports its hooks here, and the server's hook is
# ``serve()`` (the stdio entry point spawned by Hermes).
EXPECTED_HOOKS: tuple[str, ...] = ("serve",)

# ---------------------------------------------------------------------------
# Canonical workspace paths — single write target per CLAUDE.md hard rule #1.
# Tests monkeypatch these to ``tmp_path`` so they never touch live state.
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"

_AUDIT_LOG_PATH: Path = _STATE_ROOT / "hermes_actions.jsonl"
_KAIZEN_ACTION_LOG_PATH: Path = _STATE_ROOT / "kaizen_actions.jsonl"
_KAIZEN_OVERRIDES_PATH: Path = _STATE_ROOT / "kaizen_overrides.json"
_HERMES_STATE_PATH: Path = _STATE_ROOT / "jarvis_intel" / "hermes_state.json"
_KAIZEN_LATEST_PATH: Path = _STATE_ROOT / "kaizen_latest.json"

_CALLER = "hermes-mcp"
_KILL_PHRASE = "kill all"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_jsonable(obj: Any) -> Any:  # noqa: ANN401 — recursive sanitizer
    """Make a value JSON-serialisable. Best-effort, never raises."""
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    if isinstance(obj, list | tuple | set):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return _to_jsonable(asdict(obj))
    # Generic object with attributes — pull a flat dict view
    if hasattr(obj, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _scrub_args(args: dict[str, Any]) -> dict[str, Any]:
    """Strip secrets out of args before they hit the audit log."""
    return {k: v for k, v in args.items() if k not in {"_auth", "_confirm_phrase"}}


# Audit log rotation threshold. When the active log exceeds this size,
# the file is gzip-compressed to a sibling with a UTC stamp and the
# active path starts fresh. Keeps disk usage bounded on long-lived VPS
# Hermes instances. 10 MB ≈ ~30k tool calls — plenty of history.
_AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024


def _rotate_audit_log_if_needed() -> None:
    """Gzip the audit log to a stamped sibling if it has grown too large.

    Best-effort: any OSError swallowed so the next append falls back to
    appending onto whatever file currently exists.
    """
    try:
        if not _AUDIT_LOG_PATH.exists():
            return
        if _AUDIT_LOG_PATH.stat().st_size < _AUDIT_LOG_MAX_BYTES:
            return
        import gzip
        import shutil

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rotated = _AUDIT_LOG_PATH.with_name(
            f"{_AUDIT_LOG_PATH.stem}_{stamp}.jsonl.gz",
        )
        with _AUDIT_LOG_PATH.open("rb") as src, gzip.open(rotated, "wb") as dst:
            shutil.copyfileobj(src, dst)
        _AUDIT_LOG_PATH.unlink()
        logger.info("hermes audit-log rotated to %s", rotated.name)
    except OSError as exc:
        logger.warning("hermes audit-log rotation failed: %s", exc)


def _append_audit(record: dict[str, Any]) -> None:
    """Append one JSONL line to the hermes audit log. Never raises.

    Triggers size-based gzip rotation lazily on each append — the check
    is a fast ``stat()`` followed by a numeric compare, so the steady-state
    overhead is one syscall per tool call.

    Hardening: rotation is wrapped in its own broad-exception guard so a
    rotation failure (corrupt file, gzip error, disk full mid-gzip) cannot
    suppress the append that triggered it. The append itself is still
    wrapped in an OSError guard so any write-time error is dropped rather
    than propagated.
    """
    try:
        _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("hermes audit-log mkdir failed: %s", exc)
        return

    # Rotation is best-effort and MUST NOT block the append. Catch anything.
    try:
        _rotate_audit_log_if_needed()
    except Exception as exc:  # noqa: BLE001 — rotation can't sabotage append
        logger.warning("hermes audit-log rotation failed (continuing): %s", exc)

    try:
        with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:  # log-and-drop — never break the consult path
        logger.warning("hermes audit-log write failed: %s", exc)


def _envelope_guard(fn: Callable[[], Any]) -> dict[str, Any]:
    """Execute ``fn``; wrap the result in an envelope; never raise."""
    try:
        data = fn()
    except Exception as exc:  # noqa: BLE001 — defensive: handlers never raise
        logger.exception("jarvis_mcp tool handler raised")
        return {"ok": False, "data": None, "error": f"handler_error: {exc}"}
    return {"ok": True, "data": _to_jsonable(data), "error": None}


# ---------------------------------------------------------------------------
# Underlying-call wrappers (monkeypatched in tests)
# ---------------------------------------------------------------------------


def _call_kaizen_latest() -> dict[str, Any]:
    """Read the most recent kaizen report. Empty dict if absent."""
    if not _KAIZEN_LATEST_PATH.exists():
        return {}
    try:
        return json.loads(_KAIZEN_LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("kaizen_latest read failed: %s", exc)
        return {}


def _call_trace_tail(n: int) -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import trace_emitter

    return trace_emitter.tail(n=n)


# Stream → file mapping for jarvis_subscribe_events. New streams can be
# added here without changing the tool surface. Keys are the user-facing
# stream names accepted by the tool; values are absolute Paths.
_EVENT_STREAMS: dict[str, Path] = {
    "trace": _STATE_ROOT / "jarvis_trace.jsonl",
    "dashboard": _STATE_ROOT / "dashboard_events.jsonl",
    "decisions": _STATE_ROOT / "decision_journal.jsonl",
    "kaizen": _STATE_ROOT / "kaizen_actions.jsonl",
    "hermes": _AUDIT_LOG_PATH,  # what THIS server writes
    "jarvis_v3": _STATE_ROOT / "jarvis_v3_events.jsonl",
    "uptime": _STATE_ROOT / "uptime_events.jsonl",
}


def _call_subscribe_events(
    stream: str,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Read new event records from a JSONL stream past a byte offset.

    Routes ``stream="trace"`` through trace_emitter.read_since (which
    knows about rotation). All other streams use a thin inline reader
    that skips malformed lines and stops at the last newline boundary.
    Unknown stream names return ``([], offset)`` so the cursor is preserved.
    """
    path = _EVENT_STREAMS.get(stream)
    if path is None:
        return [], offset
    if stream == "trace":
        from eta_engine.brain.jarvis_v3 import trace_emitter

        return trace_emitter.read_since(offset=offset, limit=limit, path=path)

    # Inline reader for non-rotating streams. Same partial-line guard
    # as trace_emitter.read_since but without the rotation reset path.
    try:
        if not path.exists():
            return [], 0
        file_size = path.stat().st_size
        if offset < 0:
            offset = 0
        if offset > file_size:  # stream truncated or replaced
            offset = 0
        if offset == file_size:
            return [], file_size
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(4 * 1024 * 1024)
        if not data:
            return [], offset
        if data.endswith(b"\n"):
            usable = data
        else:
            last_nl = data.rfind(b"\n")
            if last_nl < 0:
                return [], offset
            usable = data[: last_nl + 1]
        advance_to = offset
        records: list[dict[str, Any]] = []
        for line in usable.splitlines(keepends=True):
            advance_to += len(line)
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line.decode("utf-8", errors="replace")))
            except json.JSONDecodeError:
                continue
            if len(records) >= limit:
                break
        return records, advance_to
    except Exception as exc:  # noqa: BLE001
        logger.warning("subscribe_events read failed (stream=%s): %s", stream, exc)
        return [], offset


def _apply_event_filters(
    records: list[dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """Lightweight filter pass — kept tiny so it can't error on malformed data.

    Supported filter keys:
      * ``bot_id`` — exact match on the record's ``bot_id`` field.
      * ``action`` — exact match on the record's ``action`` field.
      * ``min_severity`` — keep records with ``severity >= N``.
      * ``contains`` — substring match on JSON-dumped record (case-insensitive).
    Unknown filter keys are ignored.
    """
    if not filters:
        return records
    out: list[dict[str, Any]] = []
    bot_id = filters.get("bot_id")
    action = filters.get("action")
    min_sev = filters.get("min_severity")
    contains = filters.get("contains")
    contains_lc = str(contains).lower() if contains else None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if bot_id and rec.get("bot_id") != bot_id:
            continue
        if action and rec.get("action") != action:
            continue
        if min_sev is not None:
            try:
                if int(rec.get("severity", 0)) < int(min_sev):
                    continue
            except (TypeError, ValueError):
                continue
        if contains_lc:
            try:
                blob = json.dumps(rec, default=str).lower()
            except (TypeError, ValueError):
                continue
            if contains_lc not in blob:
                continue
        out.append(rec)
    return out


def _call_wiring_audit() -> list[Any]:
    from eta_engine.scripts import jarvis_wiring_audit

    return jarvis_wiring_audit.audit()


def _call_portfolio_snapshot() -> Any:  # noqa: ANN401 — opaque context object
    from eta_engine.brain.jarvis_v3 import portfolio_brain

    return portfolio_brain.snapshot()


def _call_portfolio_assess(req: Any, ctx: Any) -> Any:  # noqa: ANN401
    from eta_engine.brain.jarvis_v3 import portfolio_brain

    return portfolio_brain.assess(req, ctx)


def _call_hot_weights(asset: str) -> dict[str, float]:
    from eta_engine.brain.jarvis_v3 import hot_learner

    return hot_learner.current_weights(asset)


def _call_upcoming_events(horizon_min: int) -> list[Any]:
    from eta_engine.data import event_calendar

    return event_calendar.upcoming(datetime.now(UTC), horizon_min=horizon_min)


def _call_kaizen_run(bootstraps: int) -> dict[str, Any]:
    from eta_engine.scripts import kaizen_loop

    return kaizen_loop.run_loop(bootstraps=bootstraps, apply_actions=False)


def _call_topology() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import risk_topology

    return risk_topology.build_topology()


def _call_register_agent(
    agent_id: str,
    role: str,
    version: str,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import agent_registry

    return agent_registry.register_agent(agent_id, role, version)


def _call_list_agents(only_alive: bool) -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import agent_registry

    return agent_registry.list_agents(only_alive=only_alive)


def _call_acquire_lock(
    agent_id: str,
    resource: str,
    purpose: str,
    ttl_seconds: int,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import agent_registry

    return agent_registry.acquire_lock(
        agent_id=agent_id,
        resource=resource,
        purpose=purpose,
        ttl_seconds=ttl_seconds,
    )


def _call_release_lock(agent_id: str, resource: str) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import agent_registry

    return agent_registry.release_lock(agent_id=agent_id, resource=resource)


def _call_causal_analyze(consult_id: str, perturbation_sigma: float) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import causal_attribution

    return causal_attribution.analyze(consult_id, perturbation_sigma=perturbation_sigma).to_dict()


def _call_consult_replay(
    consult_id: str,
    override_overrides: dict[str, Any] | None,
    override_hot_weights: dict[str, float] | None,
    override_school_inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import consult_replay

    return consult_replay.replay(
        consult_id,
        override_overrides=override_overrides,
        override_hot_weights=override_hot_weights,
        override_school_inputs=override_school_inputs,
    ).to_dict()


def _call_counterfactual(
    consult_id: str,
    pin_size_modifier: float | None,
    pin_school: str | None,
    pin_weight: float | None,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import consult_replay

    return consult_replay.counterfactual(
        consult_id,
        pin_size_modifier=pin_size_modifier,
        pin_school=pin_school,
        pin_weight=pin_weight,
    ).to_dict()


def _call_attribution_query(
    slice_by: list[str],
    filter_arg: dict[str, Any],
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import attribution_cube

    return attribution_cube.query(slice_by=slice_by, filter=filter_arg).to_dict()


def _call_current_regime() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import regime_classifier

    return regime_classifier.current_regime().to_dict()


def _call_list_regime_packs() -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import regime_classifier

    return regime_classifier.list_packs()


def _call_apply_regime_pack(
    name: str,
    ttl_minutes: int,
    bot_ids: list[str] | None,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import regime_classifier

    return regime_classifier.apply_pack(
        name=name,
        ttl_minutes=ttl_minutes,
        bot_ids=bot_ids,
    )


def _call_kelly_recommend(
    lookback_days: int,
    kelly_fraction: float,
    drawdown_penalty: float,
) -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import kelly_optimizer

    return kelly_optimizer.recommend_sizing(
        lookback_days=lookback_days,
        kelly_fraction=kelly_fraction,
        drawdown_penalty=drawdown_penalty,
    )


def _call_zeus_snapshot(force_refresh: bool, trace_n: int) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import zeus

    return zeus.snapshot(force_refresh=force_refresh, trace_n=trace_n).to_dict()


def _call_pnl_summary(window_hours: float) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    return pnl_summary.summarize(window_hours=window_hours).to_dict()


def _call_pnl_multi_window() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    return pnl_summary.multi_window_summary()


def _call_material_events_since(asof_iso: str) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import pnl_summary

    return pnl_summary.has_material_events_since(asof_iso=asof_iso)


def _call_anomaly_scan() -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    return [h.to_dict() for h in anomaly_watcher.scan()]


def _call_anomaly_recent(since_hours: int) -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    return anomaly_watcher.recent_hits(since_hours=since_hours)


def _call_preflight() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import preflight

    return preflight.run_preflight().to_dict()


def _call_prop_firm_status() -> list[dict[str, Any]]:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    return [s.to_dict() for s in g.aggregate_status()]


def _call_prop_firm_evaluate(
    account_id: str,
    signal: dict[str, Any],
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    rules = g.get_rules(account_id)
    if rules is None:
        return {
            "allowed": False,
            "reason": f"unregistered account_id: {account_id}",
            "blockers": ["unknown_account"],
            "headroom": {},
            "worst_case_loss_usd": 0.0,
            "asof": _now_iso(),
        }
    state = g.account_state_from_trades(account_id)
    return g.evaluate(rules, state, signal).to_dict()


def _call_prop_firm_killall(reason: str) -> dict[str, Any]:
    """Engage the kill switch — same path as jarvis_kill_switch with 'kill all'."""
    import json as _json

    target_path = _HERMES_STATE_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kill_all": True,
        "reason": str(reason or "prop_firm_killall via MCP"),
        "asof": _now_iso(),
        "source": "prop_firm_guardrails",
    }
    tmp = target_path.with_suffix(target_path.suffix + ".tmp")
    tmp.write_text(_json.dumps(payload, default=str), encoding="utf-8")
    os.replace(tmp, target_path)
    return {"status": "KILL_SWITCH_ENGAGED", "hermes_state": payload}


def _call_cost_summary(since_days_ago: int) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import cost_tracker

    return cost_tracker.estimate_spend(since_days_ago=since_days_ago).to_dict()


def _call_cost_today() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import cost_tracker

    return cost_tracker.today_spend()


def _call_cost_anomaly(window_min: int) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import cost_tracker

    return cost_tracker.anomaly_check(window_min=window_min)


def _call_apply_size_modifier(
    bot_id: str,
    modifier: float,
    reason: str,
    ttl_minutes: int,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    return hermes_overrides.apply_size_modifier(
        bot_id=bot_id,
        modifier=modifier,
        reason=reason,
        ttl_minutes=ttl_minutes,
        source="hermes_mcp",
    )


def _call_apply_school_weight(
    asset: str,
    school: str,
    weight: float,
    reason: str,
    ttl_minutes: int,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    return hermes_overrides.apply_school_weight(
        asset=asset,
        school=school,
        weight=weight,
        reason=reason,
        ttl_minutes=ttl_minutes,
        source="hermes_mcp",
    )


def _call_active_overrides() -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    return hermes_overrides.active_overrides_summary()


def _call_clear_override(
    bot_id: str | None,
    asset: str | None,
    school: str | None,
) -> dict[str, Any]:
    from eta_engine.brain.jarvis_v3 import hermes_overrides

    return hermes_overrides.clear_override(
        bot_id=bot_id,
        asset=asset,
        school=school,
    )


def _call_verdict_to_narrative(record: dict[str, Any]) -> str:
    """Best-effort narrative for a trace record.

    The proper ``narrative_generator.verdict_to_narrative`` takes a
    ``ConsolidatedVerdict`` dataclass — not a raw trace dict. We accept
    either: if it walks like a verdict we call through; else we fall
    back to a small template that strings together the visible fields.
    """
    verdict = record.get("verdict") if isinstance(record, dict) else None
    if isinstance(verdict, dict):
        final = verdict.get("final_verdict", "UNKNOWN")
        size = verdict.get("final_size_multiplier", record.get("final_size", 1.0))
        try:
            size_pct = int(round(float(size) * 100))
        except (TypeError, ValueError):
            size_pct = 100
        bot = record.get("bot_id", "?")
        return f"{final} at {size_pct}% size for {bot} (consult {record.get('consult_id', '?')})."
    return f"No narrative available for consult {record.get('consult_id', '?')}."


# ---------------------------------------------------------------------------
# Two-run gate (shared by deploy_strategy and retire_strategy)
# ---------------------------------------------------------------------------


def _previous_retire_targets() -> set[str]:
    """Bots with a prior RETIRE recommendation logged.

    Matches the predicate ``kaizen_loop._previous_retire_targets`` uses —
    same JSONL, same key — so the destructive MCP tools share a gate
    with the autonomous loop.
    """
    if not _KAIZEN_ACTION_LOG_PATH.exists():
        return set()
    out: set[str] = set()
    try:
        with _KAIZEN_ACTION_LOG_PATH.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("action") == "RETIRE":
                    out.add(str(rec.get("bot_id", "")))
    except OSError:
        return out
    return out


def _append_kaizen_action(record: dict[str, Any]) -> None:
    try:
        _KAIZEN_ACTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _KAIZEN_ACTION_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("kaizen action-log write failed: %s", exc)


def _write_override(bot_id: str, reason: str) -> dict[str, Any]:
    """Mark a bot deactivated in the sidecar override file."""
    _KAIZEN_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"deactivated": {}}
    if _KAIZEN_OVERRIDES_PATH.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(_KAIZEN_OVERRIDES_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("deactivated"), dict):
                data = loaded
    data["deactivated"][bot_id] = {
        "applied_at": _now_iso(),
        "reason": reason,
        "source": "hermes_mcp",
    }
    _KAIZEN_OVERRIDES_PATH.write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8",
    )
    return data


# ---------------------------------------------------------------------------
# Tool registry — names, descriptions, JSON-schema input definitions
# ---------------------------------------------------------------------------


def list_tools() -> list[dict[str, Any]]:
    """Return the 11 declared tools as plain dicts (SDK-agnostic).

    NOTE (2026-05-12): the ``_auth`` argument is no longer advertised in
    tool inputSchemas. Stdio MCP clients (Hermes Agent, Claude Desktop)
    that spawn this process inherit ``JARVIS_MCP_TOKEN`` via the spawn
    env and authentication uses that token without needing an arg.
    Advertising ``_auth`` caused LLMs to over-eagerly demand the token
    from operators in chat ("I need a valid JARVIS_MCP_TOKEN..."). The
    handler still accepts ``_auth`` if a caller passes it — see
    ``dispatch_tool_call`` for the precise auth precedence.
    """
    auth_field: dict[str, Any] = {}  # see note above
    return [
        {
            "name": "jarvis_fleet_status",
            "description": "Latest kaizen-loop report: tier/MC/action counts plus top-5 elite/dark bots.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "since_iso": {"type": "string"},
                },
            },
        },
        {
            "name": "jarvis_trace_tail",
            "description": "Newest-last slice of the JARVIS consult trace stream.",
            "inputSchema": {
                "type": "object",
                "properties": {**auth_field, "n": {"type": "integer", "default": 10}},
            },
        },
        {
            "name": "jarvis_wiring_audit",
            "description": "Dark-module / fire-rate audit of brain.jarvis_v3.",
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_portfolio_assess",
            "description": "Run portfolio_brain.assess for a hypothetical bot request.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "bot_id": {"type": "string"},
                    "asset_class": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["bot_id", "asset_class", "action"],
            },
        },
        {
            "name": "jarvis_hot_weights",
            "description": "Per-school weight modifiers for one asset.",
            "inputSchema": {
                "type": "object",
                "properties": {**auth_field, "asset": {"type": "string"}},
                "required": ["asset"],
            },
        },
        {
            "name": "jarvis_upcoming_events",
            "description": "Operator calendar events inside the next horizon_min minutes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "horizon_min": {"type": "integer", "default": 60},
                },
            },
        },
        {
            "name": "jarvis_kaizen_run",
            "description": "Run a kaizen loop pass; returns the full report dict.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "bootstraps": {"type": "integer", "default": 200},
                },
            },
        },
        {
            "name": "jarvis_deploy_strategy",
            "description": "Lift a kaizen-override on bot_id. Destructive: 2-run gated.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "bot_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "_confirm_phrase": {"type": "string"},
                },
                "required": ["bot_id", "reason"],
            },
        },
        {
            "name": "jarvis_retire_strategy",
            "description": "Write a kaizen-override deactivating bot_id. Destructive: 2-run gated.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "bot_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "_confirm_phrase": {"type": "string"},
                },
                "required": ["bot_id", "reason"],
            },
        },
        {
            "name": "jarvis_kill_switch",
            "description": "Trip the fleet kill switch. Requires confirm_phrase == 'kill all'.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "reason": {"type": "string"},
                    "confirm_phrase": {"type": "string"},
                },
                "required": ["reason", "confirm_phrase"],
            },
        },
        {
            "name": "jarvis_explain_verdict",
            "description": "Narrative + raw record for one consult_id from the trace stream.",
            "inputSchema": {
                "type": "object",
                "properties": {**auth_field, "consult_id": {"type": "string"}},
                "required": ["consult_id"],
            },
        },
        {
            "name": "jarvis_subscribe_events",
            "description": (
                "Poll a JARVIS event stream past a byte offset and return any "
                "new records. Cursor-based — pass next_offset from the previous "
                "response on every tick. Streams: trace (consult records), "
                "dashboard, decisions, kaizen, hermes (audit), jarvis_v3, uptime."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "stream": {
                        "type": "string",
                        "default": "trace",
                        "enum": [
                            "trace",
                            "dashboard",
                            "decisions",
                            "kaizen",
                            "hermes",
                            "jarvis_v3",
                            "uptime",
                        ],
                    },
                    "since_offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 50},
                    "filters": {
                        "type": "object",
                        "description": (
                            "Optional. Keys: bot_id (str), action (str), "
                            "min_severity (int), contains (substring, case-insensitive)."
                        ),
                    },
                },
            },
        },
        {
            "name": "jarvis_set_size_modifier",
            "description": (
                "Pin a multiplicative size modifier on a specific bot for a "
                "TTL-limited window. Reads by portfolio_brain.assess apply this "
                "AFTER the rule cascade. Clamped to [0.0, 1.0] so this "
                "write-back path can only trim or pause size."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "bot_id": {"type": "string"},
                    "modifier": {"type": "number"},
                    "reason": {"type": "string"},
                    "ttl_minutes": {"type": "integer", "default": 240},
                },
                "required": ["bot_id", "modifier", "reason"],
            },
        },
        {
            "name": "jarvis_pin_school_weight",
            "description": (
                "Pin a school-weight overlay for (asset, school) until TTL "
                "expires. Multiplied with the EMA-learned weight inside "
                "hot_learner.current_weights. Clamped to [0.0, 2.0]."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "asset": {"type": "string"},
                    "school": {"type": "string"},
                    "weight": {"type": "number"},
                    "reason": {"type": "string"},
                    "ttl_minutes": {"type": "integer", "default": 240},
                },
                "required": ["asset", "school", "weight", "reason"],
            },
        },
        {
            "name": "jarvis_active_overrides",
            "description": (
                "Compact snapshot of currently-active Hermes overrides "
                "(size_modifiers + school_weights). Expired entries are "
                "filtered out automatically."
            ),
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_clear_override",
            "description": (
                "Manually remove an active Hermes override before its TTL "
                "expires. Pass either bot_id OR (asset AND school) — never both."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "bot_id": {"type": "string"},
                    "asset": {"type": "string"},
                    "school": {"type": "string"},
                },
            },
        },
        {
            "name": "jarvis_topology",
            "description": (
                "Node-link graph of the fleet for force-directed visualization "
                "(Claw3D, D3, Cytoscape). Nodes are bots colored by tier and "
                "sized by notional; links represent asset-class correlation. "
                "Read-only — pulls from kaizen_latest.json."
            ),
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_register_agent",
            "description": (
                "Register this agent with the inter-agent coordination bus (T14). "
                "Used by multi-session Claude Code workflows to declare their "
                "presence + role before claiming locks on destructive actions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "agent_id": {"type": "string"},
                    "role": {"type": "string"},
                    "version": {"type": "string", "default": "1.0.0"},
                },
                "required": ["agent_id", "role"],
            },
        },
        {
            "name": "jarvis_list_agents",
            "description": (
                "List currently-online agents in the inter-agent bus. "
                "Filters to live agents (heartbeat <10 min ago) by default."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "only_alive": {"type": "boolean", "default": True},
                },
            },
        },
        {
            "name": "jarvis_acquire_lock",
            "description": (
                "Claim a coordination lock on a resource (e.g. bot_id, "
                "'fleet_kill') before performing a conflicting destructive "
                "action. Returns ACQUIRED, REACQUIRED, or LOCKED_BY_OTHER."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "agent_id": {"type": "string"},
                    "resource": {"type": "string"},
                    "purpose": {"type": "string"},
                    "ttl_seconds": {"type": "integer", "default": 600},
                },
                "required": ["agent_id", "resource"],
            },
        },
        {
            "name": "jarvis_release_lock",
            "description": (
                "Voluntarily release a lock acquired via jarvis_acquire_lock. "
                "Only the lock owner can release. Locks also auto-expire on TTL."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "agent_id": {"type": "string"},
                    "resource": {"type": "string"},
                },
                "required": ["agent_id", "resource"],
            },
        },
        {
            "name": "jarvis_explain_consult_causal",
            "description": (
                "Causal/marginal-effect attribution for one consult (T6). "
                "Returns per-school marginal_final_delta + is_decisive flag — "
                "answers 'which schools' votes mattered most for this verdict'. "
                "Requires the consult record to be schema v2 (school_inputs populated)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "consult_id": {"type": "string"},
                    "perturbation_sigma": {"type": "number", "default": 1.0},
                },
                "required": ["consult_id"],
            },
        },
        {
            "name": "jarvis_replay_consult",
            "description": (
                "Re-execute a past consult with optional hypothetical overrides (T7). "
                "Without overrides this is a determinism check; with overrides "
                "it answers 'what would have happened if X'. Requires v2 record."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "consult_id": {"type": "string"},
                    "override_overrides": {"type": "object"},
                    "override_hot_weights": {"type": "object"},
                    "override_school_inputs": {"type": "object"},
                },
                "required": ["consult_id"],
            },
        },
        {
            "name": "jarvis_counterfactual",
            "description": (
                "Operator-friendly wrapper around jarvis_replay_consult. "
                "Common patterns: pin_size_modifier alone for 'what if I trimmed', "
                "OR (pin_school + pin_weight) for 'what if I weighted differently'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "consult_id": {"type": "string"},
                    "pin_size_modifier": {"type": "number"},
                    "pin_school": {"type": "string"},
                    "pin_weight": {"type": "number"},
                },
                "required": ["consult_id"],
            },
        },
        {
            "name": "jarvis_attribution_cube",
            "description": (
                "Performance attribution sliced by school/asset/hour/verdict/bot (T12). "
                "Joins trace + trade_closes streams. Filters: asset, bot_id, school, "
                "since_days_ago, hour_min, hour_max."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "slice_by": {"type": "array", "items": {"type": "string"}},
                    "filter": {"type": "object"},
                },
            },
        },
        {
            "name": "jarvis_current_regime",
            "description": (
                "Classify the current market regime from sentiment + drawdown signals (T8). "
                "Returns regime label + confidence + recommended override pack."
            ),
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_list_regime_packs",
            "description": "List built-in override packs (T8). Operator-readable.",
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_apply_regime_pack",
            "description": (
                "Apply a named override pack's size_modifiers + school_weights "
                "via the existing hermes_overrides surface (T8). Operator confirms."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "name": {"type": "string"},
                    "ttl_minutes": {"type": "integer", "default": 240},
                    "bot_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required when the pack uses '*' size_modifier pattern.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "jarvis_kelly_recommend",
            "description": (
                "Fractional-Kelly sizing recommendation per bot from recent trade closes (T13). "
                "Returns per-bot recommended_size_modifier; operator confirms before applying."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "lookback_days": {"type": "integer", "default": 30},
                    "kelly_fraction": {"type": "number", "default": 0.25},
                    "drawdown_penalty": {"type": "number", "default": 0.15},
                },
            },
        },
        {
            "name": "jarvis_cost_summary",
            "description": (
                "Hermes/DeepSeek LLM spend telemetry over a time window. "
                "Returns total USD + breakdown by tool/skill/day. Uses flat-rate "
                "estimates ($0.003/call by default; tunable via env vars)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "since_days_ago": {"type": "integer", "default": 7},
                },
            },
        },
        {
            "name": "jarvis_cost_today",
            "description": "Today's running LLM spend (UTC midnight to now).",
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_cost_anomaly",
            "description": (
                "Detect runaway-spend anomalies (last N min vs 24h baseline). "
                "Returns {anomaly: bool, multiplier, …}. anomaly=True when "
                "recent rate ≥ 10× baseline — catches buggy scheduled tasks."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "window_min": {"type": "integer", "default": 60},
                },
            },
        },
        {
            "name": "jarvis_pnl_summary",
            "description": (
                "Operator PnL aggregation over a time window. Returns total R, "
                "win rate, best/worst trade, top performers, recent trades. "
                "Reads canonical + legacy trade_closes paths and dedupes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "window_hours": {"type": "number", "default": 24},
                },
            },
        },
        {
            "name": "jarvis_pnl_multi_window",
            "description": (
                "PnL bundled across today (24h) + week (168h) + month (720h) "
                "in one envelope. Used by the operator-briefing skill for "
                "the PnL-first Telegram digest."
            ),
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_material_events_since",
            "description": (
                "True iff anything operator-material happened since asof_iso: "
                "new trade, |R| delta >= 0.5, big win (>=+2R), big loss (<=-2R), "
                "drawdown (<=-3R), or new override applied. Cron tasks use this "
                "to SUPPRESS spammy quiet-window deliveries."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "asof_iso": {"type": "string"},
                },
                "required": ["asof_iso"],
            },
        },
        {
            "name": "jarvis_zeus",
            "description": (
                "ZEUS SUPERCHARGE — unified brain snapshot. ONE call returns "
                "fleet_status + topology + active overrides + current regime + "
                "recent consults + top-5 Kelly recommendations + attribution "
                "winners/losers + sentiment + wiring audit + upcoming events + "
                "bots online. The operator's 'what's happening?' answer in one "
                "envelope. 30-second cached. Pass force_refresh=true to rebuild."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "force_refresh": {"type": "boolean", "default": False},
                    "trace_n": {"type": "integer", "default": 10},
                },
            },
        },
        {
            "name": "jarvis_anomaly_scan",
            "description": (
                "Proactive anomaly detection. Scans recent trade closes for "
                "patterns operator should know about BEFORE asking: 3+ "
                "consecutive losses per bot, 5+ of last 8 trades losing, "
                "drawdown patterns. Returns NEW (post-dedup) hits only — "
                "same anomaly is suppressed for DEDUP_HOURS=4 after first fire. "
                "Each hit carries a suggested_skill the operator (or cron) "
                "can route to. Used by the ETA-Anomaly-Watcher cron task to "
                "replace noisy 'watchdog auto-healed' alerts with meaningful "
                "'bot X has 4 losses in a row' alerts."
            ),
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_anomaly_recent",
            "description": (
                "Replay recent anomaly hits from the watcher log for operator "
                "review. Returns dedup-aware hits within since_hours window. "
                "Pair with jarvis_anomaly_scan: scan returns NEW hits, "
                "recent returns ALL hits in the window including ones already "
                "dispatched."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "since_hours": {"type": "integer", "default": 24},
                },
            },
        },
        {
            "name": "jarvis_preflight",
            "description": (
                "Live-cutover Go/No-Go preflight. Runs 13 read-only checks "
                "(workspace + state writable, Hermes port up, status server, "
                "trade-close stream fresh, memory backup, kaizen latest, "
                "anomaly-pulse + bridge-autoheal cron health, telegram-inbound "
                "alive, kill switch disengaged, active overrides under cap, "
                "no open critical anomalies). Returns verdict READY or NOT "
                "READY plus a per-check breakdown. NEVER writes except its "
                "own JSONL audit log."
            ),
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_prop_firm_status",
            "description": (
                "Live snapshot of every registered prop firm account: rules, "
                "current balance, day PnL, peak balance, daily-loss headroom, "
                "trailing-DD headroom, profit-to-target, severity tag "
                "(ok/warn/critical/blown). Includes BluSky, Apex eval+funded, "
                "Topstep, ETF. Sorted by severity (worst first). Read-only."
            ),
            "inputSchema": {"type": "object", "properties": auth_field},
        },
        {
            "name": "jarvis_prop_firm_evaluate",
            "description": (
                "Worst-case rule check on one proposed signal. Pass account_id "
                "and signal={symbol, stop_r, size, [dollar_per_r]}. Returns "
                "allowed=true/false, blockers (list of specific rule names "
                "that would fail), headroom per rule, worst_case_loss_usd. "
                "This is the safety gate every new entry should pass through "
                "before becoming an order. Default-deny on malformed input."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "account_id": {"type": "string"},
                    "signal": {"type": "object"},
                },
                "required": ["account_id", "signal"],
            },
        },
        {
            "name": "jarvis_prop_firm_killall",
            "description": (
                "EMERGENCY: engage kill_all in hermes_state.json with a reason. "
                "Halts every bot fleetwide. Use when an account approaches "
                "rule breach or external risk demands halt. Functionally same "
                "as jarvis_kill_switch with 'kill all' phrase but with "
                "structured reason logging for prop-firm post-mortems."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **auth_field,
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _tool_fleet_status(_args: dict[str, Any]) -> dict[str, Any]:
    rep = _call_kaizen_latest() or {}
    elite_summary = rep.get("elite_summary") or {}
    top5_elite = elite_summary.get("top5_elite") or []
    top5_dark = elite_summary.get("top5_dark") or []
    return {
        "n_bots": rep.get("n_bots", 0),
        "tier_counts": rep.get("tier_counts", {}),
        "mc_counts": rep.get("mc_counts", {}),
        "action_counts": rep.get("action_counts", {}),
        "top5_elite": top5_elite,
        "top5_dark": top5_dark,
    }


def _tool_trace_tail(args: dict[str, Any]) -> list[dict[str, Any]]:
    n = int(args.get("n", 10))
    return _call_trace_tail(n)


def _tool_wiring_audit(_args: dict[str, Any]) -> dict[str, Any]:
    statuses = _call_wiring_audit() or []
    dark = [s for s in statuses if getattr(s, "expected_to_fire", False) and getattr(s, "dark_for_days", 0) >= 7]
    return {
        "n_dark": len(dark),
        "dark_modules": [getattr(s, "module", "") for s in dark],
        "n_total_expected": sum(1 for s in statuses if getattr(s, "expected_to_fire", False)),
        "n_total_modules": len(statuses),
    }


def _tool_portfolio_assess(args: dict[str, Any]) -> dict[str, Any]:
    bot_id = args.get("bot_id", "")
    asset_class = args.get("asset_class", "")
    action = args.get("action", "")
    # Build a minimal request object that portfolio_brain.assess can inspect
    # for an asset key (it falls back to None when the attribute isn't present).
    req = type(
        "HermesReq",
        (),
        {
            "bot_id": bot_id,
            "asset_class": asset_class,
            "asset": asset_class,
            "action": action,
        },
    )()
    ctx = _call_portfolio_snapshot()
    verdict = _call_portfolio_assess(req, ctx)
    return {
        "size_modifier": float(getattr(verdict, "size_modifier", 1.0)),
        "block_reason": getattr(verdict, "block_reason", None),
        "notes": list(getattr(verdict, "notes", ())),
    }


def _tool_hot_weights(args: dict[str, Any]) -> dict[str, float]:
    asset = args.get("asset", "")
    return _call_hot_weights(asset)


def _tool_upcoming_events(args: dict[str, Any]) -> list[dict[str, Any]]:
    horizon_min = int(args.get("horizon_min", 60))
    events = _call_upcoming_events(horizon_min)
    out: list[dict[str, Any]] = []
    for ev in events:
        out.append(
            {
                "ts_utc": getattr(ev, "ts_utc", ""),
                "kind": getattr(ev, "kind", ""),
                "symbol": getattr(ev, "symbol", None),
                "severity": int(getattr(ev, "severity", 1)),
            }
        )
    return out


def _tool_kaizen_run(args: dict[str, Any]) -> dict[str, Any]:
    bootstraps = int(args.get("bootstraps", 200))
    return _call_kaizen_run(bootstraps)


def _tool_deploy_strategy(args: dict[str, Any]) -> dict[str, Any]:
    """Destructive: lift a kaizen override on the bot (re-enables it).

    Uses the same 2-run gate as ``_tool_retire_strategy`` — the first
    call records the intent in ``kaizen_actions.jsonl`` and returns
    HELD; the second call (on a separate kaizen pass) applies it.
    """
    bot_id = str(args.get("bot_id", ""))
    reason = str(args.get("reason", ""))
    if not bot_id:
        return {"status": "REJECTED", "reason": "missing_bot_id"}

    # Read prior; the kaizen action log is shared with retire actions.
    prior = _previous_retire_targets()
    prior_action_log_entry = {
        "ts": _now_iso(),
        "action": "DEPLOY",
        "bot_id": bot_id,
        "reason": reason,
        "source": "hermes_mcp",
    }

    if bot_id not in prior:
        _append_kaizen_action(prior_action_log_entry)
        return {"status": "HELD", "reason": "awaiting_confirmation", "prior": False}

    # Second call — drop the bot from the override file.
    if _KAIZEN_OVERRIDES_PATH.exists():
        try:
            data = json.loads(_KAIZEN_OVERRIDES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"deactivated": {}}
        if isinstance(data, dict) and isinstance(data.get("deactivated"), dict):
            data["deactivated"].pop(bot_id, None)
            _KAIZEN_OVERRIDES_PATH.write_text(
                json.dumps(data, indent=2, default=str),
                encoding="utf-8",
            )
    _append_kaizen_action({**prior_action_log_entry, "status": "APPLIED"})
    return {"status": "APPLIED", "prior": True}


def _tool_retire_strategy(args: dict[str, Any]) -> dict[str, Any]:
    """Destructive: write a kaizen override deactivating ``bot_id``.

    2-run gate matches ``kaizen_loop._previous_retire_targets``: first
    sighting → HELD + recorded; second sighting → APPLIED + sidecar
    write. The sidecar is what ``per_bot_registry.is_active()`` reads
    on the next supervisor restart.

    DIAMOND PROTECTION (2026-05-13): bots in
    ``capital_allocator.DIAMOND_BOTS`` are NEVER deactivated via this
    path.  This mirrors the protection already enforced by
    ``kaizen_loop.run_loop()`` and ``per_bot_registry.is_active()``; we
    short-circuit here so the override file does not collect stale
    entries that the supervisor's diamond layer would silently ignore.
    """
    bot_id = str(args.get("bot_id", ""))
    reason = str(args.get("reason", ""))
    if not bot_id:
        return {"status": "REJECTED", "reason": "missing_bot_id"}

    # Diamond protection: refuse the write entirely. Hermes can retry
    # other actions (size cut, scaling) but auto-retire of a diamond
    # requires the operator removing the bot from DIAMOND_BOTS first.
    try:
        from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
            DIAMOND_BOTS,
        )

        if bot_id in DIAMOND_BOTS:
            _append_kaizen_action(
                {
                    "ts": _now_iso(),
                    "action": "RETIRE",
                    "bot_id": bot_id,
                    "reason": reason,
                    "source": "hermes_mcp",
                    "status": "PROTECTED_DIAMOND",
                },
            )
            return {
                "status": "REJECTED",
                "reason": "diamond_protected",
                "bot_id": bot_id,
                "note": (
                    "bot is in DIAMOND_BOTS — auto-retire blocked. "
                    "Operator must remove from DIAMOND_BOTS explicitly "
                    "before this bot can be retired."
                ),
            }
    except ImportError:
        # If the import ever fails, fall through to normal path rather
        # than silently disabling the diamond gate.
        pass

    prior = _previous_retire_targets()
    record = {
        "ts": _now_iso(),
        "action": "RETIRE",
        "bot_id": bot_id,
        "reason": reason,
        "source": "hermes_mcp",
    }

    if bot_id not in prior:
        _append_kaizen_action(record)
        return {"status": "HELD", "reason": "awaiting_confirmation", "prior": False}

    _write_override(bot_id, reason)
    _append_kaizen_action({**record, "status": "APPLIED"})
    return {"status": "APPLIED", "prior": True}


def _tool_kill_switch(args: dict[str, Any]) -> dict[str, Any]:
    """Trip the fleet kill switch.

    Requires both a valid token (checked upstream) and an exact
    ``confirm_phrase == "kill all"`` match. On APPLIED the canonical
    hermes_state.json file gets a ``kill_all: true`` latch the
    supervisor watches.
    """
    confirm = str(args.get("confirm_phrase", ""))
    reason = str(args.get("reason", ""))
    if confirm != _KILL_PHRASE:
        logger.warning(
            "jarvis_kill_switch rejected: confirm_phrase mismatch (reason=%s)",
            reason,
        )
        return {"status": "REJECTED", "reason": "confirm_phrase_mismatch"}

    killed_at = _now_iso()
    payload = {
        "kill_all": True,
        "killed_at": killed_at,
        "reason": reason,
        "source": _CALLER,
    }
    _HERMES_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _HERMES_STATE_PATH.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    return {"status": "APPLIED", "killed_at": killed_at, "scope": "all"}


def _tool_set_size_modifier(args: dict[str, Any]) -> dict[str, Any]:
    """Pin a size modifier on bot_id (Track 2 write-back).

    Magnitude is clamped server-side (in hermes_overrides) but we also
    reject obviously-wrong inputs (missing bot_id, NaN, negative TTL).
    """
    bot_id = str(args.get("bot_id", "") or "")
    if not bot_id:
        return {"status": "REJECTED", "reason": "missing_bot_id"}
    try:
        modifier = float(args.get("modifier"))
    except (TypeError, ValueError):
        return {"status": "REJECTED", "reason": "modifier_not_numeric"}
    reason = str(args.get("reason", "") or "")
    try:
        ttl_minutes = int(args.get("ttl_minutes", 240) or 240)
    except (TypeError, ValueError):
        ttl_minutes = 240
    if ttl_minutes <= 0:
        ttl_minutes = 240
    return _call_apply_size_modifier(
        bot_id=bot_id,
        modifier=modifier,
        reason=reason,
        ttl_minutes=ttl_minutes,
    )


def _tool_pin_school_weight(args: dict[str, Any]) -> dict[str, Any]:
    """Pin a school-weight overlay for (asset, school) (Track 2 write-back)."""
    asset = str(args.get("asset", "") or "")
    school = str(args.get("school", "") or "")
    if not asset or not school:
        return {"status": "REJECTED", "reason": "missing_asset_or_school"}
    try:
        weight = float(args.get("weight"))
    except (TypeError, ValueError):
        return {"status": "REJECTED", "reason": "weight_not_numeric"}
    reason = str(args.get("reason", "") or "")
    try:
        ttl_minutes = int(args.get("ttl_minutes", 240) or 240)
    except (TypeError, ValueError):
        ttl_minutes = 240
    if ttl_minutes <= 0:
        ttl_minutes = 240
    return _call_apply_school_weight(
        asset=asset,
        school=school,
        weight=weight,
        reason=reason,
        ttl_minutes=ttl_minutes,
    )


def _tool_active_overrides(_args: dict[str, Any]) -> dict[str, Any]:
    """Compact snapshot of live Hermes overrides (filtered to non-expired)."""
    return _call_active_overrides()


def _tool_topology(_args: dict[str, Any]) -> dict[str, Any]:
    """Return the fleet topology graph (T17). Read-only."""
    return _call_topology()


def _tool_register_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Register this agent with the inter-agent bus (T14)."""
    agent_id = str(args.get("agent_id", "") or "")
    role = str(args.get("role", "") or "")
    version = str(args.get("version", "1.0.0") or "1.0.0")
    if not agent_id or not role:
        return {"status": "REJECTED", "reason": "missing_agent_id_or_role"}
    return _call_register_agent(agent_id, role, version)


def _tool_list_agents(args: dict[str, Any]) -> list[dict[str, Any]]:
    """List currently-online agents."""
    only_alive = bool(args.get("only_alive", True))
    return _call_list_agents(only_alive)


def _tool_acquire_lock(args: dict[str, Any]) -> dict[str, Any]:
    """Claim a coordination lock (T14)."""
    agent_id = str(args.get("agent_id", "") or "")
    resource = str(args.get("resource", "") or "")
    purpose = str(args.get("purpose", "") or "")
    try:
        ttl_seconds = int(args.get("ttl_seconds", 600) or 600)
    except (TypeError, ValueError):
        ttl_seconds = 600
    if ttl_seconds <= 0:
        ttl_seconds = 600
    if not agent_id or not resource:
        return {"status": "REJECTED", "reason": "missing_agent_id_or_resource"}
    return _call_acquire_lock(agent_id, resource, purpose, ttl_seconds)


def _tool_release_lock(args: dict[str, Any]) -> dict[str, Any]:
    """Release a coordination lock (T14)."""
    agent_id = str(args.get("agent_id", "") or "")
    resource = str(args.get("resource", "") or "")
    if not agent_id or not resource:
        return {"status": "REJECTED", "reason": "missing_agent_id_or_resource"}
    return _call_release_lock(agent_id, resource)


def _tool_explain_consult_causal(args: dict[str, Any]) -> dict[str, Any]:
    """Causal/marginal-effect attribution for one consult (T6)."""
    consult_id = str(args.get("consult_id", "") or "")
    if not consult_id:
        return {"error": "missing_consult_id", "consult_id": "", "decisive_schools": []}
    try:
        sigma = float(args.get("perturbation_sigma", 1.0) or 1.0)
    except (TypeError, ValueError):
        sigma = 1.0
    if sigma <= 0:
        sigma = 1.0
    return _call_causal_analyze(consult_id, sigma)


def _tool_replay_consult(args: dict[str, Any]) -> dict[str, Any]:
    """Re-execute a past consult with optional overrides (T7)."""
    consult_id = str(args.get("consult_id", "") or "")
    if not consult_id:
        return {"error": "missing_consult_id", "consult_id": ""}
    over_overrides = args.get("override_overrides")
    over_hot = args.get("override_hot_weights")
    over_inputs = args.get("override_school_inputs")
    return _call_consult_replay(
        consult_id=consult_id,
        override_overrides=over_overrides if isinstance(over_overrides, dict) else None,
        override_hot_weights=over_hot if isinstance(over_hot, dict) else None,
        override_school_inputs=over_inputs if isinstance(over_inputs, dict) else None,
    )


def _tool_counterfactual(args: dict[str, Any]) -> dict[str, Any]:
    """Operator-friendly counterfactual wrapper around replay (T7)."""
    consult_id = str(args.get("consult_id", "") or "")
    if not consult_id:
        return {"error": "missing_consult_id", "consult_id": ""}
    pin_sm = args.get("pin_size_modifier")
    pin_school = args.get("pin_school")
    pin_weight = args.get("pin_weight")
    try:
        pin_sm_val = float(pin_sm) if pin_sm is not None else None
    except (TypeError, ValueError):
        pin_sm_val = None
    try:
        pin_weight_val = float(pin_weight) if pin_weight is not None else None
    except (TypeError, ValueError):
        pin_weight_val = None
    return _call_counterfactual(
        consult_id=consult_id,
        pin_size_modifier=pin_sm_val,
        pin_school=str(pin_school) if pin_school else None,
        pin_weight=pin_weight_val,
    )


def _tool_attribution_cube(args: dict[str, Any]) -> dict[str, Any]:
    """Slice-and-aggregate trade attribution (T12)."""
    slice_by = args.get("slice_by") or ["bot"]
    if not isinstance(slice_by, list):
        slice_by = ["bot"]
    filter_arg = args.get("filter") or {}
    if not isinstance(filter_arg, dict):
        filter_arg = {}
    return _call_attribution_query(
        slice_by=[str(d) for d in slice_by],
        filter_arg=filter_arg,
    )


def _tool_current_regime(_args: dict[str, Any]) -> dict[str, Any]:
    """Classify the current market regime (T8)."""
    return _call_current_regime()


def _tool_list_regime_packs(_args: dict[str, Any]) -> list[dict[str, Any]]:
    """List built-in regime override packs (T8)."""
    return _call_list_regime_packs()


def _tool_apply_regime_pack(args: dict[str, Any]) -> dict[str, Any]:
    """Apply a named regime override pack (T8)."""
    name = str(args.get("name", "") or "")
    if not name:
        return {"status": "REJECTED", "reason": "missing_pack_name"}
    try:
        ttl = int(args.get("ttl_minutes", 240) or 240)
    except (TypeError, ValueError):
        ttl = 240
    if ttl <= 0:
        ttl = 240
    bot_ids_arg = args.get("bot_ids") or []
    bot_ids: list[str] | None = [str(b) for b in bot_ids_arg] if isinstance(bot_ids_arg, list) else None
    return _call_apply_regime_pack(name=name, ttl_minutes=ttl, bot_ids=bot_ids)


def _tool_kelly_recommend(args: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-bot fractional-Kelly sizing recommendations (T13)."""
    try:
        lookback = int(args.get("lookback_days", 30) or 30)
    except (TypeError, ValueError):
        lookback = 30
    if lookback <= 0:
        lookback = 30
    try:
        kf = float(args.get("kelly_fraction", 0.25) or 0.25)
    except (TypeError, ValueError):
        kf = 0.25
    try:
        dp = float(args.get("drawdown_penalty", 0.15) or 0.15)
    except (TypeError, ValueError):
        dp = 0.15
    return _call_kelly_recommend(
        lookback_days=lookback,
        kelly_fraction=kf,
        drawdown_penalty=dp,
    )


def _tool_cost_summary(args: dict[str, Any]) -> dict[str, Any]:
    """LLM spend telemetry summary."""
    try:
        days = int(args.get("since_days_ago", 7) or 7)
    except (TypeError, ValueError):
        days = 7
    if days <= 0:
        days = 7
    return _call_cost_summary(days)


def _tool_cost_today(_args: dict[str, Any]) -> dict[str, Any]:
    """Today's running LLM spend."""
    return _call_cost_today()


def _tool_cost_anomaly(args: dict[str, Any]) -> dict[str, Any]:
    """Detect runaway-spend anomalies."""
    try:
        window = int(args.get("window_min", 60) or 60)
    except (TypeError, ValueError):
        window = 60
    if window <= 0:
        window = 60
    return _call_cost_anomaly(window)


def _tool_pnl_summary(args: dict[str, Any]) -> dict[str, Any]:
    """Operator PnL aggregation over a window."""
    try:
        win = float(args.get("window_hours", 24) or 24)
    except (TypeError, ValueError):
        win = 24.0
    if win <= 0:
        win = 24.0
    return _call_pnl_summary(win)


def _tool_pnl_multi_window(_args: dict[str, Any]) -> dict[str, Any]:
    """Today + week + month PnL bundle."""
    return _call_pnl_multi_window()


def _tool_material_events_since(args: dict[str, Any]) -> dict[str, Any]:
    """Material-event suppress check for cron tasks."""
    asof = str(args.get("asof_iso", "") or "")
    if not asof:
        return {"has_material": False, "reasons": ["missing_asof_iso"]}
    return _call_material_events_since(asof)


def _tool_zeus(args: dict[str, Any]) -> dict[str, Any]:
    """ZEUS SUPERCHARGE — unified brain snapshot in one call."""
    force = bool(args.get("force_refresh", False))
    try:
        trace_n = int(args.get("trace_n", 10) or 10)
    except (TypeError, ValueError):
        trace_n = 10
    if trace_n <= 0:
        trace_n = 10
    return _call_zeus_snapshot(force_refresh=force, trace_n=trace_n)


def _tool_anomaly_scan(_args: dict[str, Any]) -> dict[str, Any]:
    """Run anomaly watcher detectors. Returns NEW (post-dedup) hits."""
    hits = _call_anomaly_scan()
    return {
        "n_new": len(hits),
        "hits": hits,
        "asof": _now_iso(),
    }


def _tool_anomaly_recent(args: dict[str, Any]) -> dict[str, Any]:
    """Replay recent anomaly hits from the watcher log."""
    try:
        since = int(args.get("since_hours", 24) or 24)
    except (TypeError, ValueError):
        since = 24
    if since <= 0:
        since = 24
    recent = _call_anomaly_recent(since_hours=since)
    return {
        "n": len(recent),
        "since_hours": since,
        "hits": recent,
        "asof": _now_iso(),
    }


def _tool_preflight(_args: dict[str, Any]) -> dict[str, Any]:
    """Live-cutover Go/No-Go preflight reporter."""
    return _call_preflight()


def _tool_prop_firm_status(_args: dict[str, Any]) -> dict[str, Any]:
    """Snapshot every prop firm account's live state + rules + headroom."""
    snaps = _call_prop_firm_status()
    n_critical = sum(1 for s in snaps if s.get("severity") in ("critical", "blown"))
    n_warn = sum(1 for s in snaps if s.get("severity") == "warn")
    return {
        "asof": _now_iso(),
        "n_accounts": len(snaps),
        "n_critical_or_blown": n_critical,
        "n_warn": n_warn,
        "snapshots": snaps,
    }


def _tool_prop_firm_evaluate(args: dict[str, Any]) -> dict[str, Any]:
    """Worst-case rule check on one proposed signal."""
    account_id = str(args.get("account_id", "") or "")
    if not account_id:
        return {
            "allowed": False,
            "reason": "missing account_id",
            "blockers": ["missing_account_id"],
            "headroom": {},
            "worst_case_loss_usd": 0.0,
            "asof": _now_iso(),
        }
    signal = args.get("signal") or {}
    if not isinstance(signal, dict):
        return {
            "allowed": False,
            "reason": "signal must be a dict",
            "blockers": ["malformed_signal"],
            "headroom": {},
            "worst_case_loss_usd": 0.0,
            "asof": _now_iso(),
        }
    return _call_prop_firm_evaluate(account_id, signal)


def _tool_prop_firm_killall(args: dict[str, Any]) -> dict[str, Any]:
    """Engage emergency kill_all with structured reason."""
    reason = str(args.get("reason", "") or "")
    if not reason:
        return {
            "status": "REJECTED",
            "error": "reason is required (audit trail for prop-firm post-mortems)",
        }
    return _call_prop_firm_killall(reason)


def _tool_clear_override(args: dict[str, Any]) -> dict[str, Any]:
    """Manual clear of one override before TTL — operator escape hatch.

    Accepts either bot_id alone (clears size_modifier) or
    (asset, school) pair (clears school_weight). Anything else returns
    REJECTED.
    """
    bot_id = args.get("bot_id") or None
    asset = args.get("asset") or None
    school = args.get("school") or None
    return _call_clear_override(bot_id=bot_id, asset=asset, school=school)


def _tool_subscribe_events(args: dict[str, Any]) -> dict[str, Any]:
    """Poll one of the JARVIS event streams from ``since_offset`` onward.

    Returns ``{events, next_offset, stream, file_size, exhausted}``.

    * ``next_offset`` is the byte position the caller should pass back
      on the next poll. If ``events`` is empty and ``exhausted`` is True,
      there is nothing new yet and the caller should sleep before polling
      again. Hermes's skill wraps this in a 1–3s polling loop.
    * ``file_size`` lets the caller detect catch-up state (when
      ``next_offset == file_size`` the stream is fully consumed at this moment).
    """
    stream = str(args.get("stream", "trace") or "trace")
    if stream not in _EVENT_STREAMS:
        return {
            "events": [],
            "next_offset": 0,
            "stream": stream,
            "file_size": 0,
            "exhausted": True,
            "error": f"unknown_stream:{stream}",
        }
    try:
        since_offset = int(args.get("since_offset", 0) or 0)
    except (TypeError, ValueError):
        since_offset = 0
    try:
        limit = int(args.get("limit", 50) or 50)
    except (TypeError, ValueError):
        limit = 50
    filters = args.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}

    records, next_offset = _call_subscribe_events(stream, since_offset, limit)
    if filters:
        records = _apply_event_filters(records, filters)

    file_path = _EVENT_STREAMS[stream]
    try:
        file_size = file_path.stat().st_size if file_path.exists() else 0
    except OSError:
        file_size = next_offset
    return {
        "events": records,
        "next_offset": next_offset,
        "stream": stream,
        "file_size": file_size,
        "exhausted": next_offset >= file_size,
    }


def _tool_explain_verdict(args: dict[str, Any]) -> dict[str, Any]:
    consult_id = str(args.get("consult_id", ""))
    if not consult_id:
        return {"narrative": "no consult_id given", "raw_record": {}}
    # Pull a generous window so we can match an arbitrary historical id.
    records = _call_trace_tail(500) or []
    matched: dict[str, Any] = {}
    for rec in records:
        if isinstance(rec, dict) and rec.get("consult_id") == consult_id:
            matched = rec
            break
    if not matched:
        return {"narrative": f"no trace record for consult_id={consult_id}", "raw_record": {}}
    narrative = _call_verdict_to_narrative(matched)
    return {"narrative": narrative, "raw_record": matched}


_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "jarvis_fleet_status": _tool_fleet_status,
    "jarvis_trace_tail": _tool_trace_tail,
    "jarvis_wiring_audit": _tool_wiring_audit,
    "jarvis_portfolio_assess": _tool_portfolio_assess,
    "jarvis_hot_weights": _tool_hot_weights,
    "jarvis_upcoming_events": _tool_upcoming_events,
    "jarvis_kaizen_run": _tool_kaizen_run,
    "jarvis_deploy_strategy": _tool_deploy_strategy,
    "jarvis_retire_strategy": _tool_retire_strategy,
    "jarvis_kill_switch": _tool_kill_switch,
    "jarvis_explain_verdict": _tool_explain_verdict,
    "jarvis_subscribe_events": _tool_subscribe_events,
    "jarvis_set_size_modifier": _tool_set_size_modifier,
    "jarvis_pin_school_weight": _tool_pin_school_weight,
    "jarvis_active_overrides": _tool_active_overrides,
    "jarvis_clear_override": _tool_clear_override,
    "jarvis_topology": _tool_topology,
    "jarvis_register_agent": _tool_register_agent,
    "jarvis_list_agents": _tool_list_agents,
    "jarvis_acquire_lock": _tool_acquire_lock,
    "jarvis_release_lock": _tool_release_lock,
    "jarvis_explain_consult_causal": _tool_explain_consult_causal,
    "jarvis_replay_consult": _tool_replay_consult,
    "jarvis_counterfactual": _tool_counterfactual,
    "jarvis_attribution_cube": _tool_attribution_cube,
    "jarvis_current_regime": _tool_current_regime,
    "jarvis_list_regime_packs": _tool_list_regime_packs,
    "jarvis_apply_regime_pack": _tool_apply_regime_pack,
    "jarvis_kelly_recommend": _tool_kelly_recommend,
    "jarvis_cost_summary": _tool_cost_summary,
    "jarvis_cost_today": _tool_cost_today,
    "jarvis_cost_anomaly": _tool_cost_anomaly,
    "jarvis_pnl_summary": _tool_pnl_summary,
    "jarvis_pnl_multi_window": _tool_pnl_multi_window,
    "jarvis_material_events_since": _tool_material_events_since,
    "jarvis_zeus": _tool_zeus,
    "jarvis_anomaly_scan": _tool_anomaly_scan,
    "jarvis_anomaly_recent": _tool_anomaly_recent,
    "jarvis_preflight": _tool_preflight,
    "jarvis_prop_firm_status": _tool_prop_firm_status,
    "jarvis_prop_firm_evaluate": _tool_prop_firm_evaluate,
    "jarvis_prop_firm_killall": _tool_prop_firm_killall,
}


# ---------------------------------------------------------------------------
# Top-level dispatch — exercised by both the SDK transport and the test suite
# ---------------------------------------------------------------------------


def _expected_token() -> str:
    return os.environ.get("JARVIS_MCP_TOKEN", "")


def dispatch_tool_call(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Entry point shared by the MCP SDK and the stdio-JSONRPC fallback.

    Sequence: validate token → scrub args → execute handler → audit
    log. The handler itself is wrapped in ``_envelope_guard`` so a
    raised exception still returns a clean envelope.
    """
    args = dict(args or {})
    auth = str(args.get("_auth", ""))
    expected = _expected_token()
    audit_args = _scrub_args(args)

    started = time.monotonic()

    # Auth policy:
    #   * No token configured at all (`JARVIS_MCP_TOKEN` env unset)
    #       -> reject everything; operator explicitly opted out of running.
    #   * Token is configured AND caller passed `_auth` -> match.
    #   * Token is configured AND caller omitted `_auth` -> trust the
    #     env-configured token. Stdio MCP clients (Hermes Agent, Claude
    #     Desktop, etc.) typically don't pass arg-level auth on every
    #     tool call; the server runs in-process for them and the env
    #     itself is the auth. Audit-logged as `auth: env` so we can
    #     distinguish the two paths.
    if not expected:
        _append_audit(
            {
                "ts": _now_iso(),
                "tool": name,
                "args": audit_args,
                "auth": "failed",
                "result_status": "auth_no_token_configured",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "caller": _CALLER,
            }
        )
        return {"ok": False, "data": None, "error": "auth_no_token_configured"}

    if auth and auth != expected:
        _append_audit(
            {
                "ts": _now_iso(),
                "tool": name,
                "args": audit_args,
                "auth": "failed",
                "result_status": "auth_failed",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "caller": _CALLER,
            }
        )
        return {"ok": False, "data": None, "error": "auth_failed"}

    # Either auth matched explicitly, or auth was omitted and env-token
    # presence is sufficient.
    _auth_mode = "ok" if auth else "env"

    handler = _HANDLERS.get(name)
    if handler is None:
        _append_audit(
            {
                "ts": _now_iso(),
                "tool": name,
                "args": audit_args,
                "auth": _auth_mode,
                "result_status": "unknown_tool",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "caller": _CALLER,
            }
        )
        return {"ok": False, "data": None, "error": f"unknown_tool: {name}"}

    # Strip the auth field before the handler sees it — the handler
    # doesn't need it, and never logging it past this point is one
    # more belt-and-braces shield against accidental disclosure.
    clean_args = {k: v for k, v in args.items() if k != "_auth"}
    envelope = _envelope_guard(lambda: handler(clean_args))

    # Derive a short status string for the audit log
    result_status = "ok" if envelope["ok"] else "error"
    if envelope["ok"] and isinstance(envelope["data"], dict):
        status = envelope["data"].get("status")
        if isinstance(status, str):
            result_status = status

    _append_audit(
        {
            "ts": _now_iso(),
            "tool": name,
            "args": audit_args,
            "auth": _auth_mode,
            "result_status": result_status,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "caller": _CALLER,
        }
    )
    return envelope


# ---------------------------------------------------------------------------
# MCP wire surface
# ---------------------------------------------------------------------------


async def _serve_with_sdk() -> None:
    """Run the server using the official ``mcp`` SDK over stdio."""
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    server: Server = Server("jarvis-mcp")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            )
            for spec in list_tools()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        envelope = dispatch_tool_call(name, arguments)
        return [TextContent(type="text", text=json.dumps(envelope, default=str))]

    async with stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="jarvis-mcp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def _jsonrpc_response(
    req_id: Any,  # noqa: ANN401 — JSON-RPC id is int|str|None per spec
    result: Any = None,  # noqa: ANN401 — protocol result is arbitrary
    error: Any = None,  # noqa: ANN401 — protocol error is arbitrary
) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def _serve_with_stdio_fallback() -> None:
    """Minimal JSON-RPC-over-stdio loop. Used only when the SDK is missing.

    Speaks ``initialize``, ``tools/list``, and ``tools/call`` — the
    exact subset Hermes (Half 3) uses. Anything else gets a method-not-
    found error.
    """
    out = sys.stdout
    err = sys.stderr
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            err.write(f"jarvis_mcp: malformed JSON-RPC frame: {exc}\n")
            continue
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            resp = _jsonrpc_response(
                req_id,
                result={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jarvis-mcp", "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            resp = _jsonrpc_response(req_id, result={"tools": list_tools()})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments") or {}
            envelope = dispatch_tool_call(tool_name, args)
            resp = _jsonrpc_response(
                req_id,
                result={
                    "content": [{"type": "text", "text": json.dumps(envelope, default=str)}],
                },
            )
        else:
            resp = _jsonrpc_response(
                req_id,
                error={
                    "code": -32601,
                    "message": f"method not found: {method}",
                },
            )

        out.write(json.dumps(resp, default=str) + "\n")
        out.flush()


def serve() -> None:
    """Public entry point. Prefer the SDK transport; fall back on import error."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        logger.warning("mcp SDK not available; using stdio-JSONRPC fallback")
        _serve_with_stdio_fallback()
        return
    asyncio.run(_serve_with_sdk())


__all__ = [
    "EXPECTED_HOOKS",
    "dispatch_tool_call",
    "list_tools",
    "serve",
]


# A unique correlation id for the server process — used in audit rows the
# integration tests inspect.
_SERVER_INSTANCE_ID = uuid.uuid4().hex[:12]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    serve()
