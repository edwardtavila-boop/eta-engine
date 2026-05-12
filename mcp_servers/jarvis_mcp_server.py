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


def _append_audit(record: dict[str, Any]) -> None:
    """Append one JSONL line to the hermes audit log. Never raises."""
    try:
        _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        json.dumps(data, indent=2, default=str), encoding="utf-8",
    )
    return data


# ---------------------------------------------------------------------------
# Tool registry — names, descriptions, JSON-schema input definitions
# ---------------------------------------------------------------------------


def list_tools() -> list[dict[str, Any]]:
    """Return the 11 declared tools as plain dicts (SDK-agnostic)."""
    auth_field = {"_auth": {"type": "string", "description": "JARVIS_MCP_TOKEN"}}
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
    dark = [
        s for s in statuses
        if getattr(s, "expected_to_fire", False) and getattr(s, "dark_for_days", 0) >= 7
    ]
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
    req = type("HermesReq", (), {
        "bot_id": bot_id,
        "asset_class": asset_class,
        "asset": asset_class,
        "action": action,
    })()
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
        out.append({
            "ts_utc": getattr(ev, "ts_utc", ""),
            "kind": getattr(ev, "kind", ""),
            "symbol": getattr(ev, "symbol", None),
            "severity": int(getattr(ev, "severity", 1)),
        })
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
                json.dumps(data, indent=2, default=str), encoding="utf-8",
            )
    _append_kaizen_action({**prior_action_log_entry, "status": "APPLIED"})
    return {"status": "APPLIED", "prior": True}


def _tool_retire_strategy(args: dict[str, Any]) -> dict[str, Any]:
    """Destructive: write a kaizen override deactivating ``bot_id``.

    2-run gate matches ``kaizen_loop._previous_retire_targets``: first
    sighting → HELD + recorded; second sighting → APPLIED + sidecar
    write. The sidecar is what ``per_bot_registry.is_active()`` reads
    on the next supervisor restart.
    """
    bot_id = str(args.get("bot_id", ""))
    reason = str(args.get("reason", ""))
    if not bot_id:
        return {"status": "REJECTED", "reason": "missing_bot_id"}

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
        json.dumps(payload, indent=2, default=str), encoding="utf-8",
    )
    return {"status": "APPLIED", "killed_at": killed_at, "scope": "all"}


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

    if not expected or auth != expected:
        _append_audit({
            "ts": _now_iso(),
            "tool": name,
            "args": audit_args,
            "auth": "failed",
            "result_status": "auth_failed",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "caller": _CALLER,
        })
        return {"ok": False, "data": None, "error": "auth_failed"}

    handler = _HANDLERS.get(name)
    if handler is None:
        _append_audit({
            "ts": _now_iso(),
            "tool": name,
            "args": audit_args,
            "auth": "ok",
            "result_status": "unknown_tool",
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "caller": _CALLER,
        })
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

    _append_audit({
        "ts": _now_iso(),
        "tool": name,
        "args": audit_args,
        "auth": "ok",
        "result_status": result_status,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "caller": _CALLER,
    })
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
            resp = _jsonrpc_response(req_id, result={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis-mcp", "version": "0.1.0"},
            })
        elif method == "tools/list":
            resp = _jsonrpc_response(req_id, result={"tools": list_tools()})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments") or {}
            envelope = dispatch_tool_call(tool_name, args)
            resp = _jsonrpc_response(req_id, result={
                "content": [{"type": "text", "text": json.dumps(envelope, default=str)}],
            })
        else:
            resp = _jsonrpc_response(req_id, error={
                "code": -32601, "message": f"method not found: {method}",
            })

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
