"""
Deploy // live_codex_smoke
==========================
Codex-first live smoke for the Force Multiplier lane.

This replaces the old Claude/Anthropic smoke. Codex is the active
Lead Architect / Systems Expert lane; DeepSeek remains the paid API worker.
The smoke writes a canonical artifact under workspace_roots.ETA_RUNTIME_STATE_DIR.

Run from C:\\EvolutionaryTradingAlgo\\eta_engine:
    .venv\\Scripts\\python.exe -m deploy.scripts.live_codex_smoke
    .venv\\Scripts\\python.exe -m deploy.scripts.live_codex_smoke --live

Default mode verifies local policy wiring and Codex CLI discovery without
spending subscription quota. Use --live for a tiny Codex CLI round trip.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from eta_engine.brain.cli_provider import call_codex, check_codex_available, cli_provider_status
from eta_engine.brain.multi_model import _classify_cli_failure
from eta_engine.scripts import workspace_roots


DEFAULT_MODEL = "gpt-5.5"
SMOKE_FILE = "live_codex_smoke.json"


def _artifact_path(state_dir: Path | None = None) -> Path:
    return (state_dir or workspace_roots.ETA_RUNTIME_STATE_DIR) / SMOKE_FILE


def _write_artifact(payload: dict[str, object], *, state_dir: Path | None = None) -> Path:
    out = _artifact_path(state_dir)
    workspace_roots.ensure_dir(out.parent)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


def _base_payload(*, live: bool) -> dict[str, object]:
    status = cli_provider_status()
    return {
        "ts": datetime.now(UTC).isoformat(),
        "lane": "codex",
        "legacy_claude_policy": "disabled",
        "live": live,
        "codex_available": bool(status.get("codex_available")),
        "codex_command": status.get("codex_command", ""),
        "claude_disabled_by_policy": bool(status.get("claude_disabled_by_policy", True)),
    }


def run_smoke(*, live: bool, state_dir: Path | None = None) -> tuple[int, dict[str, object], Path]:
    load_dotenv()
    payload = _base_payload(live=live)

    if not check_codex_available():
        payload.update(
            {
                "ok": False,
                "status": "codex_cli_missing",
                "message": "Codex CLI is not installed or not discoverable; install @openai/codex and run codex login.",
            }
        )
        return 2, payload, _write_artifact(payload, state_dir=state_dir)

    if not live:
        payload.update(
            {
                "ok": True,
                "status": "policy_ready",
                "message": "Codex is the active architect/review lane; live round trip skipped.",
            }
        )
        return 0, payload, _write_artifact(payload, state_dir=state_dir)

    model = os.environ.get("ETA_CODEX_DEFAULT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    response = call_codex(
        system_prompt="You are verifying the ETA Force Multiplier Codex lane. Reply with one word.",
        user_message="Reply only with the word: READY",
        model=model,
        timeout=int(os.environ.get("ETA_CLI_TIMEOUT_SEC", "300") or "300"),
    )
    failure = _classify_cli_failure(response)
    ok = failure is None and bool(response.text.strip())
    payload.update(
        {
            "ok": ok,
            "status": "live_ok" if ok else f"live_failed_{failure or 'empty'}",
            "model": response.model,
            "elapsed_ms": response.elapsed_ms,
            "exit_code": response.exit_code,
            "failure": failure,
            "response_preview": response.text.strip()[:500],
        }
    )
    return (0 if ok else 3), payload, _write_artifact(payload, state_dir=state_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex-first Force Multiplier smoke")
    parser.add_argument("--live", action="store_true", help="Run a tiny Codex CLI round trip.")
    parser.add_argument("--json", action="store_true", help="Print the JSON artifact payload.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Override runtime state directory for tests.")
    args = parser.parse_args(argv)

    rc, payload, out = run_smoke(live=args.live, state_dir=args.state_dir)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        mark = "OK" if rc == 0 else "FAIL"
        print(f"[live-codex-smoke] {mark}: {payload.get('message') or payload.get('status')}")
        print(f"[live-codex-smoke] artifact: {out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
