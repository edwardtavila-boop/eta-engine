"""Emit a changed-only flaw-hardening heartbeat for ETA automation.

This watchdog is deliberately read-only. It summarizes the highest-value
remaining flaw surfaces from canonical runtime artifacts plus a few static
architecture hotspots so the VPS can keep an always-on operator receipt
without pretending Codex thread heartbeats are a daemon.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.obs import firm_scorecard  # noqa: E402
from eta_engine.scripts import workspace_roots  # noqa: E402

DEFAULT_OUT = workspace_roots.ETA_FLAW_HARDENING_SNAPSHOT_PATH

_HOTSPOT_PATHS = {
    "jarvis_strategy_supervisor": workspace_roots.ETA_ENGINE_ROOT / "scripts" / "jarvis_strategy_supervisor.py",
    "broker_router": workspace_roots.ETA_ENGINE_ROOT / "scripts" / "broker_router.py",
    "dashboard_api": workspace_roots.ETA_ENGINE_ROOT / "deploy" / "scripts" / "dashboard_api.py",
}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _line_count(path: Path) -> int | None:
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8"))
    except OSError:
        return None


def _first_blocked_prop_check(report: dict[str, Any]) -> dict[str, Any]:
    for check in report.get("checks", []):
        if isinstance(check, dict) and str(check.get("status") or "").upper() == "BLOCKED":
            return check
    return {}


def build_snapshot(
    *,
    scorecard: dict[str, Any] | None = None,
    prop_live_readiness: dict[str, Any] | None = None,
    launch_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_scorecard = scorecard if isinstance(scorecard, dict) else firm_scorecard.build_scorecard()
    current_prop = (
        prop_live_readiness
        if isinstance(prop_live_readiness, dict)
        else _load_json(workspace_roots.ETA_PROP_LIVE_READINESS_PATH)
    )
    current_launch = (
        launch_readiness
        if isinstance(launch_readiness, dict)
        else _load_json(workspace_roots.ETA_DIAMOND_PROP_LAUNCH_READINESS_PATH)
    )

    summary = current_scorecard.get("summary") if isinstance(current_scorecard.get("summary"), dict) else {}
    prop_blocker = _first_blocked_prop_check(current_prop)
    launch_gates = current_launch.get("gates") if isinstance(current_launch.get("gates"), list) else []
    launch_primary_blocker = None
    for gate in launch_gates:
        if isinstance(gate, dict) and str(gate.get("status") or "").upper() in {"NO_GO", "HOLD"}:
            launch_primary_blocker = str(gate.get("name") or "")
            break

    hotspot_lines = {name: _line_count(path) for name, path in _HOTSPOT_PATHS.items()}
    hotspot_max_name = max(
        hotspot_lines,
        key=lambda name: hotspot_lines[name] if isinstance(hotspot_lines[name], int) else -1,
    )
    hotspot_max_lines = hotspot_lines.get(hotspot_max_name)

    top_statuses = [
        str(current_scorecard.get("status") or ""),
        str(current_prop.get("summary") or ""),
        str(current_launch.get("overall_verdict") or ""),
    ]
    if any(status.upper() in {"BLOCKED", "NO_GO"} for status in top_statuses):
        status = "blocked"
    elif any(status.upper() in {"LIMITED", "HOLD", "DEGRADED"} for status in top_statuses):
        status = "limited"
    else:
        status = "ready"

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "eta_flaw_hardening_heartbeat",
        "status": status,
        "scorecard_status": current_scorecard.get("status"),
        "scorecard_composite_score": current_scorecard.get("composite_score"),
        "scorecard_grade": current_scorecard.get("grade"),
        "scorecard_primary_blocker": summary.get("launch_readiness_primary_blocker"),
        "scorecard_primary_blocker_detail": summary.get("launch_readiness_primary_blocker_detail"),
        "scorecard_cap_reason": summary.get("composite_score_cap_reason"),
        "prop_live_readiness_status": current_prop.get("summary") or "missing",
        "prop_live_readiness_primary_blocker": prop_blocker.get("name"),
        "prop_live_readiness_primary_blocker_detail": prop_blocker.get("detail"),
        "prop_live_readiness_next_actions": (
            current_prop.get("next_actions")
            if isinstance(current_prop.get("next_actions"), list)
            else []
        ),
        "launch_readiness_verdict": current_launch.get("overall_verdict") or "missing",
        "launch_readiness_primary_blocker": launch_primary_blocker,
        "architecture_hotspot_lines": hotspot_lines,
        "architecture_hotspot_max_name": hotspot_max_name,
        "architecture_hotspot_max_lines": hotspot_max_lines,
    }


def compare_snapshots(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous = previous if isinstance(previous, dict) else {}
    changed_fields: list[str] = []
    for field in (
        "status",
        "scorecard_status",
        "scorecard_composite_score",
        "scorecard_primary_blocker",
        "prop_live_readiness_status",
        "prop_live_readiness_primary_blocker",
        "launch_readiness_verdict",
        "launch_readiness_primary_blocker",
        "architecture_hotspot_max_name",
        "architecture_hotspot_max_lines",
    ):
        if previous.get(field) != current.get(field):
            changed_fields.append(field)
    previous_hotspots = (
        previous.get("architecture_hotspot_lines")
        if isinstance(previous.get("architecture_hotspot_lines"), dict)
        else {}
    )
    current_hotspots = (
        current.get("architecture_hotspot_lines")
        if isinstance(current.get("architecture_hotspot_lines"), dict)
        else {}
    )
    for name, current_value in current_hotspots.items():
        if previous_hotspots.get(name) != current_value:
            changed_fields.append(f"architecture_hotspot_lines.{name}")
    changed = bool(changed_fields)
    summary = "flaw hardening snapshot unchanged"
    if changed:
        summary = "flaw hardening drift detected: " + ", ".join(changed_fields)
    return {
        "changed": changed,
        "summary": summary,
        "changed_fields": changed_fields,
    }


def write_snapshot(snapshot: dict[str, Any], path: Path, *, previous_path: Path | None = None) -> Path:
    workspace_roots.ensure_parent(path)
    resolved_previous_path = previous_path or default_previous_path_for(path)
    if path.exists():
        workspace_roots.ensure_parent(resolved_previous_path)
        resolved_previous_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_snapshot(path: Path) -> dict[str, Any]:
    return _load_json(path)


def default_previous_path_for(path: Path) -> Path:
    if path == workspace_roots.ETA_FLAW_HARDENING_SNAPSHOT_PATH:
        return workspace_roots.ETA_FLAW_HARDENING_PREVIOUS_SNAPSHOT_PATH
    return path.with_suffix(path.suffix + ".previous")


def build_snapshot_with_drift(*, out_path: Path, previous_path: Path | None) -> dict[str, Any]:
    resolved_previous_path = previous_path or default_previous_path_for(out_path)
    snapshot = build_snapshot()
    previous = load_snapshot(out_path) or load_snapshot(resolved_previous_path)
    snapshot["drift"] = compare_snapshots(previous, snapshot)
    return snapshot


def build_heartbeat(snapshot: dict[str, Any], snapshot_path: Path | None) -> dict[str, Any]:
    drift = snapshot.get("drift") if isinstance(snapshot.get("drift"), dict) else {}
    changed = bool(drift.get("changed"))
    return {
        "schema_version": 1,
        "generated_at": snapshot.get("generated_at"),
        "source": "eta_flaw_hardening_heartbeat.drift",
        "notify": changed,
        "status": snapshot.get("status"),
        "scorecard_status": snapshot.get("scorecard_status"),
        "scorecard_composite_score": snapshot.get("scorecard_composite_score"),
        "scorecard_primary_blocker": snapshot.get("scorecard_primary_blocker"),
        "prop_live_readiness_status": snapshot.get("prop_live_readiness_status"),
        "prop_live_readiness_primary_blocker": snapshot.get("prop_live_readiness_primary_blocker"),
        "launch_readiness_verdict": snapshot.get("launch_readiness_verdict"),
        "launch_readiness_primary_blocker": snapshot.get("launch_readiness_primary_blocker"),
        "architecture_hotspot_max_name": snapshot.get("architecture_hotspot_max_name"),
        "architecture_hotspot_max_lines": snapshot.get("architecture_hotspot_max_lines"),
        "drift_changed": changed,
        "drift_summary": drift.get("summary"),
        "changed_fields": drift.get("changed_fields") or [],
        "snapshot_path": str(snapshot_path) if snapshot_path is not None else None,
    }


def render_text(heartbeat: dict[str, Any]) -> str:
    return (
        "flaw_hardening_heartbeat "
        f"notify={'yes' if heartbeat.get('notify') else 'no'} "
        f"status={heartbeat.get('status')} "
        f"scorecard={heartbeat.get('scorecard_status')} "
        f"score={heartbeat.get('scorecard_composite_score')} "
        f"scorecard_blocker={heartbeat.get('scorecard_primary_blocker') or 'none'} "
        f"prop_gate={heartbeat.get('prop_live_readiness_status')} "
        f"prop_blocker={heartbeat.get('prop_live_readiness_primary_blocker') or 'none'} "
        f"launch={heartbeat.get('launch_readiness_verdict')} "
        f"launch_blocker={heartbeat.get('launch_readiness_primary_blocker') or 'none'} "
        f"hotspot={heartbeat.get('architecture_hotspot_max_name')}:{heartbeat.get('architecture_hotspot_max_lines')} "
        f"changed_fields={','.join(heartbeat.get('changed_fields') or []) or 'none'} "
        f"drift={heartbeat.get('drift_summary') or 'none'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flaw_hardening_heartbeat")
    parser.add_argument("--out", type=Path, default=workspace_roots.ETA_FLAW_HARDENING_SNAPSHOT_PATH)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print JSON heartbeat payload")
    parser.add_argument("--changed-only", action="store_true", help="suppress output when the snapshot did not drift")
    parser.add_argument("--no-write", action="store_true", help="build and print without writing the snapshot")
    parser.add_argument("--strict-blockers", action="store_true", help="exit 2 when the top-level status is blocked")
    parser.add_argument("--strict-drift", action="store_true", help="exit 3 when snapshot drift is detected")
    args = parser.parse_args(argv)
    try:
        args.out = workspace_roots.resolve_under_workspace(args.out, label="--out")
        if args.previous is not None:
            args.previous = workspace_roots.resolve_under_workspace(args.previous, label="--previous")
    except ValueError as exc:
        parser.error(str(exc))

    previous_path = args.previous or default_previous_path_for(args.out)
    snapshot = build_snapshot_with_drift(out_path=args.out, previous_path=previous_path)
    written_path = None if args.no_write else write_snapshot(snapshot, args.out, previous_path=previous_path)
    heartbeat = build_heartbeat(snapshot, written_path)

    if not args.changed_only or heartbeat["notify"]:
        if args.json:
            print(json.dumps(heartbeat, indent=2, sort_keys=True, default=str))
        else:
            print(render_text(heartbeat))

    if args.strict_drift and heartbeat["notify"]:
        return 3
    if args.strict_blockers and str(heartbeat.get("status") or "").lower() == "blocked":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
