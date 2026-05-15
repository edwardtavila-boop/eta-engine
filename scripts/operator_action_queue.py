"""
Print the operator-only TODO list with current state probes.

Why this exists
---------------
Several blockers between "code complete" and "live tick" are
operator-only -- broker funding, credential stashing in keyring,
alert credentials, and design-call decisions. Ancillary MCP OAuth
re-auth is tracked separately as observed integration debt. These accumulated through
the v0.1.64-v0.1.69 residual-risk closure work into a 20-item OP-list
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

from eta_engine.scripts import workspace_roots  # noqa: E402

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


def _runtime_state_path(name: str) -> Path:
    """Return a canonical runtime-state artifact path."""
    return ROOT.parent / "var" / "eta_engine" / "state" / name


def _read_runtime_state(name: str) -> dict[str, Any]:
    """Return a canonical runtime-state JSON artifact, or empty dict."""
    p = _runtime_state_path(name)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_json_path(path: Path) -> dict[str, Any]:
    """Return a JSON object from ``path``, or empty dict on any failure."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _gateway_exe_present(gateway_dir: Path = Path(r"C:\Jts\ibgateway\1046")) -> bool:
    """Return whether the canonical IB Gateway 10.46 executable exists.

    IBC renames the live gateway binary to ``ibgateway1.exe`` during its
    wrapper install, so both names must count as a recovered canonical
    runtime.
    """
    return any((gateway_dir / name).exists() for name in ("ibgateway.exe", "ibgateway1.exe"))


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
        item.detail = "Both keys resolve; Hermes alert transport is ready"
    else:
        item.verdict = VERDICT_BLOCKED
        missing = []
        if not bot:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat:
            missing.append("TELEGRAM_CHAT_ID")
        item.detail = (
            f"Missing: {', '.join(missing)}. Hermes alert delivery stays degraded, "
            "but this does not block the paper_live trading path."
        )
    item.evidence = {
        "TELEGRAM_BOT_TOKEN": bot,
        "TELEGRAM_CHAT_ID": chat,
        "launch_blocker": False,
        "role": "alerts_transport",
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
        item.verdict = VERDICT_OBSERVED
        item.detail = (
            "preflight report has no clock_drift gate yet; rerun the live-tiny "
            "preflight on the trading host. Missing evidence is visible, but "
            "does not block launch unless the gate reports FAIL."
        )
        item.evidence = {
            "gate_missing": True,
            "launch_blocker": False,
            "next_command": "python -m eta_engine.scripts.live_tiny_preflight_dryrun",
        }
        return item

    status = str(drift_gate.get("status", "UNKNOWN")).upper()
    if status == "PASS":
        item.verdict = VERDICT_DONE
        item.detail = drift_gate.get("detail", "clock_drift PASS")
        item.evidence = {**drift_gate, "launch_blocker": False}
    elif status == "FAIL":
        item.verdict = VERDICT_BLOCKED
        item.detail = f"clock_drift {drift_gate.get('status')}: {drift_gate.get('detail', '')}"
        item.evidence = {**drift_gate, "launch_blocker": True}
    elif status == "SKIP":
        item.verdict = VERDICT_OBSERVED
        item.detail = (
            f"clock_drift SKIP: {drift_gate.get('detail', '')}. "
            "Visible for operator follow-up; not launch-blocking unless it flips FAIL."
        )
        item.evidence = {**drift_gate, "launch_blocker": False}
    else:
        item.verdict = VERDICT_UNKNOWN
        item.detail = f"clock_drift {status}: {drift_gate.get('detail', '')}"
        item.evidence = {**drift_gate, "launch_blocker": False}
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
    if not isinstance(evidence, dict):
        evidence = {}
    extras = getattr(assignment, "extras", {}) or {}
    if not isinstance(extras, dict):
        extras = {}
    launch_role = evidence.get("launch_role")
    registry_promotion_status = extras.get("promotion_status")
    registry_non_edge = (
        launch_role == "non_edge_exposure"
        or registry_promotion_status == "non_edge_strategy"
        or bool(extras.get("non_edge_reason"))
    )
    issues = result.get("issues") or []
    warnings = result.get("warnings") or []
    audit_promotion_status = result.get("promotion_status")
    registry_deactivated = bool(extras.get("deactivated"))
    audit_deactivated = audit_promotion_status == "deactivated" or launch_role == "deactivated"
    if result.get("status") == "READY" and registry_non_edge and not issues and not warnings:
        item.verdict = VERDICT_DONE
        item.detail = (
            "crypto_seed is ready as a non-edge BTC exposure accumulator; "
            "it is no longer treated as a blocked alpha redesign item."
        )
        enriched_evidence = dict(evidence)
        enriched_evidence.setdefault("launch_role", "non_edge_exposure")
        if isinstance(registry_promotion_status, str) and registry_promotion_status:
            enriched_evidence["registry_promotion_status"] = registry_promotion_status
        if bool(extras.get("deactivated")):
            enriched_evidence["registry_deactivated"] = True
        if audit_promotion_status:
            enriched_evidence["audit_promotion_status"] = audit_promotion_status
        item.evidence = {**result, "evidence": enriched_evidence}
        return item
    if audit_deactivated and not issues:
        item.verdict = VERDICT_BLOCKED
        item.detail = (
            "crypto_seed is deactivated/excluded from launch; keep it visible for BTC exposure review, "
            "but do not block paper_live launch for active paper-ready bots."
        )
        item.evidence = {
            **result,
            "launch_blocker": False,
            "launch_role": "deactivated",
            "registry_deactivated": registry_deactivated,
            "audit_promotion_status": audit_promotion_status,
        }
        return item

    item.verdict = VERDICT_BLOCKED
    blockers = warnings or issues or ["readiness check did not clear"]
    item.detail = f"crypto_seed readiness still needs work: {blockers[0]}"
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


def _op16_next_commands(bot_id: str) -> list[str]:
    """Return actionable research commands, not a launch-check loop."""
    return [
        f"python -m eta_engine.scripts.run_research_grid --source registry --bots {bot_id} --report-policy runtime",
        f"python -m eta_engine.scripts.paper_live_launch_check --bots {bot_id} --json",
    ]


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
                "next_commands": _op16_next_commands(bot_id),
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
        "launch_blocker": False,
        "launch_role": "strategy_optimization_backlog",
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
        title="Resolve current VPS failover red blockers; review amber warnings",
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
    if severity == "red":
        item.verdict = VERDICT_BLOCKED
        launch_blocker = True
    elif severity == "amber":
        item.verdict = VERDICT_OBSERVED
        launch_blocker = False
    elif severity == "green":
        item.verdict = VERDICT_DONE
        launch_blocker = False
    else:
        item.verdict = VERDICT_UNKNOWN
        launch_blocker = False

    if blockers:
        first = blockers[0]
        next_commands = first.get("next_commands") or []
        command_hint = f"; next: {next_commands[0]}" if next_commands else ""
        label = "blocker" if launch_blocker else "warning"
        item.detail = (
            f"{severity.upper()} failover {label} with {len(blockers)} item(s); "
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
        "launch_blocker": launch_blocker,
    }
    return item


def _op19_ibgateway_1046_runtime() -> OpItem:
    item = OpItem(
        op_id="OP-19",
        title="Install/configure canonical IB Gateway and recover TWS API 4002",
        where="python -m eta_engine.scripts.ibkr_surface_status --skip-client-portal",
    )
    install = _read_runtime_state("ibgateway_install.json")
    repair = _read_runtime_state("ibgateway_repair.json")
    reauth = _read_runtime_state("ibgateway_reauth.json")
    tws = _read_runtime_state("tws_watchdog.json")
    repair_gateway_dir = repair.get("gateway_dir")
    gateway_exe = _gateway_exe_present()
    if not gateway_exe and isinstance(repair_gateway_dir, str) and repair_gateway_dir.strip():
        gateway_exe = _gateway_exe_present(Path(repair_gateway_dir))
    single_source = repair.get("single_source") if isinstance(repair.get("single_source"), dict) else {}
    task_states = single_source.get("task_states") if isinstance(single_source.get("task_states"), dict) else {}
    task_canonical = bool(single_source.get("gateway_task_canonical"))
    repair_tasks = repair.get("tasks") if isinstance(repair.get("tasks"), dict) else {}
    eta_gateway_task_result = str(repair_tasks.get("ETA-IBGateway") or "").strip()
    tws_healthy = tws.get("healthy") is True
    handshake_ok = (tws.get("details") or {}).get("handshake_ok") is True
    credential_status = reauth.get("credential_status") if isinstance(reauth.get("credential_status"), dict) else {}
    gateway_authority = reauth.get("gateway_authority") if isinstance(reauth.get("gateway_authority"), dict) else {}
    non_authoritative_gateway_host = (
        reauth.get("status") == "non_authoritative_gateway_host"
        or gateway_authority.get("allowed") is False
    )
    missing_ibc_credentials = reauth.get("status") == "missing_ibc_credentials" or (
        credential_status and credential_status.get("ready") is False and reauth.get("operator_action_required")
    )

    next_commands: list[str]
    if non_authoritative_gateway_host:
        item.verdict = VERDICT_BLOCKED
        item.detail = (
            "This host is not the VPS Gateway authority; do not repair, start, or reauth "
            "IB Gateway from this desktop. Verify the VPS authority marker and recovery lane "
            "on the 24/7 server."
        )
        next_commands = [
            (
                "On the VPS only: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                ".\\eta_engine\\deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps"
            ),
            "On the VPS only: python -m eta_engine.scripts.ibgateway_reauth_controller --execute",
        ]
        severity = "red"
    elif gateway_exe and task_canonical and tws_healthy and handshake_ok:
        item.verdict = VERDICT_DONE
        item.detail = "IB Gateway, recovery tasks, and TWS API 4002 handshake are healthy."
        next_commands = ["python -m eta_engine.scripts.ibkr_surface_status --skip-client-portal"]
        severity = "green"
    elif missing_ibc_credentials:
        item.verdict = VERDICT_BLOCKED
        item.detail = str(
            reauth.get("operator_action")
            or "IBC credentials are missing or still placeholder; seed them before Gateway auto-recovery."
        )
        next_commands = [
            r".\eta_engine\deploy\scripts\set_ibc_credentials.ps1 -PromptForPassword",
            "python -m eta_engine.scripts.ibgateway_reauth_controller --execute",
        ]
        severity = "red"
    elif not gateway_exe:
        signature = install.get("authenticode_status") or "unknown"
        sha = install.get("installer_sha256") or "missing"
        item.verdict = VERDICT_BLOCKED
        item.detail = f"IB Gateway 10.46 is not installed; installer Authenticode={signature}, sha256={sha}."
        next_commands = [
            (
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                ".\\eta_engine\\deploy\\scripts\\install_ibgateway_1046.ps1 "
                "-Install -RepairAfterInstall"
            ),
        ]
        severity = "red"
    elif gateway_exe and tws_healthy and handshake_ok and (not task_canonical or not task_states):
        item.verdict = VERDICT_BLOCKED
        item.detail = (
            "IB Gateway 10.46 and TWS API 4002 are healthy, but the recovery/startup "
            "tasks are still not canonical for unattended launch."
        )
        if eta_gateway_task_result:
            item.detail += f" ETA-IBGateway update result: {eta_gateway_task_result}"
        next_commands = [
            (
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                ".\\eta_engine\\deploy\\scripts\\repair_ibgateway_vps.ps1 "
                "-ApplyJtsIni -ApplyVmOptions -RepairTasks -EnforceSingleSource -UseIbc"
            ),
        ]
        severity = "red"
    elif not task_canonical or not task_states:
        item.verdict = VERDICT_BLOCKED
        item.detail = "IB Gateway 10.46 exists, but config/recovery tasks are not canonical."
        next_commands = [
            (
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                ".\\eta_engine\\deploy\\scripts\\repair_ibgateway_vps.ps1 "
                "-ApplyJtsIni -ApplyVmOptions -RepairTasks -EnforceSingleSource"
            ),
        ]
        severity = "red"
    else:
        status = reauth.get("status") or tws.get("status") or "not_ready"
        item.verdict = VERDICT_BLOCKED
        item.detail = f"IB Gateway 10.46 is present, but TWS API 4002 is not handshake-ready; status={status}."
        next_commands = [
            "python -m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002",
            "python -m eta_engine.scripts.ibgateway_reauth_controller --execute",
        ]
        severity = "red"

    item.evidence = {
        "overall_severity": severity,
        "gateway_exe_present": gateway_exe,
        "task_canonical": task_canonical,
        "task_update_result": eta_gateway_task_result,
        "task_states": task_states,
        "tws_healthy": tws_healthy,
        "handshake_ok": handshake_ok,
        "install": install,
        "repair": repair,
        "reauth": reauth,
        "gateway_authority": gateway_authority,
        "non_authoritative_gateway_host": non_authoritative_gateway_host,
        "credential_status": credential_status,
        "tws_watchdog": tws,
        "allow_unsigned_requires_source_confirmation": True,
        "blockers": [
            {
                "name": "ibgateway_1046_runtime",
                "summary": item.detail,
                "next_commands": next_commands,
                "evidence": {
                    "gateway_exe_present": gateway_exe,
                    "task_canonical": task_canonical,
                    "task_update_result": eta_gateway_task_result,
                    "tws_healthy": tws_healthy,
                    "handshake_ok": handshake_ok,
                    "non_authoritative_gateway_host": non_authoritative_gateway_host,
                    "gateway_authority": gateway_authority,
                    "allow_unsigned_requires_source_confirmation": True,
                },
            },
        ],
    }
    return item


def _symbols_from_reconcile_rows(payload: dict[str, Any], key: str) -> list[str]:
    """Extract sorted symbols from a reconcile row list."""
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    symbols = {
        str(row.get("symbol") or "").strip().upper()
        for row in rows
        if isinstance(row, dict) and str(row.get("symbol") or "").strip()
    }
    return sorted(symbols)


def _symbols_from_reconcile(payload: dict[str, Any], key: str) -> list[str]:
    """Extract symbol summaries from hardening or raw reconcile payloads."""
    symbol_key = f"{key}_symbols"
    symbols = payload.get(symbol_key)
    if isinstance(symbols, list):
        return sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    return _symbols_from_reconcile_rows(payload, key)


def _reconcile_human_summary(
    *,
    broker_only: list[str],
    supervisor_only: list[str],
    divergent: list[str],
) -> str:
    parts: list[str] = []
    if broker_only:
        parts.append(f"broker-only: {', '.join(broker_only)}")
    if supervisor_only:
        parts.append(f"supervisor-only: {', '.join(supervisor_only)}")
    if divergent:
        parts.append(f"divergent: {', '.join(divergent)}")
    return "; ".join(parts) if parts else "no mismatches"


def _op20_supervisor_broker_reconcile() -> OpItem:
    item = OpItem(
        op_id="OP-20",
        title="Reconcile broker vs supervisor open positions",
        where="python -m eta_engine.scripts.supervisor_broker_reconcile_heartbeat --json",
    )
    hardening = _read_json_path(workspace_roots.ETA_VPS_OPS_HARDENING_AUDIT_PATH)
    gates = hardening.get("safety_gates") if isinstance(hardening.get("safety_gates"), dict) else {}
    hardening_gate = gates.get("supervisor_reconcile") if isinstance(gates.get("supervisor_reconcile"), dict) else {}
    raw_reconcile = _read_json_path(workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH)
    source = hardening_gate or raw_reconcile

    if not source:
        item.verdict = VERDICT_UNKNOWN
        item.detail = (
            "No current broker/supervisor reconcile artifact is available. Refresh the read-only "
            "VPS reconcile heartbeat before using the operator queue as launch truth."
        )
        item.evidence = {
            "overall_severity": "amber",
            "launch_blocker": False,
            "source": "missing_reconcile_artifact",
            "hardening_path": str(workspace_roots.ETA_VPS_OPS_HARDENING_AUDIT_PATH),
            "reconcile_path": str(workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH),
        }
        return item

    broker_only = _symbols_from_reconcile(source, "broker_only")
    supervisor_only = _symbols_from_reconcile(source, "supervisor_only")
    divergent = _symbols_from_reconcile(source, "divergent")
    mismatch_count = int(source.get("mismatch_count") or len(broker_only) + len(supervisor_only) + len(divergent))
    blocking_mismatch_count = len(broker_only) + len(divergent)
    ready = source.get("ready")
    if ready is None:
        ready = blocking_mismatch_count == 0
    summary = _reconcile_human_summary(
        broker_only=broker_only,
        supervisor_only=supervisor_only,
        divergent=divergent,
    )
    action_candidates = hardening.get("next_actions") if isinstance(hardening.get("next_actions"), list) else []
    reconcile_actions = [
        str(action)
        for action in action_candidates
        if action and "reconcile" in str(action).lower()
    ]
    if blocking_mismatch_count:
        human_action = reconcile_actions[0] if reconcile_actions else (
            f"Do not unlock new entries: reconcile broker/supervisor positions ({summary}) "
            "before clearing the supervisor entry halt"
        )
    else:
        human_action = (
            f"Paper-live may continue: broker exposure is not unknown, but clean up supervisor-only "
            f"paper state ({summary}) after confirming broker flat for the listed symbol(s)."
        )

    if ready is True and mismatch_count == 0:
        item.verdict = VERDICT_DONE
        item.detail = "Broker and supervisor open-position snapshots match."
        severity = "green"
        launch_blocker = False
    elif blocking_mismatch_count:
        item.verdict = VERDICT_BLOCKED
        item.detail = f"{mismatch_count} broker/supervisor mismatch(es): {summary}."
        severity = "red"
        launch_blocker = True
    else:
        item.verdict = VERDICT_BLOCKED
        item.detail = (
            f"{mismatch_count} supervisor-only paper-state mismatch(es): {summary}. "
            "No broker-only or divergent exposure was found, so this is not a paper-launch blocker."
        )
        severity = "amber"
        launch_blocker = False

    item.evidence = {
        "overall_severity": severity,
        "launch_blocker": launch_blocker,
        "blocking_mismatch_count": blocking_mismatch_count,
        "source": source.get("source") or "supervisor_reconcile",
        "status": source.get("status"),
        "ready": blocking_mismatch_count == 0,
        "mismatch_count": mismatch_count,
        "broker_only_symbols": broker_only,
        "supervisor_only_symbols": supervisor_only,
        "divergent_symbols": divergent,
        "checked_at": source.get("checked_at"),
        "age_s": source.get("age_s"),
        "order_action_allowed": False,
        "blockers": [
            {
                "name": "supervisor_broker_reconcile",
                "summary": item.detail,
                "next_commands": [
                    human_action,
                    "python -m eta_engine.scripts.supervisor_broker_reconcile_heartbeat --json",
                ],
                "evidence": {
                    "broker_only_symbols": broker_only,
                    "supervisor_only_symbols": supervisor_only,
                    "divergent_symbols": divergent,
                    "order_action_allowed": False,
                },
            }
        ],
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
    items.append(_op19_ibgateway_1046_runtime())
    items.append(_op20_supervisor_broker_reconcile())
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
