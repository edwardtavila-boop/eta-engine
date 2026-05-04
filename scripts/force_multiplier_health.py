"""Force Multiplier health probe.

Run this anytime to verify the three-provider integration end-to-end.
Reports per-provider availability, runs a tiny live call where safe,
and prints actionable next steps for any failure.

Usage
-----
    python -m eta_engine.scripts.force_multiplier_health
    python -m eta_engine.scripts.force_multiplier_health --live    # also do live calls

The --live flag costs ~$0.000005 on DeepSeek and uses ~5s of Claude/Codex
subscription quota. Skip it on quota-constrained days.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure project root is importable when run as `python script.py`
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eta_engine.brain.cli_provider import (
    call_claude,
    call_codex,
    check_claude_available,
    check_codex_available,
    cli_provider_status,
)
from eta_engine.brain.llm_provider import chat_completion, ModelTier, native_provider_info
from eta_engine.brain.model_policy import ForceProvider, TaskCategory, force_provider_for
from eta_engine.brain.multi_model import _classify_cli_failure, force_multiplier_status


def _hr(label: str = "") -> None:
    bar = "-" * 70
    print(f"\n{bar}\n{label}\n{bar}" if label else bar)


def probe_deepseek(*, live: bool) -> tuple[bool, str]:
    info = native_provider_info()
    if not info.get("deepseek_key_configured"):
        return False, "DEEPSEEK_API_KEY missing — set it in eta_engine/.env"
    if not live:
        return True, "configured (skipped live call)"
    try:
        # DeepSeek V4 Flash has thinking enabled by default — budget below
        # ~150 tokens is entirely consumed by reasoning with no visible output.
        # Use HAIKU tier (non-thinking variant) for the probe.
        r = chat_completion(
            tier=ModelTier.HAIKU,
            system_prompt="Reply with one short word.",
            user_message="Reply only with the word: READY",
            max_tokens=256,
            temperature=0.0,
        )
        if r.text.strip():
            return True, f"live OK ({r.input_tokens}+{r.output_tokens} tok, ${r.cost_usd:.6f})"
        return False, "empty response (model returned no text — check thinking budget)"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "401" in msg or "Authentication" in msg:
            return False, f"API key rejected — rotate DEEPSEEK_API_KEY ({msg[:80]})"
        return False, f"call failed: {msg[:120]}"


def probe_claude(*, live: bool) -> tuple[bool, str]:
    if not check_claude_available():
        return False, "claude CLI not installed — run: npm install -g @anthropic-ai/claude-code"
    status = cli_provider_status()
    if not live:
        return True, f"installed: {status['claude_command']} (skipped live call)"
    resp = call_claude(
        user_message="Reply only with the word: READY",
        model="haiku",
        max_budget_usd=0.05,
        timeout=60,
    )
    failure = _classify_cli_failure(resp)
    if failure is None and resp.text.strip():
        return True, f"live OK ({resp.elapsed_ms:.0f}ms, exit={resp.exit_code})"
    if failure == "auth":
        return False, (
            "claude not authenticated for non-interactive use — run `claude setup-token` "
            "(`claude login` only auths the chat session; -p needs a long-lived token)"
        )
    if failure == "quota":
        return False, "claude monthly quota exhausted — wait for reset or upgrade plan"
    if failure == "timeout":
        return False, "claude CLI timed out — check network / subscription"
    return False, f"failure={failure or 'empty'} text={resp.text[:120]!r}"


def probe_codex(*, live: bool) -> tuple[bool, str]:
    if not check_codex_available():
        return False, "codex CLI not installed — run: npm install -g @openai/codex"
    status = cli_provider_status()
    if not live:
        return True, f"installed: {status['codex_command']} (skipped live call)"
    # ChatGPT subscriptions only support gpt-5.4 (not o3/o4-mini); use that
    # for the live probe so we don't get a misleading 400-not-supported error.
    resp = call_codex(
        user_message="Reply only with the word: READY",
        model="gpt-5.4",
        timeout=120,
    )
    failure = _classify_cli_failure(resp)
    if failure is None and resp.text.strip():
        return True, f"live OK ({resp.elapsed_ms:.0f}ms, exit={resp.exit_code})"
    if failure == "auth":
        return False, "codex not authenticated — run `codex login` (uses ChatGPT Plus/Pro subscription)"
    if failure == "quota":
        return False, "codex monthly quota exhausted — resets at start of next billing cycle"
    if failure == "timeout":
        return False, "codex CLI timed out — long-horizon tasks can take >2 min"
    return False, f"failure={failure or 'empty'} text={resp.text[:120]!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Force Multiplier health probe")
    parser.add_argument("--live", action="store_true",
                        help="Also run live calls (costs ~$0.000005 + sub quota)")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG logs")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    _hr("Force Multiplier Health Probe")
    print(f"workspace: {ROOT}")
    print(f"live calls: {'ENABLED' if args.live else 'DISABLED (use --live to enable)'}")

    _hr("Provider status")
    probes = [
        ("DEEPSEEK (Worker Bee)", probe_deepseek),
        ("CLAUDE   (Lead Architect)", probe_claude),
        ("CODEX    (Systems Expert)", probe_codex),
    ]

    results: list[tuple[str, bool, str]] = []
    for name, fn in probes:
        ok, msg = fn(live=args.live)
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}]  {name:30s}  {msg}")
        results.append((name, ok, msg))

    _hr("Routing table (24 task categories)")
    rt = force_multiplier_status()["routing_table"]
    by_provider: dict[str, list[str]] = {}
    for cat, prov in rt.items():
        by_provider.setdefault(prov, []).append(cat)
    for prov in ("claude", "deepseek", "codex"):
        cats = by_provider.get(prov, [])
        print(f"  {prov:9s} ({len(cats):2d} tasks): {', '.join(cats[:4])}{' ...' if len(cats) > 4 else ''}")

    _hr("Summary")
    pass_count = sum(1 for _, ok, _ in results if ok)
    print(f"  {pass_count}/3 providers ready")
    if pass_count < 3:
        print("\n  Next steps:")
        for name, ok, msg in results:
            if not ok:
                print(f"    - {name}: {msg}")
    print()
    return 0 if pass_count == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
