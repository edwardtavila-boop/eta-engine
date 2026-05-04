"""Force-Multiplier CLI frontend — call the orchestrator from a terminal.

Without this script, using the integration requires writing Python:

    from eta_engine.brain.multi_model import route_and_execute
    from eta_engine.brain.model_policy import TaskCategory
    resp = route_and_execute(category=TaskCategory.ARCHITECTURE_DECISION, ...)

This wraps that into a one-liner the operator can run from PowerShell:

    python -m eta_engine.scripts.fm "Should I split bot.py into bot/ pkg?" \
        --category architecture_decision

Subcommands
===========
  ``ask``      — single ``route_and_execute`` call; prints the response
  ``chain``    — full Claude→DeepSeek→Codex pipeline
  ``status``   — show provider availability + recent telemetry summary
  ``log``      — tail the FM telemetry log

Examples
========
::

    fm ask "Why does the gauntlet require 50 trades?" \
        --category architecture_decision

    fm chain "Add OCO bracket retry-with-jitter to live BTC venue" \
        --skip verify

    fm status              # provider health + last 100 calls aggregated
    fm log -n 5            # last 5 telemetry records, pretty-printed
    fm log --provider claude   # only claude calls
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make the workspace root importable when invoked as `python script.py`.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eta_engine.brain.model_policy import (  # noqa: E402  (needs sys.path setup above)
    ForceProvider,
    TaskCategory,
)
from eta_engine.brain.multi_model import (  # noqa: E402
    force_multiplier_chain,
    force_multiplier_status,
    route_and_execute,
)
from eta_engine.brain.multi_model_telemetry import read_recent, summarize  # noqa: E402

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _category_choices() -> list[str]:
    return [c.value for c in TaskCategory]


def _provider_choices() -> list[str]:
    return [p.value for p in ForceProvider]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fm",
        description="Force-Multiplier CLI — route LLM tasks across DeepSeek/Claude/Codex",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="show INFO logs")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --- ask ---
    p_ask = sub.add_parser("ask", help="single routed call")
    p_ask.add_argument("task", help="the task description / user message")
    p_ask.add_argument(
        "--category", "-c",
        choices=_category_choices(),
        default=TaskCategory.SIMPLE_EDIT.value,
        help="TaskCategory to route by (default: simple_edit -> deepseek)",
    )
    p_ask.add_argument(
        "--system", "-s",
        default="",
        help="system prompt (defaults to empty)",
    )
    p_ask.add_argument(
        "--max-tokens", type=int, default=1024,
        help="max output tokens (default 1024)",
    )
    p_ask.add_argument(
        "--max-cost", type=float, default=0.10,
        help="hard cost ceiling in USD (default $0.10; 0 = no cap)",
    )
    p_ask.add_argument(
        "--temperature", type=float, default=0.7,
        help="sampling temperature (default 0.7)",
    )
    p_ask.add_argument(
        "--provider",
        choices=_provider_choices(),
        help="override the policy and force a specific provider",
    )
    p_ask.add_argument(
        "--workspace",
        help="working directory passed to CLI providers (default: current dir)",
    )
    p_ask.add_argument(
        "--json", action="store_true",
        help="emit the full MultiModelResponse as JSON instead of just text",
    )

    # --- chain ---
    p_chain = sub.add_parser("chain", help="run plan -> implement -> verify pipeline")
    p_chain.add_argument("task", help="the task description")
    p_chain.add_argument(
        "--skip", action="append", default=[],
        choices=["plan", "implement", "verify"],
        help="skip a stage (repeat to skip multiple)",
    )
    p_chain.add_argument(
        "--max-tokens", type=int, default=2048,
        help="per-stage token budget (default 2048)",
    )
    p_chain.add_argument(
        "--max-cost", type=float, default=1.00,
        help="total cost ceiling in USD across all stages (default $1.00)",
    )
    p_chain.add_argument(
        "--workspace",
        help="working directory passed to CLI providers",
    )
    p_chain.add_argument(
        "--json", action="store_true",
        help="emit the full ChainResult as JSON",
    )

    # --- status ---
    p_status = sub.add_parser("status", help="provider availability + telemetry summary")
    p_status.add_argument(
        "--json", action="store_true",
        help="emit JSON instead of human-readable",
    )
    p_status.add_argument(
        "--limit", type=int, default=1000,
        help="how many recent telemetry records to aggregate (default 1000)",
    )

    # --- log ---
    p_log = sub.add_parser("log", help="tail the FM telemetry log")
    p_log.add_argument(
        "-n", "--limit", type=int, default=20,
        help="how many records to show (default 20)",
    )
    p_log.add_argument(
        "--provider",
        choices=_provider_choices(),
        help="filter by actual_provider",
    )
    p_log.add_argument(
        "--fallbacks-only", action="store_true",
        help="only show records where fallback_used=True",
    )
    p_log.add_argument(
        "--json", action="store_true",
        help="emit raw JSONL lines (default: pretty table)",
    )

    return p


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_ask(args: argparse.Namespace) -> int:
    fp = ForceProvider(args.provider) if args.provider else None
    max_cost = args.max_cost if args.max_cost > 0 else None
    resp = route_and_execute(
        category=TaskCategory(args.category),
        system_prompt=args.system,
        user_message=args.task,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        workspace=args.workspace,
        force_provider=fp,
        max_cost_usd=max_cost,
    )
    if args.json:
        print(json.dumps({
            "provider": resp.provider.value,
            "model": resp.model,
            "tier": resp.tier.value if resp.tier else None,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cost_usd": resp.cost_usd,
            "elapsed_ms": resp.elapsed_ms,
            "fallback_used": resp.fallback_used,
            "fallback_reason": resp.fallback_reason,
            "text": resp.text,
        }, indent=2))
    else:
        # Header line, then the text.
        prov = resp.provider.value
        suffix = " (fallback)" if resp.fallback_used else ""
        print(f"# {prov}{suffix}  model={resp.model}  cost=${resp.cost_usd:.6f}  "
              f"elapsed={resp.elapsed_ms:.0f}ms", file=sys.stderr)
        if resp.fallback_used:
            print(f"# fallback_reason: {resp.fallback_reason}", file=sys.stderr)
        print(resp.text)
    return 0 if resp.text.strip() else 2


def _cmd_chain(args: argparse.Namespace) -> int:
    result = force_multiplier_chain(
        task=args.task,
        workspace=args.workspace,
        skip=tuple(args.skip),
        max_tokens=args.max_tokens,
        max_total_cost_usd=args.max_cost,
    )

    if args.json:
        out = {
            "task": result.task,
            "total_cost_usd": result.total_cost_usd,
            "total_elapsed_ms": result.total_elapsed_ms,
            "fallbacks_used": result.fallbacks_used,
            "aborted_at": result.aborted_at,
        }
        for stage_name in ("plan", "implement", "verify"):
            stage = getattr(result, stage_name)
            if stage:
                out[stage_name] = {
                    "provider": stage.provider.value,
                    "model": stage.model,
                    "fallback_used": stage.fallback_used,
                    "text": stage.text,
                }
        print(json.dumps(out, indent=2))
        return 0

    # Human-readable output
    bar = "=" * 70
    for stage_name in ("plan", "implement", "verify"):
        stage = getattr(result, stage_name)
        if stage is None:
            continue
        print(f"\n{bar}\n{stage_name.upper()}  ({stage.provider.value}"
              f"{' fallback' if stage.fallback_used else ''})\n{bar}")
        print(stage.text)

    print(f"\n{bar}\nSUMMARY\n{bar}")
    print(f"  total_cost_usd: ${result.total_cost_usd:.6f}")
    print(f"  total_elapsed:  {result.total_elapsed_ms:.0f}ms")
    if result.fallbacks_used:
        print("  fallbacks:")
        for reason in result.fallbacks_used:
            print(f"    - {reason}")
    if result.aborted_at:
        print(f"  ABORTED at stage: {result.aborted_at}")
    return 1 if result.aborted_at else 0


def _cmd_status(args: argparse.Namespace) -> int:
    health = force_multiplier_status()
    summary = summarize(limit=args.limit)
    if args.json:
        print(json.dumps({"health": health, "telemetry": summary}, indent=2, default=str))
        return 0

    print("Force-Multiplier providers:")
    for prov_name, prov_info in health["providers"].items():
        avail = prov_info.get("available")
        mark = "OK" if avail else "FAIL"
        print(f"  [{mark:4s}] {prov_name:9s}  {prov_info.get('role', '')}")
    print()
    print(f"Telemetry (last {args.limit} calls):")
    print(f"  total calls:    {summary['calls']}")
    print(f"  total spend:    ${summary['total_cost_usd']:.6f}")
    print(f"  fallback rate:  {summary.get('fallback_rate', 0) * 100:.1f}%")
    if summary["calls"]:
        print("  by provider:")
        for prov, slot in summary["by_provider"].items():
            print(f"    {prov:9s}  calls={slot['calls']:4d}  "
                  f"cost=${slot['cost_usd']:.6f}  fallbacks={slot['fallbacks_received']}")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    records = read_recent(limit=args.limit * 4 if args.provider or args.fallbacks_only else args.limit)
    if args.provider:
        records = [r for r in records if r.get("actual_provider") == args.provider]
    if args.fallbacks_only:
        records = [r for r in records if r.get("fallback_used")]
    records = records[-args.limit:]

    if not records:
        print("(no records)")
        return 0

    if args.json:
        for r in records:
            print(json.dumps(r, default=str))
        return 0

    # Pretty table
    print(f"{'TIMESTAMP':26s} {'CATEGORY':24s} {'PROVIDER':10s} "
          f"{'MODEL':22s} {'COST':>10s} {'MS':>7s} FB")
    print("-" * 110)
    for r in records:
        ts = r.get("ts", "")[:25]
        cat = (r.get("category") or "")[:24]
        prov = (r.get("actual_provider") or "")[:10]
        model = (r.get("model") or "")[:22]
        cost = r.get("cost_usd") or 0
        ms = r.get("elapsed_ms") or 0
        fb = "Y" if r.get("fallback_used") else "."
        print(f"{ts:26s} {cat:24s} {prov:10s} {model:22s} ${cost:>8.5f} {ms:>7.0f} {fb}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s [%(name)s] %(message)s",
    )

    handlers = {
        "ask": _cmd_ask,
        "chain": _cmd_chain,
        "status": _cmd_status,
        "log": _cmd_log,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
