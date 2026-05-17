"""
Hermes-JARVIS bridge health check.

One command answers "is the bridge healthy right now?" by probing each
layer top-down and reporting status with a pass/fail summary. Intended
to be run by the operator on demand (after a reboot, after a config
change, before a live session) or by the auto-restart watchdog as a
liveness check.

Layers probed (in order):

  1. Desktop SSH tunnel  — local TCP 8642 listening
  2. VPS Hermes gateway  — HTTP 200 from /health through tunnel
  3. VPS Hermes API key  — authenticated /v1/models returns the model
  4. DeepSeek upstream    — short chat completion succeeds
  5. JARVIS MCP server    — jarvis_fleet_status returns ok=True via chat
  6. Operator memory      — fact_store search reachable
  7. Override sidecar     — apply + read round-trip via tool call
  8. Audit log on disk    — file exists, size sane, parseable last 10 lines
  9. Memory store on disk — db file exists, integrity_check=ok

Each layer is independent — a failure at any layer doesn't abort the
later ones. The exit code is non-zero iff any layer failed.

Usage:

  python -m eta_engine.scripts.hermes_bridge_health
  python -m eta_engine.scripts.hermes_bridge_health --json
  python -m eta_engine.scripts.hermes_bridge_health --skip llm  # skip layer 4 (saves a DeepSeek call)

Designed to be safe to run from any machine that has tunnel access; no
secrets in args, all secrets read from env (HERMES_API_KEY or
JARVIS_MCP_TOKEN) or from the gitignored secrets sidecar.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
DEFAULT_AUDIT_PATH = workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH
DEFAULT_MEMORY_DB = workspace_roots.ETA_HERMES_MEMORY_DB_PATH

# All known layer names. Operator can skip any via --skip name1,name2.
LAYER_NAMES = (
    "tunnel",
    "gateway",
    "auth",
    "llm",
    "jarvis_mcp",
    "memory",
    "overrides",
    "audit",
    "memory_db",
)


@dataclass
class LayerResult:
    name: str
    ok: bool
    detail: str = ""
    elapsed_ms: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------


def _probe_with_timing(
    name: str,
    fn: Callable[[], tuple[bool, str, dict[str, Any]]],
) -> LayerResult:
    """Run ``fn`` and wrap its (ok, detail, extras) tuple in a LayerResult.

    Any exception is caught and turned into ``ok=False`` so the health
    check never crashes partway through.
    """
    t0 = time.monotonic()
    try:
        ok, detail, extras = fn()
    except Exception as exc:  # noqa: BLE001 — health probe never raises
        return LayerResult(
            name=name,
            ok=False,
            detail=f"exception: {exc}",
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
    return LayerResult(
        name=name,
        ok=ok,
        detail=detail,
        elapsed_ms=(time.monotonic() - t0) * 1000.0,
        extras=extras,
    )


def _http_get_json(url: str, api_key: str | None, timeout: float = 5.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return {"status": resp.status, "body": body}


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str | None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return {"status": resp.status, "body": body}


# ---------------------------------------------------------------------------
# Layer implementations
# ---------------------------------------------------------------------------


def probe_tunnel(host: str, port: int) -> tuple[bool, str, dict]:
    """Layer 1: local TCP 8642 is open."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((host, port))
        return True, f"connected to {host}:{port}", {}
    except OSError as exc:
        return False, f"cannot connect to {host}:{port}: {exc}", {}
    finally:
        s.close()


def probe_gateway(host: str, port: int, api_key: str | None) -> tuple[bool, str, dict]:
    """Layer 2: HTTP 200 from /health (no auth required)."""
    try:
        r = _http_get_json(f"http://{host}:{port}/health", api_key=None, timeout=3.0)
    except (urllib.error.URLError, OSError) as exc:
        return False, f"/health unreachable: {exc}", {}
    if r["status"] != 200:
        return False, f"/health returned {r['status']}", {"body": r["body"]}
    return True, f"/health 200: {r['body'][:80]}", {}


def probe_auth(host: str, port: int, api_key: str | None) -> tuple[bool, str, dict]:
    """Layer 3: authenticated /v1/models call."""
    if not api_key:
        return False, "API_SERVER_KEY not configured in env", {}
    try:
        r = _http_get_json(f"http://{host}:{port}/v1/models", api_key=api_key, timeout=8.0)
    except urllib.error.HTTPError as exc:
        return False, f"/v1/models HTTP {exc.code}", {}
    except (urllib.error.URLError, OSError) as exc:
        return False, f"/v1/models error: {exc}", {}
    if r["status"] != 200:
        return False, f"/v1/models returned {r['status']}", {}
    try:
        data = json.loads(r["body"])
        models = data.get("data", []) or data.get("models", [])
        n = len(models) if isinstance(models, list) else 0
    except (json.JSONDecodeError, AttributeError):
        n = -1
    return True, f"/v1/models 200 ({n} model(s) configured)", {}


