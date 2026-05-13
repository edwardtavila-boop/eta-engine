"""
CLI-based LLM provider — shell out to ``claude`` and ``codex`` CLIs.
Uses subscription-based OAuth (Claude Pro / ChatGPT Plus/Pro) — no API keys.

Architecture
============
  Claude  → ``claude -p "..." --print --output-format text``
  Codex   → ``codex exec "..." --full-auto --ephemeral -C <workspace>``
  DeepSeek → API-based (handled by ``llm_provider.py``)

Both CLIs are invoked via ``npx`` by default (subscription auth). Direct
binary paths can be configured via env vars:
  ETA_CLAUDE_CLI  — path or command for claude (default: "npx @anthropic-ai/claude-code")
  ETA_CODEX_CLI   — path or command for codex  (default: "npx codex")
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CLIResponse:
    text: str
    provider: str  # "claude" | "codex"
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    elapsed_ms: float
    exit_code: int


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _find_npx() -> str:
    return shutil.which("npx") or "npx"


def _resolve_cli(env_key: str, fallback_binary: str, npx_package: str) -> list[str]:
    """Resolve a CLI invocation in priority order.

    Order (first match wins):
      1. ``$env_key`` environment variable (split on whitespace) — explicit override.
      2. ``shutil.which(fallback_binary)`` — direct binary on PATH (Windows: ``.cmd``).
      3. ``npx <npx_package>`` — npm-resolved invocation.

    Direct binaries are preferred over ``npx`` because they (a) are faster and
    (b) inherit OAuth state from the user's terminal-installed CLI.
    """
    # Lazy-load .env so ETA_CLAUDE_CLI / ETA_CODEX_CLI are visible.
    from eta_engine.brain.llm_provider import _ensure_dotenv  # noqa: PLC0415

    _ensure_dotenv()

    explicit = os.environ.get(env_key, "").strip()
    if explicit:
        parts = explicit.split()
        if len(parts) == 1:
            resolved = shutil.which(parts[0])
            if resolved:
                return [resolved]
        return parts

    direct = shutil.which(fallback_binary)
    if direct:
        return [direct]

    return [_find_npx(), npx_package]


def _claude_command() -> list[str]:
    """Return the command list to invoke Claude.

    Priority: ETA_CLAUDE_CLI env → ``claude`` on PATH → ``npx @anthropic-ai/claude-code``
    """
    return _resolve_cli("ETA_CLAUDE_CLI", "claude", "@anthropic-ai/claude-code")


def _codex_command() -> list[str]:
    """Return the command list to invoke Codex.

    Priority: ETA_CODEX_CLI env → ``codex`` on PATH → ``npx codex``
    """
    return _resolve_cli("ETA_CODEX_CLI", "codex", "codex")


DEFAULT_TIMEOUT_SEC = int(os.environ.get("ETA_CLI_TIMEOUT_SEC", "300"))
DEFAULT_WORKSPACE = str(Path(__file__).resolve().parents[2])


def _subprocess_env() -> dict[str, str]:
    """Build environment dict for CLI subprocess calls.

    On Windows, ``HOME`` may not be set but Claude CLI needs it to find
    ``~/.claude/credentials.json``. Map ``USERPROFILE`` → ``HOME``.

    Also filters out keys set to empty strings by ``python-dotenv`` so
    Windows user-level env vars (e.g. ``ANTHROPIC_API_KEY``) are not
    blanked by empty ``.env`` entries.
    """
    env = {k: v for k, v in os.environ.items() if v}
    if not env.get("HOME") and env.get("USERPROFILE"):
        env["HOME"] = env["USERPROFILE"]
    return env


# ---------------------------------------------------------------------------
# Claude (Anthropic) — subscription-based CLI
# ---------------------------------------------------------------------------


def call_claude(
    *,
    system_prompt: str = "",
    user_message: str,
    model: str = "sonnet",
    max_tokens: int = 4096,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    workspace: str | None = None,
    max_budget_usd: float = 1.00,
) -> CLIResponse:
    """Non-interactive Claude call via``claude -p --print``.

    Authenticates via OAuth (Claude Pro subscription). No API key needed.
    Uses ``npx @anthropic-ai/claude-code`` by default.

    ``max_tokens`` is passed as ``--max-budget-usd`` (Claude CLI doesn't
    support a direct token limit — budget acts as a soft cap).
    """
    base_cmd = _claude_command()
    started = time.time()

    cmd: list[str] = [
        *base_cmd,
        "-p",
        user_message,
        "--print",
        "--output-format",
        "text",
        "--model",
        model,
        "--max-budget-usd",
        str(max_budget_usd),
        "--no-session-persistence",
    ]

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    if workspace:
        cmd.extend(["--add-dir", workspace])

    logger.debug("claude cmd: %s [...]", " ".join(cmd[:6]))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace or DEFAULT_WORKSPACE,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        elapsed = (time.time() - started) * 1000
        logger.error("Claude CLI timed out after %ds", timeout)
        return CLIResponse(
            text="",
            provider="claude",
            model=model,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            elapsed_ms=elapsed,
            exit_code=-1,
        )
    except FileNotFoundError:
        elapsed = (time.time() - started) * 1000
        logger.error("Claude CLI not found: %s", " ".join(base_cmd))
        return CLIResponse(
            text="",
            provider="claude",
            model=model,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            elapsed_ms=elapsed,
            exit_code=-2,
        )

    elapsed = (time.time() - started) * 1000
    output = result.stdout.strip()
    if result.stderr:
        logger.debug("claude stderr: %s", result.stderr[:500])

    if result.returncode != 0 and not output:
        output = f"[claude error exit={result.returncode}] {result.stderr[:500]}"
    elif result.returncode != 0:
        logger.warning("claude exit=%d, output=%s, stderr=%s", result.returncode, output[:200], result.stderr[:200])

    return CLIResponse(
        text=output,
        provider="claude",
        model=model,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        elapsed_ms=elapsed,
        exit_code=result.returncode,
    )


# ---------------------------------------------------------------------------
# Codex (OpenAI) — subscription-based CLI
# ---------------------------------------------------------------------------


def call_codex(
    *,
    system_prompt: str = "",
    user_message: str,
    model: str = "o3",
    timeout: int = DEFAULT_TIMEOUT_SEC,
    workspace: str | None = None,
    sandbox: str = "workspace-write",
) -> CLIResponse:
    """Non-interactive Codex call via``codex exec``.

    Authenticates via OAuth (ChatGPT Plus/Pro subscription). No API key needed.
    Uses ``--ephemeral`` to avoid session clutter and ``--full-auto`` for
    unattended execution (sandbox=workspace-write, ask=on-request).
    Uses ``npx codex`` by default.
    """
    base_cmd = _codex_command()
    started = time.time()
    wd = workspace or DEFAULT_WORKSPACE

    cmd: list[str] = [
        *base_cmd,
        "exec",
        "--full-auto",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        wd,
        "-m",
        model,
    ]

    prompt = user_message
    if system_prompt:
        prompt = f"<system>\n{system_prompt}\n</system>\n\n{user_message}"

    # Windows command-line is capped at ~32k chars (CreateProcess limit).
    # For long prompts (esp. in `force_multiplier_chain` where verify receives
    # plan+impl), pass via stdin using `codex exec -` (read prompt from stdin).
    use_stdin = len(prompt) > 8000
    if use_stdin:
        cmd.append("-")  # codex exec convention: '-' = read prompt from stdin
    else:
        cmd.append(prompt)

    logger.debug("codex cmd: %s exec ... (stdin=%s, prompt_len=%d)", base_cmd[0], use_stdin, len(prompt))

    try:
        # When the prompt is positional, explicitly close stdin (DEVNULL) so
        # codex doesn't sit on `Reading additional input from stdin...`.
        # When using stdin (long prompt), pass it via the input= kwarg.
        run_kwargs: dict[str, Any] = dict(
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=wd,
            env=_subprocess_env(),
        )
        if use_stdin:
            run_kwargs["input"] = prompt
        else:
            run_kwargs["stdin"] = subprocess.DEVNULL
        result = subprocess.run(cmd, **run_kwargs)
    except subprocess.TimeoutExpired:
        elapsed = (time.time() - started) * 1000
        logger.error("Codex CLI timed out after %ds", timeout)
        return CLIResponse(
            text="",
            provider="codex",
            model=model,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            elapsed_ms=elapsed,
            exit_code=-1,
        )
    except FileNotFoundError:
        elapsed = (time.time() - started) * 1000
        logger.error("Codex CLI not found: %s", " ".join(base_cmd))
        return CLIResponse(
            text="",
            provider="codex",
            model=model,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            elapsed_ms=elapsed,
            exit_code=-2,
        )

    elapsed = (time.time() - started) * 1000
    output = result.stdout.strip()

    if result.returncode != 0 and not output:
        # Capture more stderr so the actual API error survives truncation.
        # Codex prints its banner + several KB of session info before the
        # real error message (which is what the operator needs to see).
        output = f"[codex error exit={result.returncode}] {result.stderr[:2000]}"

    return CLIResponse(
        text=output,
        provider="codex",
        model=model,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        elapsed_ms=elapsed,
        exit_code=result.returncode,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

# Cached per-binary availability — avoids 6 subprocess `--version` spawns
# per ``force_multiplier_chain`` call. Cache key includes the resolved
# command so changing $ETA_CLAUDE_CLI invalidates entries automatically.
_AVAILABILITY_TTL_SEC = 60.0
_availability_cache: dict[str, tuple[float, bool]] = {}
_availability_lock = threading.Lock()


def _check_cli_available(cmd_list: list[str], label: str) -> bool:
    """Return True if ``cmd --version`` exits 0. Cached for 60s.

    Narrow exception handling: only treats the canonical "binary missing"
    family of errors as unavailable. Anything else (PermissionError,
    OSError, etc.) is logged and re-raised — these are operator-visible
    bugs that shouldn't silently degrade routing.
    """
    cache_key = f"{label}:{' '.join(cmd_list)}"
    now = time.time()
    with _availability_lock:
        cached = _availability_cache.get(cache_key)
        if cached and (now - cached[0]) < _AVAILABILITY_TTL_SEC:
            return cached[1]
    cmd = [*cmd_list, "--version"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            env=_subprocess_env(),
        )
        ok = result.returncode == 0
    except (FileNotFoundError, NotADirectoryError):
        ok = False
    except subprocess.TimeoutExpired:
        logger.warning("%s --version timed out — treating as unavailable", label)
        ok = False
    except OSError as exc:
        logger.error("%s --version raised OSError: %s", label, exc)
        ok = False
    with _availability_lock:
        _availability_cache[cache_key] = (now, ok)
    return ok


def invalidate_availability_cache() -> None:
    """Force a fresh ``--version`` probe on next ``check_*_available`` call."""
    with _availability_lock:
        _availability_cache.clear()


def check_claude_available() -> bool:
    return _check_cli_available(_claude_command(), "claude")


def check_codex_available() -> bool:
    return _check_cli_available(_codex_command(), "codex")


def cli_provider_status() -> dict[str, Any]:
    return {
        "claude_available": check_claude_available(),
        "claude_command": " ".join(_claude_command()),
        "codex_available": check_codex_available(),
        "codex_command": " ".join(_codex_command()),
        "timeout_sec": DEFAULT_TIMEOUT_SEC,
    }
