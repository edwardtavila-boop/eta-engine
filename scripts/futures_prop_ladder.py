"""Build the automated futures prop-lane ladder.

The ladder is a read/write status artifact, not a route flipper. It keeps
`volume_profile_mnq` as the only primary live candidate and reserves a
small number of runner-up slots for Nasdaq/S&P/Dow minis and micros while
their evidence matures in paper/research.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.scripts import workspace_roots  # noqa: E402

PRIMARY_BOT = "volume_profile_mnq"
RUNNER_BOTS = (
    "volume_profile_nq",
    "rsi_mr_mnq_v2",
    "mym_sweep_reclaim",
    "mes_sweep_reclaim_v2",
    "mnq_anchor_sweep",
)
RUNNER_SLOTS = 3
FOCUS_ROOTS = frozenset({"MNQ", "NQ", "MES", "ES", "MYM", "YM"})
DEFAULT_OUT = workspace_roots.ETA_RUNTIME_STATE_DIR / "futures_prop_ladder_latest.json"
DEFAULT_STRICT_GATE_DIR = workspace_roots.ETA_ENGINE_ROOT / "reports"


def _symbol_root(symbol: str) -> str:
    raw = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())
    for root in sorted(FOCUS_ROOTS, key=len, reverse=True):
        if raw.startswith(root):
            return root
    return raw.rstrip("0123456789")


def _instrument_family(root: str) -> str:
    if root in {"MNQ", "NQ"}:
        return "nasdaq"
    if root in {"MES", "ES"}:
        return "sp500"
    if root in {"MYM", "YM"}:
        return "dow"
    return "other"


def _load_json(path: Path) -> Any:  # noqa: ANN401
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _readiness_rows_from_snapshot(
    path: Path = workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [row for row in payload["rows"] if isinstance(row, dict)]
    return []


def _prop_readiness_from_snapshot(
    path: Path = workspace_roots.ETA_RUNTIME_STATE_DIR / "tradovate_prop_readiness.json",
) -> dict[str, Any]:
    payload = _load_json(path)
    if isinstance(payload, dict):
        return payload
    try:
        from eta_engine.scripts.tradovate_prop_readiness import build_report  # noqa: PLC0415

        report = build_report(prop_account="blusky_50k", phase="predeposit")
        return report if isinstance(report, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _strict_gate_metrics_from_report(path: Path) -> dict[str, dict[str, Any]]:
    payload = _load_json(path)
    if not isinstance(payload, list):
        return {}
    metrics: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        bot_id = str(row.get("bot") or row.get("bot_id") or "").strip()
        if bot_id:
            metrics[bot_id] = row
    return metrics


def _latest_strict_gate_metrics(reports_dir: Path = DEFAULT_STRICT_GATE_DIR) -> dict[str, dict[str, Any]]:
    reports = sorted(
        reports_dir.glob("strict_gate*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    merged: dict[str, dict[str, Any]] = {}
    for path in reports:
        metrics = _strict_gate_metrics_from_report(path)
        for bot_id, row in metrics.items():
            merged.setdefault(bot_id, row)
    return merged


def _evidence_grade(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return "missing_strict_gate"
    trades = int(metrics.get("trades") or 0)
    sh_def = float(metrics.get("sh_def") or 0.0)
    long_ok = bool(metrics.get("L"))
    short_ok = bool(metrics.get("S"))
    if trades >= 1000 and sh_def >= 2.0 and long_ok and short_ok:
        return "strict_pass"
    if trades >= 1000 and sh_def >= 1.5 and long_ok:
        return "near_strict"
    if trades >= 10 and long_ok and sh_def > -0.5:
        return "small_sample_watch"
    if long_ok:
        return "watch_only"
    return "blocked_evidence"


def _candidate_order(bot_id: str) -> int:
    if bot_id == PRIMARY_BOT:
        return 0
    try:
        return RUNNER_BOTS.index(bot_id) + 1
    except ValueError:
        return 999


def _candidate_role(bot_id: str) -> str:
    return "primary" if bot_id == PRIMARY_BOT else "runner"


def _candidate_note(bot_id: str, root: str) -> str:
    if bot_id == PRIMARY_BOT:
        return "Primary prop lane: only strict-pass futures edge allowed to approach live routing."
    if bot_id == "volume_profile_nq":
        return "Nasdaq mini scale-up lane; keep behind MNQ until margin/prop buffer supports NQ size."
    if root in {"MYM", "YM"}:
        return "Dow micro/mini runner-up lane; diversification candidate, evidence still maturing."
    if root in {"MES", "ES"}:
        return "S&P micro/mini runner-up lane; research/paper only until strict gate improves."
    return "Runner-up lane; paper/research only until strict promotion gates pass."


def _blockers(
    *,
    row: dict[str, Any],
    role: str,
    prop_summary: str,
    evidence_grade: str,
) -> list[str]:
    blockers: list[str] = []
    launch_lane = str(row.get("launch_lane") or "").lower()
    data_status = str(row.get("data_status") or "").lower()
    promotion_status = str(row.get("promotion_status") or "").lower()
    if row.get("active", True) is False or "deactivated" in {launch_lane, data_status, promotion_status}:
        source = str(row.get("deactivation_source") or "").strip()
        suffix = f" via {source}" if source else ""
        blockers.append(f"bot row is deactivated{suffix}")
    if prop_summary != "READY_FOR_DRY_RUN":
        blockers.append(f"prop readiness is {prop_summary or 'UNKNOWN'}, not READY_FOR_DRY_RUN")
    if not bool(row.get("can_live_trade")):
        blockers.append("bot row is not can_live_trade")
    if evidence_grade not in {"strict_pass", "near_strict"}:
        blockers.append(f"strict-gate evidence is {evidence_grade}")
    if role != "primary":
        blockers.append("runner slot is paper/research only")
    return blockers


def _candidate_from_row(
    row: dict[str, Any],
    *,
    role: str,
    prop_summary: str,
    metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    bot_id = str(row.get("bot_id") or "")
    root = _symbol_root(str(row.get("symbol") or ""))
    evidence_grade = _evidence_grade(metrics)
    blockers = _blockers(
        row=row,
        role=role,
        prop_summary=prop_summary,
        evidence_grade=evidence_grade,
    )
    return {
        "bot_id": bot_id,
        "role": role,
        "symbol": row.get("symbol") or "",
        "root": root,
        "instrument_family": _instrument_family(root),
        "strategy_kind": row.get("strategy_kind") or "",
        "launch_lane": row.get("launch_lane") or "",
        "active": bool(row.get("active", True)),
        "data_status": row.get("data_status") or "",
        "promotion_status": row.get("promotion_status") or "",
        "deactivation_source": row.get("deactivation_source") or "",
        "deactivation_reason": row.get("deactivation_reason") or "",
        "next_action": row.get("next_action") or "",
        "can_paper_trade": bool(row.get("can_paper_trade")),
        "can_live_trade": bool(row.get("can_live_trade")),
        "strict_gate": metrics or {},
        "evidence_grade": evidence_grade,
        "live_routing_allowed": not blockers and role == "primary",
        "blockers": blockers,
        "operator_note": _candidate_note(bot_id, root),
    }


def _automation_mode(prop_summary: str, candidates: list[dict[str, Any]]) -> str:
    primary = candidates[0] if candidates else {}
    if primary.get("live_routing_allowed"):
        return "PRIMARY_READY_FOR_CONTROLLED_PROP_DRY_RUN"
    if prop_summary == "READY_FOR_DRY_RUN":
        return "PROP_DRY_RUN_READY_LIVE_BLOCKED"
    return "FULLY_AUTOMATED_PAPER_PROP_HELD"


def build_ladder_report(
    *,
    readiness_rows: list[dict[str, Any]],
    strict_gate_metrics: dict[str, dict[str, Any]] | None = None,
    prop_readiness: dict[str, Any] | None = None,
    runner_slots: int = RUNNER_SLOTS,
) -> dict[str, Any]:
    strict_gate_metrics = strict_gate_metrics or {}
    prop_readiness = prop_readiness or {}
    prop_summary = str(prop_readiness.get("summary") or "")
    by_bot = {str(row.get("bot_id") or ""): row for row in readiness_rows}
    ordered_bot_ids = [PRIMARY_BOT, *RUNNER_BOTS]

    candidates: list[dict[str, Any]] = []
    for bot_id in ordered_bot_ids:
        row = by_bot.get(bot_id)
        if row is None:
            continue
        root = _symbol_root(str(row.get("symbol") or ""))
        if root not in FOCUS_ROOTS:
            continue
        role = _candidate_role(bot_id)
        if role == "runner" and sum(1 for candidate in candidates if candidate["role"] == "runner") >= runner_slots:
            continue
        candidates.append(
            _candidate_from_row(
                row,
                role=role,
                prop_summary=prop_summary,
                metrics=strict_gate_metrics.get(bot_id),
            ),
        )

    candidates.sort(key=lambda candidate: _candidate_order(str(candidate["bot_id"])))
    automation_mode = _automation_mode(prop_summary, candidates)
    return {
        "kind": "eta_futures_prop_ladder",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "focus": {
            "primary_market": "MNQ/NQ",
            "runner_markets": ["NQ/MNQ", "MYM/YM", "MES/ES"],
            "primary_strategy": PRIMARY_BOT,
            "runner_pool": list(RUNNER_BOTS),
        },
        "summary": {
            "primary_bot": candidates[0]["bot_id"] if candidates else "",
            "runner_slots": runner_slots,
            "candidate_count": len(candidates),
            "automation_mode": automation_mode,
            "prop_readiness": prop_summary or "UNKNOWN",
            "live_routing_allowed_count": sum(1 for candidate in candidates if candidate["live_routing_allowed"]),
        },
        "candidates": candidates,
        "next_actions": _next_actions(automation_mode, prop_summary, candidates),
    }


def _next_actions(automation_mode: str, prop_summary: str, candidates: list[dict[str, Any]]) -> list[str]:
    primary = candidates[0] if candidates else {}
    source = str(primary.get("deactivation_source") or "").strip()
    primary_deactivated = (
        primary.get("active") is False
        or str(primary.get("launch_lane") or "").lower() == "deactivated"
        or str(primary.get("data_status") or "").lower() == "deactivated"
        or str(primary.get("promotion_status") or "").lower() == "deactivated"
    )
    if primary_deactivated and source == "kaizen_sidecar":
        actions = [
            "Keep volume_profile_mnq quarantined: Kaizen retired it from live evidence, so do not force it "
            "back into the prop lane without explicit operator reactivation.",
            "Use the runner-up slots and latest Kaizen ELITE/ROBUST evidence to pick the next "
            "MNQ/NQ/MES/MYM paper-soak candidate.",
        ]
    else:
        actions = [
            "Keep volume_profile_mnq as the only primary prop-lane candidate unless live Kaizen evidence retires it.",
            "Keep runner-ups in paper/research until strict-gate and closed-ledger evidence improve.",
        ]
    if prop_summary != "READY_FOR_DRY_RUN":
        actions.append("Keep Tradovate DORMANT until API/account readiness is explicitly reactivated in code and docs.")
    if automation_mode == "PROP_DRY_RUN_READY_LIVE_BLOCKED":
        actions.append("Clear bot can_live_trade and broker-native bracket/OCO gates before dry-run promotion.")
    if automation_mode == "PRIMARY_READY_FOR_CONTROLLED_PROP_DRY_RUN":
        actions.append("Run any Tradovate DORMANT-lane dry run only after explicit operator reactivation.")
    return actions


def write_report(report: dict[str, Any], path: Path = DEFAULT_OUT) -> Path:
    workspace_roots.ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the futures prop-lane ladder")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if not args.no_write:
        try:
            args.out = workspace_roots.resolve_under_workspace(args.out, label="--out")
        except ValueError as exc:
            parser.error(str(exc))

    report = build_ladder_report(
        readiness_rows=_readiness_rows_from_snapshot(),
        strict_gate_metrics=_latest_strict_gate_metrics(),
        prop_readiness=_prop_readiness_from_snapshot(),
    )
    if not args.no_write:
        write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(f"futures prop ladder: {report['summary']['automation_mode']}")
        print(f"primary: {report['summary']['primary_bot']}")
        print(f"candidates: {report['summary']['candidate_count']}")
        if not args.no_write:
            print(f"wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