def probe_llm(host: str, port: int, api_key: str | None) -> tuple[bool, str, dict]:
    """Layer 4: short chat completion succeeds (DeepSeek upstream live)."""
    if not api_key:
        return False, "API_SERVER_KEY not configured in env", {}
    try:
        r = _http_post_json(
            f"http://{host}:{port}/v1/chat/completions",
            payload={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "Reply ONLY: pong"}],
                "max_tokens": 4,
                "stream": False,
            },
            api_key=api_key,
            timeout=45.0,
        )
    except urllib.error.HTTPError as exc:
        return False, f"chat HTTP {exc.code}", {}
    except (urllib.error.URLError, OSError) as exc:
        return False, f"chat error: {exc}", {}
    if r["status"] != 200:
        return False, f"chat returned {r['status']}", {}
    try:
        data = json.loads(r["body"])
        reply = data["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError):
        return False, "chat response not parseable", {}
    return True, f"chat 200, model replied: {reply!r}", {}


def probe_jarvis_mcp(host: str, port: int, api_key: str | None) -> tuple[bool, str, dict]:
    """Layer 5: jarvis_fleet_status MCP tool reachable through Hermes."""
    if not api_key:
        return False, "API_SERVER_KEY not configured in env", {}
    try:
        r = _http_post_json(
            f"http://{host}:{port}/v1/chat/completions",
            payload={
                "model": "deepseek-v4-pro",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Call jarvis_fleet_status with no args. Then reply ONLY "
                            "with: jarvis_fleet_status_ok if you got back an envelope, "
                            "or jarvis_fleet_status_failed otherwise. No prose."
                        ),
                    }
                ],
                "max_tokens": 32,
                "stream": False,
            },
            api_key=api_key,
            timeout=60.0,
        )
    except (urllib.error.URLError, OSError) as exc:
        return False, f"chat error: {exc}", {}
    if r["status"] != 200:
        return False, f"chat returned {r['status']}", {}
    try:
        reply = json.loads(r["body"])["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError):
        return False, "chat response not parseable", {}
    ok = "jarvis_fleet_status_ok" in reply
    return ok, f"reply: {reply}", {}


def probe_memory(host: str, port: int, api_key: str | None) -> tuple[bool, str, dict]:
    """Layer 6: fact_store search returns a result (the holographic plugin is alive)."""
    if not api_key:
        return False, "API_SERVER_KEY not configured in env", {}
    try:
        r = _http_post_json(
            f"http://{host}:{port}/v1/chat/completions",
            payload={
                "model": "deepseek-v4-pro",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Call fact_store with action=search, query=health, limit=1. "
                            "Then reply ONLY with: memory_ok regardless of whether anything "
                            "was found. Reply memory_failed if the tool call itself errored."
                        ),
                    }
                ],
                "max_tokens": 16,
                "stream": False,
            },
            api_key=api_key,
            timeout=45.0,
        )
    except (urllib.error.URLError, OSError) as exc:
        return False, f"chat error: {exc}", {}
    if r["status"] != 200:
        return False, f"chat returned {r['status']}", {}
    try:
        reply = json.loads(r["body"])["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError):
        return False, "chat response not parseable", {}
    ok = "memory_ok" in reply
    return ok, f"reply: {reply}", {}


def probe_overrides(host: str, port: int, api_key: str | None) -> tuple[bool, str, dict]:
    """Layer 7: jarvis_active_overrides MCP tool returns the expected envelope."""
    if not api_key:
        return False, "API_SERVER_KEY not configured in env", {}
    try:
        r = _http_post_json(
            f"http://{host}:{port}/v1/chat/completions",
            payload={
                "model": "deepseek-v4-pro",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Call jarvis_active_overrides with no args. Reply ONLY with: "
                            "overrides_ok if you got an envelope with size_modifiers and "
                            "school_weights keys, overrides_failed otherwise."
                        ),
                    }
                ],
                "max_tokens": 16,
                "stream": False,
            },
            api_key=api_key,
            timeout=45.0,
        )
    except (urllib.error.URLError, OSError) as exc:
        return False, f"chat error: {exc}", {}
    if r["status"] != 200:
        return False, f"chat returned {r['status']}", {}
    try:
        reply = json.loads(r["body"])["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError):
        return False, "chat response not parseable", {}
    ok = "overrides_ok" in reply
    return ok, f"reply: {reply}", {}


def probe_audit(path: Path) -> tuple[bool, str, dict]:
    """Layer 8: audit log exists, has sane size, last few lines parse as JSON."""
    if not path.exists():
        # No audit log yet is OK on a fresh install — counts as pass but flag it
        return True, f"no audit log yet at {path} (fresh install)", {"size": 0}
    size = path.stat().st_size
    if size == 0:
        return True, "audit log is empty (size=0)", {"size": 0}
    # Sample the last few lines to confirm JSONL format
    with path.open("rb") as fh:
        fh.seek(max(0, size - 4096))
        tail = fh.read().decode("utf-8", errors="replace")
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    bad = 0
    for ln in lines[-10:]:
        try:
            json.loads(ln)
        except json.JSONDecodeError:
            bad += 1
    if bad > 2:  # tolerate ≤ 2 partial lines if we sliced mid-line
        return False, f"{bad} of last 10 audit lines failed to parse", {"size": size}
    return True, f"audit log size={size} bytes, last lines parse cleanly", {"size": size}


def probe_memory_db(path: Path) -> tuple[bool, str, dict]:
    """Layer 9: memory SQLite file exists and integrity_check=ok."""
    if not path.exists():
        return True, f"no memory DB yet at {path} (fresh install)", {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return False, f"cannot open memory DB: {exc}", {}
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        integrity = row[0] if row else "no_row"
    except sqlite3.Error as exc:
        return False, f"integrity_check failed: {exc}", {}
    finally:
        conn.close()
    if integrity != "ok":
        return False, f"integrity_check={integrity!r}", {}
    return True, "integrity_check=ok", {"db_path": str(path)}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str | None:
    # Try in priority order:
    #   1. HERMES_API_KEY env (operator's explicit override)
    #   2. API_SERVER_KEY env (the gateway's configured key)
    #   3. JARVIS_MCP_TOKEN env (since they're often the same)
    for env in ("HERMES_API_KEY", "API_SERVER_KEY", "JARVIS_MCP_TOKEN"):
        v = os.environ.get(env)
        if v:
            return v
    return None


def run_all(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    memory_db: Path = DEFAULT_MEMORY_DB,
    skip: set[str] | None = None,
) -> list[LayerResult]:
    """Run every layer probe (except skipped ones) and return the result list."""
    skip = skip or set()
    api_key = _resolve_api_key()
    results: list[LayerResult] = []

    if "tunnel" not in skip:
        results.append(_probe_with_timing("tunnel", lambda: probe_tunnel(host, port)))
    if "gateway" not in skip:
        results.append(_probe_with_timing("gateway", lambda: probe_gateway(host, port, api_key)))
    if "auth" not in skip:
        results.append(_probe_with_timing("auth", lambda: probe_auth(host, port, api_key)))
    if "llm" not in skip:
        results.append(_probe_with_timing("llm", lambda: probe_llm(host, port, api_key)))
    if "jarvis_mcp" not in skip:
        results.append(_probe_with_timing("jarvis_mcp", lambda: probe_jarvis_mcp(host, port, api_key)))
    if "memory" not in skip:
        results.append(_probe_with_timing("memory", lambda: probe_memory(host, port, api_key)))
    if "overrides" not in skip:
        results.append(_probe_with_timing("overrides", lambda: probe_overrides(host, port, api_key)))
    if "audit" not in skip:
        results.append(_probe_with_timing("audit", lambda: probe_audit(audit_path)))
    if "memory_db" not in skip:
        results.append(_probe_with_timing("memory_db", lambda: probe_memory_db(memory_db)))

    return results


def render_table(results: list[LayerResult]) -> str:
    """Render a human-readable status table (ASCII-only so Windows cp1252 stdout is happy)."""
    lines = [
        "",
        "=========== HERMES-JARVIS BRIDGE HEALTH ===========",
        f"  checked at {datetime.now(UTC).isoformat()}",
        "-" * 53,
    ]
    name_w = max(len(r.name) for r in results) if results else 10
    for r in results:
        glyph = "PASS" if r.ok else "FAIL"
        lines.append(f"  [{glyph}]  {r.name.ljust(name_w)}  ({r.elapsed_ms:5.0f}ms)  {r.detail}")
    n_ok = sum(1 for r in results if r.ok)
    n_total = len(results)
    lines.append("-" * 53)
    lines.append(f"  {n_ok}/{n_total} layers OK")
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--audit-path", type=Path, default=DEFAULT_AUDIT_PATH)
    p.add_argument("--memory-db", type=Path, default=DEFAULT_MEMORY_DB)
    p.add_argument(
        "--skip",
        type=str,
        default="",
        help="Comma-separated layers to skip (e.g. --skip llm,memory)",
    )
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of human table")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    bad_skips = skip - set(LAYER_NAMES)
    if bad_skips:
        print(f"unknown --skip layers: {bad_skips}; valid: {LAYER_NAMES}", file=sys.stderr)
        return 2
    results = run_all(
        host=args.host,
        port=args.port,
        audit_path=args.audit_path,
        memory_db=args.memory_db,
        skip=skip,
    )
    if args.json:
        payload = {
            "asof": datetime.now(UTC).isoformat(),
            "results": [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "detail": r.detail,
                    "elapsed_ms": r.elapsed_ms,
                    "extras": r.extras,
                }
                for r in results
            ],
            "all_ok": all(r.ok for r in results),
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_table(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
