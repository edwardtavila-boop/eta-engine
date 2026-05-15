"""Diagnose canonical diamond artifacts versus non-authoritative root-var mirrors.

Diamond receipts are written under the canonical workspace state tree:
``C:/EvolutionaryTradingAlgo/var/eta_engine/state``.

Some local watch surfaces historically looked for the same filenames directly
under ``C:/EvolutionaryTradingAlgo/var``. When that compatibility mirror is
missing, a fresh canonical artifact can be misread as stale strategy truth.
This script makes that distinction explicit.
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

from eta_engine.scripts.workspace_roots import ETA_RUNTIME_STATE_DIR, ROOT_VAR_DIR, ensure_parent  # noqa: E402

TIMESTAMP_KEYS = (
    "ts",
    "generated_at_utc",
    "generated_at",
    "as_of",
    "snapshot_ts",
    "timestamp",
    "created_at",
    "updated_at",
)

FRESHNESS_LIMITS_HOURS: dict[str, float] = {
    "diamond_edge_audit_latest.json": 1.5,
    "diamond_leaderboard_latest.json": 1.5,
    "diamond_ops_dashboard_latest.json": 1.5,
    "diamond_promotion_gate_latest.json": 25.0,
    "closed_trade_ledger_latest.json": 0.5,
}


@dataclass(frozen=True)
class ArtifactSpec:
    filename: str
    threshold_hours: float


@dataclass
class ArtifactCandidate:
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


def _extract_payload_timestamp(payload: dict[str, Any]) -> tuple[datetime | None, str | None]:
    for key in TIMESTAMP_KEYS:
        observed = parse_timestamp(payload.get(key))
        if observed is not None:
            return observed, f"payload.{key}"
    return None, None


def _summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("kind", "summary", "ts", "generated_at_utc", "generated_at", "n_diamonds", "n_scored"):
        if key in payload:
            summary[key] = payload[key]
    if isinstance(payload.get("closed_trade_count"), int):
        summary["closed_trade_count"] = payload["closed_trade_count"]
    if isinstance(payload.get("retune_queue"), list):
        summary["retune_queue_count"] = len(payload["retune_queue"])
    if isinstance(payload.get("leaderboard"), list):
        summary["leaderboard_count"] = len(payload["leaderboard"])
    if isinstance(payload.get("candidates"), list):
        summary["candidate_count"] = len(payload["candidates"])
    return summary


def inspect_artifact_candidate(
    label: str,
    path: Path,
    *,
    now: datetime | None = None,
    threshold_hours: float,
) -> ArtifactCandidate:
    now = now or datetime.now(UTC)
    threshold_seconds = threshold_hours * 3600.0
    if not path.exists():
        return ArtifactCandidate(label=label, path=str(path), exists=False)

    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return ArtifactCandidate(
                label=label,
                path=str(path),
                exists=True,
                readable=False,
                error="artifact payload is not a JSON object",
            )
    except (OSError, json.JSONDecodeError) as exc:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            age = _age_seconds(mtime, now)
        except OSError:
            age = None
        return ArtifactCandidate(
            label=label,
            path=str(path),
            exists=True,
            readable=False,
            fresh=age is not None and age <= threshold_seconds,
            age_seconds=age,
            age_source="mtime" if age is not None else None,
            error=str(exc),
        )

    observed, age_source = _extract_payload_timestamp(payload)
    if observed is None:
        try:
            observed = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            age_source = "mtime"
        except OSError as exc:
            return ArtifactCandidate(
                label=label,
                path=str(path),
                exists=True,
                readable=True,
                error=f"unable to stat artifact file: {exc}",
                payload_summary=_summarize_payload(payload),
            )

    age = _age_seconds(observed, now)
    return ArtifactCandidate(
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


def _artifact_specs() -> list[ArtifactSpec]:
    return [
        ArtifactSpec(filename=filename, threshold_hours=threshold)
        for filename, threshold in FRESHNESS_LIMITS_HOURS.items()
    ]


def _assess_artifact(
    spec: ArtifactSpec,
    *,
    state_root: Path,
    root_var_dir: Path,
    now: datetime,
) -> dict[str, Any]:
    canonical = inspect_artifact_candidate(
        "canonical_state",
        state_root / spec.filename,
        now=now,
        threshold_hours=spec.threshold_hours,
    )
    root_var_alias = inspect_artifact_candidate(
        "root_var_alias",
        root_var_dir / spec.filename,
        now=now,
        threshold_hours=spec.threshold_hours,
    )

    warnings: list[str] = []
    action_items: list[str] = []
    healthy = canonical.exists and canonical.readable and canonical.fresh
    surface_status = "ok"

    if healthy:
        if root_var_alias.exists and root_var_alias.readable and root_var_alias.fresh:
            status = "fresh"
            diagnosis = "canonical_fresh_root_var_alias_present"
        elif root_var_alias.exists and not root_var_alias.readable:
            status = "fresh"
            diagnosis = "canonical_fresh_root_var_alias_invalid"
            surface_status = "warning"
            warnings.append(
                "Canonical artifact is fresh, but the root-var compatibility alias is unreadable. "
                "Do not treat this as stale strategy truth."
            )
            action_items.append(
                "Repair or remove the unreadable root-var compatibility alias; local watch surfaces should prefer "
                "var/eta_engine/state."
            )
        else:
            status = "canonical_only"
            diagnosis = "canonical_ready_root_var_missing"
            surface_status = "warning"
            warnings.append(
                "Canonical artifact is fresh, but the root-var compatibility alias is missing. "
                "This is a local watch-surface gap, not stale strategy truth."
            )
            action_items.append(
                "Update local watch surfaces to read var/eta_engine/state first, or intentionally mirror this "
                "artifact into root var/ if a compatibility consumer still depends on it."
            )
    elif not canonical.exists:
        status = "missing"
        if root_var_alias.exists and root_var_alias.readable and root_var_alias.fresh:
            diagnosis = "canonical_missing_root_var_alias_only"
            warnings.append(
                "A fresh root-var alias exists, but the canonical artifact is missing. "
                "The local surface is reading a non-authoritative path."
            )
            action_items.append(
                "Repair the canonical writer or reader path; root-var aliases are not authoritative strategy truth."
            )
        else:
            diagnosis = "canonical_missing"
            action_items.append(
                f"Run or repair the scheduled task that writes {spec.filename} under var/eta_engine/state."
            )
        surface_status = "critical"
    elif not canonical.readable:
        status = "invalid"
        if root_var_alias.exists and root_var_alias.readable and root_var_alias.fresh:
            diagnosis = "canonical_invalid_root_var_alias_only"
            warnings.append(
                "The canonical artifact is unreadable while a root-var alias looks fresh. "
                "Treat the canonical file as the repair target."
            )
            action_items.append(
                f"Repair the canonical JSON payload for {spec.filename}; do not rely on the root-var alias as truth."
            )
        else:
            diagnosis = "canonical_invalid"
            action_items.append(f"Repair the canonical JSON payload for {spec.filename}.")
        surface_status = "critical"
    else:
        status = "stale"
        age = canonical.age_seconds or 0.0
        if root_var_alias.exists and root_var_alias.readable and root_var_alias.fresh:
            diagnosis = "canonical_stale_root_var_alias_only"
            warnings.append(
                "The canonical artifact is stale while the root-var alias looks fresh. "
                "This indicates a local surface/write-path mismatch."
            )
            action_items.append(
                f"Repair {spec.filename} at the canonical var/eta_engine/state path; canonical age is {age:.1f}s."
            )
        else:
            diagnosis = "canonical_stale"
            action_items.append(
                f"Refresh or repair {spec.filename}; canonical age is {age:.1f}s."
            )
        surface_status = "critical"

    return {
        "filename": spec.filename,
        "threshold_hours": spec.threshold_hours,
        "healthy": healthy,
        "status": status,
        "surface_status": surface_status,
        "diagnosis": diagnosis,
        "warnings": warnings,
        "action_items": action_items,
        "candidates": [canonical.to_dict(), root_var_alias.to_dict()],
    }


def build_diamond_artifact_surface_report(
    *,
    state_root: Path | None = None,
    root_var_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    state_root = state_root or ETA_RUNTIME_STATE_DIR
    root_var_dir = root_var_dir or ROOT_VAR_DIR

    artifacts = [
        _assess_artifact(spec, state_root=state_root, root_var_dir=root_var_dir, now=now)
        for spec in _artifact_specs()
    ]

    healthy = all(bool(artifact["healthy"]) for artifact in artifacts)
    warning_artifacts = [artifact for artifact in artifacts if artifact["surface_status"] == "warning"]
    critical_artifacts = [artifact for artifact in artifacts if artifact["surface_status"] == "critical"]
    warnings = [warning for artifact in artifacts for warning in artifact["warnings"]]
    action_items = [item for artifact in artifacts for item in artifact["action_items"]]

    if critical_artifacts:
        if any(artifact["diagnosis"].endswith("root_var_alias_only") for artifact in critical_artifacts):
            status = "critical"
            diagnosis = "canonical_missing_or_stale_root_var_alias_only"
        else:
            status = "critical"
            diagnosis = "canonical_artifacts_unhealthy"
    elif warning_artifacts:
        status = "surface_warning"
        diagnosis = "canonical_ready_root_var_missing"
    else:
        status = "fresh"
        diagnosis = "canonical_artifacts_fresh"

    return {
        "ts": now.isoformat(),
        "healthy": healthy,
        "status": status,
        "diagnosis": diagnosis,
        "canonical_state_root": str(state_root),
        "root_var_dir": str(root_var_dir),
        "warning_count": len(warning_artifacts),
        "critical_count": len(critical_artifacts),
        "warnings": warnings,
        "action_items": action_items,
        "artifacts": artifacts,
    }


def write_diamond_artifact_surface_report(
    report: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> Path:
    state_root = state_root or ETA_RUNTIME_STATE_DIR
    report_path = ensure_parent(state_root / "health" / "diamond_artifact_surface_check_latest.json")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report_path


def _print_human(report: dict[str, Any]) -> None:
    print(f"Diamond artifact surface: {report['status']} ({report['diagnosis']})")
    for artifact in report["artifacts"]:
        canonical = artifact["candidates"][0]
        age = canonical["age_seconds"]
        age_text = "unknown" if age is None else f"{float(age):.1f}s"
        print(
            f"- {artifact['filename']}: {artifact['status']} / {artifact['surface_status']} "
            f"(diagnosis={artifact['diagnosis']}; canonical age={age_text})"
        )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    for item in report["action_items"]:
        print(f"ACTION: {item}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the full JSON diagnostic report.")
    parser.add_argument("--write-report", action="store_true", help="Write latest report under canonical state/health.")
    parser.add_argument("--state-root", type=Path, default=ETA_RUNTIME_STATE_DIR)
    parser.add_argument("--root-var-dir", type=Path, default=ROOT_VAR_DIR)
    args = parser.parse_args(argv)

    report = build_diamond_artifact_surface_report(
        state_root=args.state_root,
        root_var_dir=args.root_var_dir,
    )
    if args.write_report:
        report["report_path"] = str(write_diamond_artifact_surface_report(report, state_root=args.state_root))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)

    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
