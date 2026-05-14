"""
Deploy // live_claude_smoke
===========================
Legacy compatibility wrapper.

Claude/Anthropic is disabled by operator policy. This module is kept only so
old aliases or scheduled commands do not break; it delegates to the active
Codex-first smoke and writes under workspace_roots.ETA_RUNTIME_STATE_DIR.

Prefer:
    .venv\\Scripts\\python.exe -m deploy.scripts.live_codex_smoke --live
"""

from __future__ import annotations

from eta_engine.deploy.scripts.live_codex_smoke import main as codex_main


def main(argv: list[str] | None = None) -> int:
    print("[live-claude-smoke] Claude is disabled by policy; running Codex smoke instead.")
    return codex_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
