"""
Print the operator-only TODO list with current state probes.

Why this exists
---------------
Several blockers between "code complete" and "live tick" are
operator-only -- broker funding, credential stashing in keyring,
alert credentials, and design-call decisions. Ancillary MCP OAuth
re-auth is tracked separately as observed integration debt. These accumulated through
the v0.1.64-v0.1.69 residual-risk closure work into a 17-item OP-list
captured in the session transcript. The operator wanted that list
as a one-liner CLI so they can run it on demand without scrolling
back through the chat.

This script reads the current state of the relevant probes and
prints the OP-list with each item marked as:

  * ``DONE`` -- evidence on disk says the operator already did it
  * ``BLOCKED`` -- pending operator action
  * ``OBSERVED`` -- partially done; check the detail line

State sources scanned
---------------------
* ``roadmap_state.json`` -> ``shared_artifacts.mcp_status`` for OAuth state
* ``state/kill_switch_latch.json`` for the persistent latch state
* ``configs/ibkr.yaml`` + ``configs/tastytrade.yaml`` presence (config stubs)
* ``configs/tradovate.yaml`` presence (DORMANT; informational)
* env-var presence for the per-venue credential keys
* ``venues/router.py::DORMANT_BROKERS`` for the dormancy mandate state
* ``docs/preflight_dryrun_report.json`` for the most recent T-minus

Usage
-----
    python -m eta_engine.scripts.operator_action_queue

    # JSON output for piping into other tools (firm-tracker, dashboard)
    python -m eta_engine.scripts.operator_action_queue --json

    # Verbose: show the source of truth for each verdict
    python -m eta_engine.scripts.operator_action_queue --verbose
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Possible verdicts for a single OP item.
VERDICT_DONE = "DONE"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_OBSERVED = "OBSERVED"
VERDICT_UNKNOWN = "UNKNOWN"


@dataclass
class OpItem:
    """One operator action with its current verdict + detail."""

    op_id: str
    title: str
    verdict: str = VERDICT_UNKNOWN
    detail: str = ""
    where: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "title": self.title,
            "verdict": self.verdict,
            "detail": self.detail,
            "where": self.where,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _read_roadmap_state() -> dict[str, Any]:
    """Return ``roadmap_state.json`` parsed, or empty dict on any failure."""
    p = ROOT / "roadmap_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_preflight_report() -> dict[str, Any]:
    """Return ``docs/preflight_dryrun_report.json`` parsed, or empty dict."""
    p = ROOT / "docs" / "preflight_dryrun_report.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_dormant_brokers() -> set[str]:
    """Read ``DORMANT_BROKERS`` from venues.router via lazy import."""
    try:
        from eta_engine.venues.router import DORMANT_BROKERS
    except ImportError:
        return set()
    return set(DORMANT_BROKERS)


def _env_key_present(name: str) -> bool:
    """Return True if ``name`` resolves via ``SecretsManager`` or os.environ."""
    if os.environ.get(name):
        return True
    with contextlib.suppress(Exception):
        from eta_engine.core.secrets import SecretsManager

        if SecretsManager(env_file=ROOT / ".env").get(name, required=False):
            return True
    return False


def _config_present(name: str) -> bool:
    """``configs/<name>`` file exists."""
    return (ROOT / "configs" / name).exists()


def _kill_switch_latch_state() -> str:
    """Return ``ARMED`` / ``TRIPPED`` / ``ABSENT`` / ``UNREADABLE``."""
    p = ROOT / "state" / "kill_switch_latch.json"
    if not p.exists():
        return "ABSENT"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "UNREADABLE"
    return data.get("state", "UNKNOWN")


# ---------------------------------------------------------------------------
# Per-OP-item probes
# ---------------------------------------------------------------------------


def _op1_fund_ibkr() -> OpItem:
    item = OpItem(
        op_id="OP-1",
        title="Fund IBKR primary account (>= $5,000 cleared, tier-A bucket)",
        where="IBKR portal",
    )
    # We cannot directly probe the broker balance without creds; the best
    # signal is whether the credential gate has populated IBKR_CP_BASE_URL +
    # IBKR_ACCOUNT_ID. Funding is downstream of creds.
    creds_present = _env_key_present("IBKR_CP_BASE_URL") and _env_key_present(
        "IBKR_ACCOUNT_ID",
    )
    if creds_present:
        item.verdict = VERDICT_OBSERVED
        item.detail = "IBKR creds populated; funding state cannot be probed without auth -- check IBKR portal manually"
    else:
        item.verdict = VERDICT_BLOCKED
        item.detail = "IBKR creds absent; populate per OP-3 first"
    item.evidence = {
        "ibkr_cp_base_url_present": _env_key_present("IBKR_CP_BASE_URL"),
        "ibkr_account_id_present": _env_key_present("IBKR_ACCOUNT_ID"),
    }
    return item


def _op2_fund_tastytrade() -> OpItem:
    item = OpItem(
        op_id="OP-2",
        title="Fund Tastytrade fallback (recommended; not blocking first live tick)",
        where="Tastytrade portal",
    )
    creds_present = (
        _env_key_present("TASTY_API_BASE_URL")
        and _env_key_present("TASTY_ACCOUNT_NUMBER")
        and _env_key_present("TASTY_SESSION_TOKEN")
    )
    if creds_present:
        item.verdict = VERDICT_OBSERVED
        item.detail = (
            "Tastytrade creds populated; funding state cannot be "
            "probed without auth -- check Tastytrade portal manually"
        )
    else:
        item.verdict = VERDICT_OBSERVED
        item.detail = (
            "Tastytrade fallback creds absent; recommended before failover drills, "
            "but not blocking first live tick while IBKR is primary."
        )
    item.evidence = {
        "tasty_api_base_url_present": _env_key_present("TASTY_API_BASE_URL"),
        "tasty_account_present": _env_key_present(
            "TASTY_ACCOUNT_NUMBER",
        ),
        "tasty_session_present": _env_key_present(
            "TASTY_SESSION_TOKEN",
        ),
        "launch_blocker": False,
        "role": "secondary_fallback",
    }
    return item


def _op3_ibkr_creds() -> OpItem:
    item = OpItem(
        op_id="OP-3",
        title="Populate IBKR_CP_BASE_URL + IBKR_ACCOUNT_ID in keyring on trading host",
        where="Trading host (keyring or .env)",
    )
    base = _env_key_present("IBKR_CP_BASE_URL")
    acct = _env_key_present("IBKR_ACCOUNT_ID")
    if base and acct:
        item.verdict = VERDICT_DONE
        item.detail = "Both keys resolve via SecretsManager / env"
    else:
        item.verdict = VERDICT_BLOCKED
        missing = []
        if not base:
            missing.append("IBKR_CP_BASE_URL")
        if not acct:
            missing.append("IBKR_ACCOUNT_ID")
        item.detail = f"Missing: {', '.join(missing)}"
    item.evidence = {"ibkr_cp_base_url": base, "ibkr_account_id": acct}
    return item


def _op4_tastytrade_creds() -> OpItem:
    item = OpItem(
        op_id="OP-4",
        title=("Populate TASTY_API_BASE_URL + TASTY_ACCOUNT_NUMBER + TASTY_SESSION_TOKEN in keyring on trading host"),
        where="Trading host (keyring or .env)",
    )
    keys = (
        "TASTY_API_BASE_URL",
        "TASTY_ACCOUNT_NUMBER",
        "TASTY_SESSION_TOKEN",
    )
    present = {k: _env_key_present(k) for k in keys}
    if all(present.values()):
        item.verdict = VERDICT_DONE
        item.detail = "All 3 Tastytrade keys resolve"
    else:
        item.verdict = VERDICT_OBSERVED
        missing = [k for k, v in present.items() if not v]
        item.detail = (
            f"Missing fallback key(s): {', '.join(missing)}. "
            "Recommended for failover, not blocking first live tick while IBKR is primary."
        )
    item.evidence = {
        **present,
        "launch_blocker": False,
        "role": "secondary_fallback",
    }
    return item


def _op5_telegram_creds() -> OpItem:
    item = OpItem(
        op_id="OP-5",
        title="Populate TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID",
        where="Trading host (keyring or .env)",
    )
    bot = _env_key_present("TELEGRAM_BOT_TOKEN")
    chat = _env_key_present("TELEGRAM_CHAT_ID")
    if bot and chat:
        item.verdict = VERDICT_DONE
        item.detail = "Both keys resolve"
    else:
        item.verdict = VERDICT_BLOCKED
        missing = []
        if not bot:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat:
            missing.append("TELEGRAM_CHAT_ID")
        item.detail = f"Missing: {', '.join(missing)}"
    item.evidence = {
        "TELEGRAM_BOT_TOKEN": bot,
        "TELEGRAM_CHAT_ID": chat,
    }
    return item


def _op6_op7_op8_mcp_oauth(roadmap: dict[str, Any]) -> list[OpItem]:
    """OAuth state for ancillary MCPs that are not trading-launch blockers."""
    mcp_status = roadmap.get("shared_artifacts", {}).get("mcp_status") or roadmap.get("mcp_status") or {}
    items: list[OpItem] = []
    for op_id, mcp in (
        ("OP-6", "jotform"),
        ("OP-7", "amplitude"),
        ("OP-8", "coupler"),
    ):
        item = OpItem(
            op_id=op_id,
            title=f"OAuth re-auth for {mcp} MCP",
            where="Browser",
        )
        status = mcp_status.get(mcp)
        if status is None:
            item.verdict = VERDICT_UNKNOWN
            item.detail = f"roadmap_state.json has no entry for mcp_status.{mcp}"
        elif status == "needs_auth":
            item.verdict = VERDICT_OBSERVED
            item.detail = (
                "needs_auth -- run OAuth flow in browser when this product/integration "
                "surface is needed; not blocking trading launch readiness"
            )
        elif status in ("ok", "authed", "authorized"):
            item.verdict = VERDICT_DONE
            item.detail = f"status={status}"
        else:
            item.verdict = VERDICT_OBSERVED
            item.detail = f"status={status}"
        item.evidence = {
            "mcp_status": status,
            "launch_blocker": False,
            "scope": "ancillary_mcp_integration",
        }
        items.append(item)
    return items


def _op9_clock_drift(preflight: dict[str, Any]) -> OpItem:
    item = OpItem(
        op_id="OP-9",
        title=("NTP resync (Windows w32tm /resync /force or Linux ntpdate) if preflight clock_drift flips RED"),
        where="Trading host",
    )
    gates = preflight.get("gates") or []
    drift_gate = next((g for g in gates if g.get("name") == "clock_drift"), None)
    if drift_gate is None:
        item.verdict = VERDICT_UNKNOWN
        item.detail = "preflight report has no clock_drift gate"
    elif drift_gate.get("status") == "PASS":
        item.verdict = VERDICT_DONE
        item.detail = drift_gate.get("detail", "clock_drift PASS")
    else:
        item.verdict = VERDICT_BLOCKED
        item.detail = f"clock_drift {drift_gate.get('status')}: {drift_gate.get('detail', '')}"
    item.evidence = drift_gate or {}
    return item


def _op10_tradovate_dormancy() -> OpItem:
    item = OpItem(
        op_id="OP-10",
        title="Confirm Tradovate remains dormant unless explicitly reactivated",
        where="venues/router.py",
    )
    dormant = _read_dormant_brokers()
    if "tradovate" in dormant:
        item.verdict = VERDICT_DONE
        item.detail = (
            f"Tradovate is DORMANT as required by current broker policy (set: {sorted(dormant)}). "
            "IBKR remains primary and Tastytrade secondary."
        )
    elif not dormant:
        item.verdict = VERDICT_BLOCKED
        item.detail = (
            "DORMANT_BROKERS is empty; Tradovate appears active. Current policy requires "
            "Tradovate to stay dormant unless the operator explicitly reactivates it in code and docs together."
        )
    else:
        item.verdict = VERDICT_BLOCKED
        item.detail = (
            f"Tradovate not in DORMANT_BROKERS but other brokers are: {sorted(dormant)}. "
            "This violates the current broker policy unless an explicit reactivation batch landed."
        )
    item.evidence = {
        "dormant_brokers": sorted(dormant),
        "policy": {
            "active_primary": "IBKR",
            "active_secondary": "Tastytrade",
            "tradovate": "dormant",
        },
    }
    return item


def _op11_killverdict_synthesis() -> OpItem:
    return OpItem(
        op_id="OP-11",
        title=(
            "Track M2 KillVerdict synthesis on sustained drift "
            "after H1 calibrator empirics from >= 30-day live-paper window"
        ),
        verdict=VERDICT_OBSERVED,
        detail=(
            "Parked until >=30 days of live-paper H1 calibrator empirics exist. "
            "Reconciler correctly stays observation-only; no operator launch block today."
        ),
        where="core/broker_equity_reconciler.py + configs/kill_switch.yaml",
        evidence={
            "prerequisite": ">=30d live-paper H1 calibrator empirics",
            "current_mode": "observation_only",
            "launch_blocker": False,
        },
    )


def _op12_per_bot_drift() -> OpItem:
    return OpItem(
        op_id="OP-12",
        title=("Track M1 per-bot drift detection (multi-account scope expansion)"),
        verdict=VERDICT_OBSERVED,
        detail=(
            "Parked until multi-account venue introspection exists. Single-account today; "
            "M1 ships when fleet grows, so this is not a current launch block."
        ),
        where="New scope",
        evidence={
            "prerequisite": "multi-account venue introspection",
            "current_scope": "single_account",
            "launch_blocker": False,
        },
    )


def _op13_strategy_review() -> OpItem:
    return OpItem(
        op_id="OP-13",
        title="Strategy-generator monthly review (Sonnet tier)",
        verdict=VERDICT_OBSERVED,
        detail=("Standing cadence per docs/live_launch_runbook.md post-launch review section"),
        where="Cron / manual",
    )


def _op14_quarterly_adversarial() -> OpItem:
    return OpItem(
        op_id="OP-14",
        title="Quarterly full adversarial cycle (Opus 5x window)",
        verdict=VERDICT_OBSERVED,
        detail=(
            "Cost-budget decision; budget the 5x window for Opus tier "
            "(risk-advocate, quant-researcher, devils-advocate)"
        ),
        where="Cron / manual",
    )


def _op15_crypto_seed() -> OpItem:
    item = OpItem(
        op_id="OP-15",
        title="Confirm crypto_seed remains non-edge BTC exposure accumulator",
        where="python -m eta_engine.scripts.paper_live_launch_check --bots crypto_seed --json",
    )
    try:
        from eta_engine.scripts.paper_live_launch_check import _audit_bot
        from eta_engine.strategies.per_bot_registry import get_for_bot
    except Exception as exc:  # noqa: BLE001 -- operator queue must stay readable
        item.verdict = VERDICT_UNKNOWN
        item.detail = f"Unable to audit crypto_seed readiness: {exc}"
        item.evidence = {"error": str(exc)}
        return item

    assignment = get_for_bot("crypto_seed")
    if assignment is None:
        item.verdict = VERDICT_BLOCKED
        item.detail = "crypto_seed missing from per-bot strategy registry"
        item.evidence = {"bot_id": "crypto_seed", "missing_assignment": True}
        return item

    result = _audit_bot(assignment)
    evidence = result.get("evidence", {})
    launch_role = evidence.get("launch_role") if isinstance(evidence, dict) else None
    if result.get("status") == "READY" and launch_role == "non_edge_exposure":
        item.verdict = VERDICT_DONE
        item.detail = (
            "crypto_seed is ready as a non-edge BTC exposure accumulator; "
            "it is no longer treated as a blocked alpha redesign item."
        )
        item.evidence = result
        return item

    item.verdict = VERDICT_BLOCKED
    warnings = result.get("warnings") or result.get("issues") or ["readiness check did not clear"]
    item.detail = f"crypto_seed readiness still needs work: {warnings[0]}"
    item.evidence = result
    return item


def _num(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _research_priority_key(blocker: dict[str, object]) -> tuple[float, ...]:
    """Sort OP-16 blockers by next-action value instead of registry order."""
    evidence = blocker.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    source = evidence.get("full_history_smoke")
    if not isinstance(source, dict):
        source = evidence

    oos = _num(source.get("agg_oos_sharpe", source.get("candidate_agg_oos_sharpe")))
    dsr = _num(source.get("dsr_pass_fraction", source.get("candidate_dsr_pass_fraction")))
    provider_backed = bool(evidence.get("provider_backed"))

    if oos is not None and oos < 0:
        bucket = 3.0
    elif oos is None:
        bucket = 2.0
    elif provider_backed and dsr is not None and dsr >= 0.5:
        bucket = 0.0
    else:
        bucket = 1.0

    return (
        bucket,
        -(dsr if dsr is not None else -1.0),
        -(oos if oos is not None else -999.0),
    )


def _op16_strategy_research_candidates() -> OpItem:
    item = OpItem(
        op_id="OP-16",
        title="Resolve research-candidate strategy gates before promotion",
        where="python -m eta_engine.scripts.paper_live_launch_check --json",
    )
    try:
        from eta_engine.scripts.paper_live_launch_check import _audit_bot
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS
    except Exception as exc:  # noqa: BLE001 -- operator queue must stay readable
        item.verdict = VERDICT_UNKNOWN
        item.detail = f"Unable to collect launch-check research warnings: {exc}"
        item.evidence = {"error": str(exc)}
        return item

    warnings = [
        _audit_bot(assignment)
        for assignment in ASSIGNMENTS
        if (assignment.extras or {}).get("promotion_status") == "research_candidate"
    ]
    active = [result for result in warnings if result.get("status") == "WARN"]
    if not active:
        item.verdict = VERDICT_DONE
        item.detail = "No research-candidate launch warnings are active"
        item.evidence = {"overall_severity": "green", "blocked_bots": []}
        return item

    blockers: list[dict[str, object]] = []
    for result in active:
        bot_id = str(result.get("bot_id") or "")
        strategy_id = str(result.get("strategy_id") or "")
        warnings_text = result.get("warnings") or []
        summary = (
            str(warnings_text[0])
            if isinstance(warnings_text, list) and warnings_text
            else "research candidate gate not fully passed"
        )
        blockers.append(
            {
                "name": bot_id,
                "summary": summary,
                "strategy_id": strategy_id,
                "next_commands": [
                    (
                        "python -m eta_engine.scripts.paper_live_launch_check "
                        f"--bots {bot_id} --json"
                    ),
                ],
                "evidence": result.get("evidence", {}),
            }
        )

    blockers.sort(key=_research_priority_key)
    first = blockers[0]
    item.verdict = VERDICT_BLOCKED
    item.detail = (
        f"{len(blockers)} research candidate bot(s) still below promotion gate; "
        f"first={first['name']}: {first['summary']}"
    )
    item.evidence = {
        "overall_severity": "amber",
        "blocked_bots": [b["name"] for b in blockers],
        "blockers": blockers,
    }
    return item


def _op17_phase_advancement() -> OpItem:
    return OpItem(
        op_id="OP-17",
        title=("Phase 4/5/6 live-tiny advancement decisions (gauntlet to 2 contracts; add NQ; add tier-B)"),
        verdict=VERDICT_OBSERVED,
        detail=("Cumulative-trade-count + drawdown gates. See live_launch_runbook.md Phase 4-6 sections."),
        where="Runbook gates",
    )


def _op18_vps_failover_readiness() -> OpItem:
    item = OpItem(
        op_id="OP-18",
        title="Resolve current VPS failover red/amber blockers",
        where="python -m eta_engine.scripts.vps_failover_summary --json",
    )
    try:
        from eta_engine.scripts import vps_failover_summary

        summary = vps_failover_summary.build_summary(skip_backup_test=True)
    except Exception as exc:  # noqa: BLE001 -- operator queue must stay readable
        item.verdict = VERDICT_UNKNOWN
        item.detail = f"Unable to collect VPS failover summary: {exc}"
        item.evidence = {"error": str(exc)}
        return item

    severity = str(summary.get("overall_severity", "unknown"))
    blockers = summary.get("blockers", [])
    counts = summary.get("counts", {})
    if severity in {"red", "amber"}:
        item.verdict = VERDICT_BLOCKED
    elif severity == "green":
        item.verdict = VERDICT_DONE
    else:
        item.verdict = VERDICT_UNKNOWN

    if blockers:
        first = blockers[0]
        next_commands = first.get("next_commands") or []
        command_hint = f"; next: {next_commands[0]}" if next_commands else ""
        item.detail = (
            f"{severity.upper()} with {len(blockers)} blocker(s); "
            f"first={first.get('name')}: {first.get('summary')}{command_hint}"
        )
    else:
        item.detail = f"{severity.upper()} failover summary; counts={counts}"
    item.evidence = {
        "overall_severity": severity,
        "counts": counts,
        "blockers": blockers,
        "generated_at": summary.get("generated_at"),
        "exit_code": summary.get("exit_code"),
    }
    return item


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def collect_items() -> list[OpItem]:
    """Build the OP item list with current state probed."""
    roadmap = _read_roadmap_state()
    preflight = _read_preflight_report()
    items: list[OpItem] = []
    items.append(_op1_fund_ibkr())
    items.append(_op2_fund_tastytrade())
    items.append(_op3_ibkr_creds())
    items.append(_op4_tastytrade_creds())
    items.append(_op5_telegram_creds())
    items.extend(_op6_op7_op8_mcp_oauth(roadmap))
    items.append(_op9_clock_drift(preflight))
    items.append(_op10_tradovate_dormancy())
    items.append(_op11_killverdict_synthesis())
    items.append(_op12_per_bot_drift())
    items.append(_op13_strategy_review())
    items.append(_op14_quarterly_adversarial())
    items.append(_op15_crypto_seed())
    items.append(_op16_strategy_research_candidates())
    items.append(_op17_phase_advancement())
    items.append(_op18_vps_failover_readiness())
    return items


def _verdict_glyph(verdict: str) -> str:
    return {
        VERDICT_DONE: "[OK]",
        VERDICT_BLOCKED: "[!!]",
        VERDICT_OBSERVED: "[~~]",
        VERDICT_UNKNOWN: "[??]",
    }.get(verdict, "[??]")


def render_text(items: list[OpItem], *, verbose: bool = False) -> str:
    """Render the OP list as a colour-free, fixed-width text block."""
    lines: list[str] = []
    counts: dict[str, int] = {}
    for item in items:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1

    lines.append("EVOLUTIONARY TRADING ALGO -- operator action queue")
    lines.append("=" * 64)
    summary = " | ".join(
        f"{v}: {counts.get(v, 0)}"
        for v in (
            VERDICT_DONE,
            VERDICT_BLOCKED,
            VERDICT_OBSERVED,
            VERDICT_UNKNOWN,
        )
    )
    lines.append(f"Summary: {summary}")
    lines.append("-" * 64)

    for item in items:
        glyph = _verdict_glyph(item.verdict)
        lines.append(
            f"{glyph} {item.op_id:5s} {item.verdict:9s} {item.title}",
        )
        if item.detail:
            lines.append(f"        {item.detail}")
        if item.where:
            lines.append(f"        where: {item.where}")
        if verbose and item.evidence:
            lines.append(f"        evidence: {item.evidence}")
        lines.append("")
    lines.append("=" * 64)
    lines.append(
        "Glyphs: [OK] DONE  [!!] BLOCKED  [~~] OBSERVED  [??] UNKNOWN",
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of text (for piping into other tools)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="include the evidence dict for each item",
    )
    args = p.parse_args(argv)

    items = collect_items()

    if args.json:
        out = {
            "items": [item.as_dict() for item in items],
            "summary": {
                "DONE": sum(1 for i in items if i.verdict == VERDICT_DONE),
                "BLOCKED": sum(1 for i in items if i.verdict == VERDICT_BLOCKED),
                "OBSERVED": sum(1 for i in items if i.verdict == VERDICT_OBSERVED),
                "UNKNOWN": sum(1 for i in items if i.verdict == VERDICT_UNKNOWN),
            },
        }
        print(json.dumps(out, indent=2))
    else:
        print(render_text(items, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(main())
