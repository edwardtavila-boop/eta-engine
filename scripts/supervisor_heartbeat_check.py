"""Diagnose the live Jarvis supervisor heartbeat from canonical ETA state.

The VPS supervisor writes its runtime heartbeat under the workspace-level
``var/eta_engine/state`` tree. Older surfaces sometimes looked for a generic
``state/supervisor`` file instead, which can make a healthy supervisor look
stale. This script makes that distinction explicit.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts.workspace_roots import (  # noqa: E402
    ETA_ENGINE_ROOT,
    ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH,
    ETA_JARVIS_SUPERVISOR_KEEPALIVE_PATH,
    ETA_RUNTIME_STATE_DIR,
    ensure_parent,
)

DEFAULT_STALE_THRESHOLD_MINUTES = 10.0
# Keep-alive runs at a faster cadence (15s default) than the main
# heartbeat. We allow it to be modestly stale (3x cadence + jitter)
# before flagging the process as dead. ETA_SUPERVISOR_KEEPALIVE_STALE_S
# overrides this floor for environments where the keepalive cadence
# has been re-tuned.
DEFAULT_KEEPALIVE_STALE_SECONDS = 60.0
CANONICAL_RELATIVE = ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH.relative_to(ETA_RUNTIME_STATE_DIR)
KEEPALIVE_RELATIVE = ETA_JARVIS_SUPERVISOR_KEEPALIVE_PATH.relative_to(ETA_RUNTIME_STATE_DIR)
LEGACY_RELATIVES = (
    ("eta_engine_state_mirror", Path("state") / "jarvis_intel" / "supervisor" / "heartbeat.json"),
    ("legacy_runtime_supervisor", Path("supervisor") / "heartbeat.json"),
    ("legacy_eta_supervisor", Path("state") / "supervisor" / "heartbeat.json"),
)


@dataclass
class HeartbeatCandidate:
    label: str
    path: str
    exists: bool
    readable: bool = False
    fresh: bool = False
    age_seconds: float | None = None
    age_source: str | None = None
    observed_ts: str | None = None
    error: str | None = None
    payload_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": self.path,
            "exists": self.exists,
            "readable": self.readable,
            "fresh": self.fresh,
            "age_seconds": round(self.age_seconds, 3) if self.age_seconds is not None else None,
            "age_source": self.age_source,
            "observed_ts": self.observed_ts,
            "error": self.error,
            "payload_summary": self.payload_summary,
        }


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(observed: datetime, now: datetime) -> float:
    return max(0.0, (now - observed).total_seconds())


def _summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("ts", "tick_count", "mode", "feed", "feed_health"):
        if key in payload:
            summary[key] = payload[key]
    bots = payload.get("bots")
    if isinstance(bots, list):
        summary["bot_count"] = len(bots)
    elif isinstance(payload.get("bot_count"), int):
        summary["bot_count"] = payload["bot_count"]
    return summary


def inspect_heartbeat_candidate(
    label: str,
    path: Path,
    *,
    now: datetime | None = None,
    threshold_minutes: float = DEFAULT_STALE_THRESHOLD_MINUTES,
) -> HeartbeatCandidate:
    now = now or datetime.now(UTC)
    threshold_seconds = threshold_minutes * 60
    if not path.exists():
        return HeartbeatCandidate(label=label, path=str(path), exists=False)

    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return HeartbeatCandidate(
                label=label,
                path=str(path),
                exists=True,
                readable=False,
                error="heartbeat payload is not a JSON object",
            )
    except (OSError, json.JSONDecodeError) as exc:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            age = _age_seconds(mtime, now)
        except OSError:
            age = None
        return HeartbeatCandidate(
            label=label,
            path=str(path),
            exists=True,
            readable=False,
            fresh=age is not None and age <= threshold_seconds,
            age_seconds=age,
            age_source="mtime" if age is not None else None,
            error=str(exc),
        )

    observed = parse_timestamp(payload.get("ts"))
    age_source = "payload.ts"
    if observed is None:
        try:
            observed = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            age_source = "mtime"
        except OSError as exc:
            return HeartbeatCandidate(
                label=label,
                path=str(path),
                exists=True,
                readable=True,
                error=f"unable to stat heartbeat file: {exc}",
                payload_summary=_summarize_payload(payload),
            )

    age = _age_seconds(observed, now)
    return HeartbeatCandidate(
        label=label,
        path=str(path),
        exists=True,
        readable=True,
        fresh=age <= threshold_seconds,
        age_seconds=age,
        age_source=age_source,
        observed_ts=observed.isoformat(),
        payload_summary=_summarize_payload(payload),
    )


def _candidate_paths(*, state_root: Path, eta_engine_root: Path) -> list[tuple[str, Path]]:
    return [
        ("canonical_runtime_jarvis_supervisor", state_root / CANONICAL_RELATIVE),
        ("eta_engine_state_mirror", eta_engine_root / LEGACY_RELATIVES[0][1]),
        ("legacy_runtime_supervisor", state_root / LEGACY_RELATIVES[1][1]),
        ("legacy_eta_supervisor", eta_engine_root / LEGACY_RELATIVES[2][1]),
    ]


def inspect_keepalive(
    path: Path,
    *,
    now: datetime | None = None,
    stale_seconds: float = DEFAULT_KEEPALIVE_STALE_SECONDS,
) -> dict[str, Any]:
    """Inspect the supervisor's independent keep-alive stamp.

    The keepalive file is written by a daemon thread on a short
    interval (default 15s) regardless of main-loop state. A fresh
    keepalive paired with a stale main heartbeat is the operator-
    facing signal of "process alive but main loop stuck" — the
    diagnostic short-circuit below converts that pair into a
    ``main_loop_stuck`` status instead of ``supervisor_dead``.

    Returned dict mirrors the heartbeat-candidate shape but trimmed
    to the fields downstream consumers actually use.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "readable": False,
        "fresh": False,
        "age_seconds": None,
        "age_source": None,
        "observed_ts": None,
        "stale_threshold_seconds": stale_seconds,
        "error": None,
    }
    if not info["exists"]:
        return info

    observed: datetime | None = None
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            info["readable"] = True
            observed = parse_timestamp(payload.get("keepalive_ts"))
            if observed is not None:
                info["age_source"] = "payload.keepalive_ts"
                info["observed_ts"] = observed.isoformat()
        else:
            info["error"] = "keepalive payload is not a JSON object"
    except (OSError, json.JSONDecodeError) as exc:
        info["error"] = str(exc)

    if observed is None:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            observed = mtime
            info["age_source"] = info["age_source"] or "mtime"
        except OSError:
            return info

    age = _age_seconds(observed, now)
    info["age_seconds"] = round(age, 3)
    info["fresh"] = age <= stale_seconds
    return info


