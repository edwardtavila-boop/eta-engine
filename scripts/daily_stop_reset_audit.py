"""Read-only audit for the daily-loss reset handoff.

The daily-loss kill switch intentionally holds new entries until the next
operator-local midnight. This audit gives the VPS a small, durable receipt that
answers: did the daily stop clear, and is the paper-live transition gate ready
again? It never clears holds, submits orders, cancels, flattens, or promotes.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import daily_loss_killswitch, paper_live_transition_check, workspace_roots  # noqa: E402

_DEFAULT_OUT = workspace_roots.ETA_DAILY_STOP_RESET_AUDIT_PATH


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _status_for_killswitch(snapshot: dict[str, Any]) -> str:
    if bool(snapshot.get("disabled")):
        return "disabled"
    return "tripped" if bool(snapshot.get("tripped")) else "clear"


def _first_failed_gate(transition: dict[str, Any]) -> dict[str, Any]:
    gates = transition.get("gates")
    if not isinstance(gates, list):
        return {}
    for gate in gates:
        if isinstance(gate, dict) and gate.get("passed") is False:
            return dict(gate)
    return {}


def _paper_live_summary(transition: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(transition.get("status") or "unknown"),
        "critical_ready": bool(transition.get("critical_ready")),
        "paper_ready_bots": _as_int(transition.get("paper_ready_bots")),
        "operator_queue_effective_launch_blocked_count": _as_int(
            transition.get("operator_queue_effective_launch_blocked_count")
        ),
        "operator_queue_first_launch_blocker_op_id": transition.get("operator_queue_first_launch_blocker_op_id"),
        "operator_queue_first_launch_next_action": transition.get("operator_queue_first_launch_next_action"),
        "cache_stale": bool(transition.get("cache_stale")),
        "source_age_s": transition.get("source_age_s"),
        "error": transition.get("error"),
    }


def _daily_loss_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    out = dict(snapshot)
    out["status"] = _status_for_killswitch(out)
    out["tripped"] = bool(out.get("tripped"))
    out["disabled"] = bool(out.get("disabled"))
    out["reason"] = str(out.get("reason") or "")
    out["reset_in_s"] = _as_int(out.get("reset_in_s"))
    return out


def _operator_next_action(
    *,
    daily_loss: dict[str, Any],
    transition: dict[str, Any],
    first_failed_gate: dict[str, Any],
    status: str,
) -> str:
    if status == "held_until_reset":
        reset_hint = str(daily_loss.get("reset_display") or daily_loss.get("reset_at") or "the next local midnight")
        return (
            "Wait for the automatic daily-loss reset at "
            f"{reset_hint}; this VPS audit will re-check paper-live readiness every 5 minutes."
        )
    if status == "still_tripped_after_reset_window":
        return (
            "Daily-loss stop is still tripped after the reset window; verify trade-close ledger date, "
            "ETA_KILLSWITCH_TIMEZONE, and ETA_KILLSWITCH_DAILY_LIMIT_USD."
        )
    if status == "reset_cleared_ready":
        return "Watch the first supervisor tick after reset; do not bypass broker/router guards."
    if status == "reset_cleared_blocked":
        gate_action = str(first_failed_gate.get("next_action") or "").strip()
        if gate_action:
            return gate_action
        transition_action = str(transition.get("operator_queue_first_launch_next_action") or "").strip()
        if transition_action:
            return transition_action
        return "Rerun python -m eta_engine.scripts.paper_live_transition_check --json and clear the first failed gate."
    return "Inspect daily_stop_reset_audit stderr and rerun the audit on the VPS."


def build_reset_audit(
    *,
    killswitch_provider: Callable[[], dict[str, Any]] = daily_loss_killswitch.killswitch_status,
    transition_provider: Callable[[], dict[str, Any]] = paper_live_transition_check.build_transition_check,
) -> dict[str, Any]:
    """Return a read-only daily-stop reset audit payload."""
    try:
        daily_loss = _daily_loss_summary(killswitch_provider())
        transition = transition_provider()
        transition = transition if isinstance(transition, dict) else {}
    except Exception as exc:  # noqa: BLE001 - scheduled audit must fail soft.
        return {
            "schema_version": 1,
            "source": "daily_stop_reset_audit",
            "generated_at": _utc_now_iso(),
            "status": "error",
            "read_only": True,
            "safe_to_trade_mutation": False,
            "post_reset_ready": False,
            "operator_next_action": "Inspect daily_stop_reset_audit stderr and rerun the audit on the VPS.",
            "error": f"{type(exc).__name__}: {exc}",
            "daily_loss_killswitch": {},
            "paper_live_transition": {},
            "first_failed_gate": {},
        }

    first_failed_gate = _first_failed_gate(transition)
    tripped = bool(daily_loss.get("tripped"))
    reset_in_s = _as_int(daily_loss.get("reset_in_s"))
    transition_ready = (
        str(transition.get("status") or "") == "ready_to_launch_paper_live"
        and bool(transition.get("critical_ready"))
        and _as_int(transition.get("operator_queue_effective_launch_blocked_count")) == 0
    )

    if tripped and reset_in_s > 0:
        status = "held_until_reset"
    elif tripped:
        status = "still_tripped_after_reset_window"
    elif transition_ready:
        status = "reset_cleared_ready"
    else:
        status = "reset_cleared_blocked"

    return {
        "schema_version": 1,
        "source": "daily_stop_reset_audit",
        "generated_at": _utc_now_iso(),
        "status": status,
        "read_only": True,
        "safe_to_trade_mutation": False,
        "post_reset_ready": (not tripped) and transition_ready,
        "operator_next_action": _operator_next_action(
            daily_loss=daily_loss,
            transition=transition,
            first_failed_gate=first_failed_gate,
            status=status,
        ),
        "daily_loss_killswitch": daily_loss,
        "paper_live_transition": _paper_live_summary(transition),
        "first_failed_gate": first_failed_gate,
    }


def write_reset_audit(payload: dict[str, Any], path: Path = _DEFAULT_OUT) -> Path:
    """Atomically write the audit payload to the canonical runtime state tree."""
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_reset_audit()
    write_reset_audit(payload, args.out)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"{payload['status']} -> {args.out}")
    return 1 if payload.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
