"""Force Multiplier health probe.

Run this anytime to verify the subscription-first integration end-to-end.
Reports allowed-provider availability, runs a tiny live call where safe,
and prints actionable next steps for any failure. Claude is intentionally
disabled by operator policy and is not counted as a failed provider.

Usage
-----
    python -m eta_engine.scripts.force_multiplier_health
    python -m eta_engine.scripts.force_multiplier_health --live    # also do live calls

The --live flag costs a tiny DeepSeek call and uses Codex subscription quota.
Skip it on quota-constrained days.
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

from datetime import UTC  # noqa: E402  (datetime import follows sys.path setup)

from eta_engine.brain.cli_provider import (  # noqa: E402  (needs sys.path setup above)
    call_codex,
    check_codex_available,
    cli_provider_status,
)
from eta_engine.brain.llm_provider import ModelTier, chat_completion, native_provider_info  # noqa: E402
from eta_engine.brain.multi_model import _classify_cli_failure, force_multiplier_status  # noqa: E402
from eta_engine.scripts import workspace_roots  # noqa: E402  (needs sys.path setup above)

#: Env-var override for the JSON snapshot path. Tests / smoke runs set
#: this to redirect the artifact to a tmp directory; production leaves
#: it unset so the canonical workspace path resolves.
_PATH_ENV_VAR: str = "ETA_FM_HEALTH_SNAPSHOT_PATH"


def default_path() -> Path:
    """Canonical workspace path for the FM-health JSON snapshot.

    Resolution order:
      * ``$ETA_FM_HEALTH_SNAPSHOT_PATH`` if set (used by tests / cron)
      * ``workspace_roots.ETA_FM_HEALTH_SNAPSHOT_PATH`` (canonical, under
        ``<workspace>/var/eta_engine/state/fm_health.json``)
    """
    override = os.environ.get(_PATH_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return workspace_roots.ETA_FM_HEALTH_SNAPSHOT_PATH


def default_legacy_path() -> Path:
    """Legacy in-repo path for the FM-health JSON snapshot.

    Used as a one-shot read fallback during the migration window so a
    dashboard that hasn't been updated yet can still discover a stale
    snapshot at the legacy path. NEVER used as a write target.
    """
    return workspace_roots.ETA_LEGACY_FM_HEALTH_SNAPSHOT_PATH


def resolve_existing_path() -> Path:
    """Return the path to read the snapshot from, preferring canonical.

    Falls back to the legacy in-repo path only when the canonical file
    does not exist *and* the legacy file does. The write path is always
    :func:`default_path`.
    """
    canonical = default_path()
    if canonical.exists():
        return canonical
    legacy = default_legacy_path()
    if legacy.exists():
        return legacy
    return canonical


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
    _ = live
    return True, "disabled by operator policy; Codex handles architect/review"


def probe_codex(*, live: bool) -> tuple[bool, str]:
    if not live:
        status = cli_provider_status(probe=False)
        if not status.get("codex_available"):
            return False, "codex CLI entrypoint not found - run: npm install -g @openai/codex"
        return True, f"path discovered: {status['codex_command']} (skipped live call)"
    if not check_codex_available():
        return False, "codex CLI not installed — run: npm install -g @openai/codex"
    resp = call_codex(
        user_message="Reply only with the word: READY",
        model=os.environ.get("ETA_CODEX_DEFAULT_MODEL", "gpt-5.5").strip() or "gpt-5.5",
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


def _probe_results(*, live: bool) -> list[tuple[str, bool, str]]:
    probes = [
        ("CODEX    (Lead Architect / Systems Expert)", probe_codex),
        ("DEEPSEEK (Worker Bee)", probe_deepseek),
    ]
    return [(name, *fn(live=live)) for name, fn in probes]


def _emit_json(*, results: list[tuple[str, bool, str]], live: bool, write_to: Path | None) -> None:
    """Emit a machine-readable status snapshot (for cron / dashboards)."""
    import json
    from datetime import datetime

    pass_count = sum(1 for _, ok, _ in results if ok)
    payload = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "live": live,
        "pass_count": pass_count,
        "total_count": len(results),
        "all_ready": pass_count == len(results),
        "providers": [
            {
                "name": name.split()[0].lower(),
                "label": name,
                "ok": ok,
                "message": msg,
            }
            for name, ok, msg in results
        ],
    }
    serialized = json.dumps(payload, indent=2)
    if write_to is not None:
        write_to.parent.mkdir(parents=True, exist_ok=True)
        write_to.write_text(serialized, encoding="utf-8")
    else:
        print(serialized)


def main() -> int:
    parser = argparse.ArgumentParser(description="Force Multiplier health probe")
    parser.add_argument("--live", action="store_true", help="Also run live calls (costs ~$0.000005 + sub quota)")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG logs")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout (machine-readable)")
    parser.add_argument(
        "--json-out",
        nargs="?",
        const="",  # bare flag -> use canonical default_path()
        help=(
            "Write JSON snapshot to this path. Useful with Task Scheduler — "
            "bare --json-out writes to var/eta_engine/state/fm_health.json "
            "(or $ETA_FM_HEALTH_SNAPSHOT_PATH override) so dashboards can "
            "poll it."
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress human-readable output (use with --json-out for cron)"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    results = _probe_results(live=args.live)
    pass_count = sum(1 for _, ok, _ in results if ok)

    # JSON snapshot to disk (cron-friendly): write before any printing so a
    # crash mid-print still leaves a usable artifact. ``--json-out`` with no
    # value (or empty string) routes to the canonical workspace path under
    # ``var/eta_engine/state/`` so callers don't have to hard-code legacy
    # in-repo locations.
    if args.json_out is not None:
        write_to = Path(args.json_out) if args.json_out else default_path()
        _emit_json(results=results, live=args.live, write_to=write_to)

    if args.json:
        _emit_json(results=results, live=args.live, write_to=None)
        return 0 if pass_count == len(results) else 1

    if args.quiet:
        return 0 if pass_count == len(results) else 1

    _hr("Force Multiplier Health Probe")
    print(f"workspace: {ROOT}")
    print(f"live calls: {'ENABLED' if args.live else 'DISABLED (use --live to enable)'}")

    _hr("Provider status")
    for name, ok, msg in results:
        mark = "OK " if ok else "FAIL"
        print(f"  [{mark}]  {name:30s}  {msg}")

    _hr("Routing table (24 task categories)")
    rt = force_multiplier_status()["routing_table"]
    by_provider: dict[str, list[str]] = {}
    for cat, prov in rt.items():
        by_provider.setdefault(prov, []).append(cat)
    for prov in ("codex", "deepseek", "claude"):
        cats = by_provider.get(prov, [])
        print(f"  {prov:9s} ({len(cats):2d} tasks): {', '.join(cats[:4])}{' ...' if len(cats) > 4 else ''}")

    _hr("Summary")
    print(f"  {pass_count}/{len(results)} allowed providers ready")
    if pass_count < len(results):
        print("\n  Next steps:")
        for name, ok, msg in results:
            if not ok:
                print(f"    - {name}: {msg}")
    print()
    return 0 if pass_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