def build_supervisor_heartbeat_report(
    *,
    state_root: Path | None = None,
    eta_engine_root: Path | None = None,
    now: datetime | None = None,
    threshold_minutes: float = DEFAULT_STALE_THRESHOLD_MINUTES,
    keepalive_stale_seconds: float = DEFAULT_KEEPALIVE_STALE_SECONDS,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    state_root = state_root or ETA_RUNTIME_STATE_DIR
    eta_engine_root = eta_engine_root or ETA_ENGINE_ROOT

    candidates = [
        inspect_heartbeat_candidate(label, path, now=now, threshold_minutes=threshold_minutes)
        for label, path in _candidate_paths(state_root=state_root, eta_engine_root=eta_engine_root)
    ]
    canonical = candidates[0]
    legacy_candidates = candidates[1:]
    existing = [candidate for candidate in candidates if candidate.age_seconds is not None]
    latest = min(existing, key=lambda candidate: candidate.age_seconds) if existing else None

    keepalive_path = state_root / KEEPALIVE_RELATIVE
    keepalive = inspect_keepalive(
        keepalive_path,
        now=now,
        stale_seconds=keepalive_stale_seconds,
    )

    warnings: list[str] = []
    action_items: list[str] = []
    healthy = canonical.exists and canonical.readable and canonical.fresh

    fresh_legacy = [
        candidate for candidate in legacy_candidates if candidate.exists and candidate.readable and candidate.fresh
    ]

    # Short-circuit: when the canonical main heartbeat is unhealthy (
    # missing/unreadable/stale) but the keep-alive is fresh, the
    # process is alive — it's the main tick loop that's blocked. This
    # distinction matters: a `supervisor_dead` page wakes the on-call
    # to restart, but `main_loop_stuck` guides the operator to
    # diagnose blocking calls (broker reconnect storm, JARVIS layer
    # deadlock) rather than killing/restarting the process.
    main_loop_stuck = (
        keepalive.get("exists")
        and keepalive.get("readable")
        and keepalive.get("fresh")
        and not (canonical.exists and canonical.readable and canonical.fresh)
        and not fresh_legacy
    )

    if not canonical.exists:
        if main_loop_stuck:
            status = "main_loop_stuck"
            diagnosis = "main_heartbeat_missing_keepalive_fresh"
            warnings.append(
                "Supervisor process is alive (keep-alive is fresh) but the main heartbeat "
                "is missing; the tick loop has not produced a heartbeat yet or is blocked.",
            )
            action_items.append(
                "Inspect the supervisor log for blocking calls (broker reconnect, JARVIS layer hang) before restarting."
            )
        elif fresh_legacy:
            status = "wrong_write_path"
            diagnosis = f"canonical_missing_{fresh_legacy[0].label}_fresh"
            warnings.append("Supervisor appears alive, but it is writing to a non-canonical heartbeat path.")
            action_items.append("Restart ETA-Jarvis-Strategy-Supervisor with the canonical var/eta_engine/state path.")
        else:
            status = "missing"
            diagnosis = "canonical_heartbeat_missing"
            action_items.append("Start or repair ETA-Jarvis-Strategy-Supervisor; canonical heartbeat was not created.")
    elif not canonical.readable:
        if main_loop_stuck:
            status = "main_loop_stuck"
            diagnosis = "main_heartbeat_unreadable_keepalive_fresh"
            warnings.append("Supervisor process is alive (keep-alive is fresh) but the main heartbeat is unreadable.")
            action_items.append("Inspect and repair the canonical heartbeat JSON payload; supervisor process is alive.")
        else:
            status = "invalid"
            diagnosis = "canonical_heartbeat_unreadable"
            action_items.append("Inspect and repair the canonical heartbeat JSON payload.")
    elif not canonical.fresh:
        if main_loop_stuck:
            status = "main_loop_stuck"
            diagnosis = "main_heartbeat_stale_keepalive_fresh"
            age = canonical.age_seconds or 0.0
            warnings.append(
                "Supervisor process is alive (keep-alive is fresh) but the main heartbeat "
                "has gone stale; the tick loop is blocked.",
            )
            action_items.append(
                "Inspect the supervisor log for blocking calls (broker reconnect, JARVIS "
                f"layer hang); main heartbeat age is {age:.1f}s.",
            )
        elif fresh_legacy:
            status = "wrong_write_path"
            diagnosis = f"canonical_stale_{fresh_legacy[0].label}_fresh"
            warnings.append(
                "Supervisor appears alive, but the canonical heartbeat is stale while a non-canonical path is fresh."
            )
            action_items.append("Restart ETA-Jarvis-Strategy-Supervisor with the canonical var/eta_engine/state path.")
        else:
            status = "stale"
            diagnosis = "canonical_heartbeat_stale"
            age = canonical.age_seconds or 0.0
            action_items.append(
                f"Restart or inspect ETA-Jarvis-Strategy-Supervisor; canonical heartbeat age is {age:.1f}s."
            )
    else:
        status = "fresh"
        stale_or_missing_legacy = [
            candidate.label for candidate in legacy_candidates if not candidate.exists or not candidate.fresh
        ]
        if stale_or_missing_legacy:
            diagnosis = "canonical_fresh_legacy_path_mismatch"
            warnings.append(
                "Canonical Jarvis supervisor heartbeat is fresh; stale alerts from legacy paths are path mismatches."
            )
        else:
            diagnosis = "canonical_heartbeat_fresh"

    # If the main heartbeat is stale AND the keepalive is also stale
    # AND no legacy mirror is fresh, surface a hint that the
    # boot-refusal latch may be tripped (the supervisor exits before
    # it ever reaches _write_heartbeat OR the keep-alive). The
    # operator runbook documents the latch clearance procedure.
    if (
        not (canonical.exists and canonical.readable and canonical.fresh)
        and not main_loop_stuck
        and not keepalive.get("fresh")
        and not fresh_legacy
    ):
        warnings.append(
            "Both the main heartbeat and keep-alive are stale or missing. "
            "If the supervisor is in the boot-refusal loop, check the "
            "kill_switch_latch.json file — see "
            "docs/SUPERVISOR_HEARTBEAT_RUNBOOK.md 'Boot-refusal pattern'."
        )

    return {
        "ts": now.isoformat(),
        "healthy": healthy,
        "status": status,
        "diagnosis": diagnosis,
        "threshold_minutes": threshold_minutes,
        "keepalive_stale_seconds": keepalive_stale_seconds,
        "canonical_path": canonical.path,
        "canonical_age_seconds": round(canonical.age_seconds, 3) if canonical.age_seconds is not None else None,
        "latest_path": latest.path if latest else None,
        "latest_label": latest.label if latest else None,
        "latest_age_seconds": round(latest.age_seconds, 3) if latest and latest.age_seconds is not None else None,
        "warnings": warnings,
        "action_items": action_items,
        "candidates": [candidate.to_dict() for candidate in candidates],
        "keepalive": keepalive,
    }


def write_supervisor_heartbeat_report(
    report: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> Path:
    state_root = state_root or ETA_RUNTIME_STATE_DIR
    report_path = ensure_parent(state_root / "health" / "supervisor_heartbeat_check_latest.json")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report_path


def _print_human(report: dict[str, Any]) -> None:
    age = report["canonical_age_seconds"]
    age_text = "unknown" if age is None else f"{age:.1f}s"
    print(f"Supervisor heartbeat: {report['status']} ({report['diagnosis']}); canonical age={age_text}")
    keepalive = report.get("keepalive") or {}
    ka_age = keepalive.get("age_seconds")
    ka_age_text = "missing" if ka_age is None else f"{ka_age:.1f}s"
    ka_state = "fresh" if keepalive.get("fresh") else "stale/missing"
    print(f"Keep-alive: {ka_state}; age={ka_age_text}")
    if report["warnings"]:
        for warning in report["warnings"]:
            print(f"WARNING: {warning}")
    if report["action_items"]:
        for item in report["action_items"]:
            print(f"ACTION: {item}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the full JSON diagnostic report.")
    parser.add_argument("--write-report", action="store_true", help="Write latest report under canonical state/health.")
    parser.add_argument("--threshold-min", type=float, default=DEFAULT_STALE_THRESHOLD_MINUTES)
    parser.add_argument(
        "--keepalive-stale-s",
        type=float,
        default=DEFAULT_KEEPALIVE_STALE_SECONDS,
        help="Seconds after which the keep-alive stamp is considered stale (default 60s).",
    )
    parser.add_argument("--state-root", type=Path, default=ETA_RUNTIME_STATE_DIR)
    parser.add_argument("--eta-root", type=Path, default=ETA_ENGINE_ROOT)
    args = parser.parse_args(argv)

    report = build_supervisor_heartbeat_report(
        state_root=args.state_root,
        eta_engine_root=args.eta_root,
        threshold_minutes=args.threshold_min,
        keepalive_stale_seconds=args.keepalive_stale_s,
    )
    if args.write_report:
        report["report_path"] = str(write_supervisor_heartbeat_report(report, state_root=args.state_root))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)

    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
