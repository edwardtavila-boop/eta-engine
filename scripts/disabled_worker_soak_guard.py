"""Fail-closed readiness guard for obsolete-worker unregister tasks.

The coordination queue includes a future task to unregister disabled workers,
but that task is intentionally gated by a 14-day soak and operator approval.
This script gives agents and automation a machine-readable check so they can
advance verification without removing workers early.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK_ID = "T-2026-05-05-007"
DEFAULT_REQUIRED_SOAK_DAYS = 14
DEFAULT_REGISTRY_REL_PATH = Path("bots") / "registry.py"


@dataclass(frozen=True)
class SoakStatus:
    created_at: datetime
    now: datetime
    ready_at: datetime
    required_days: int
    remaining_seconds: float
    ready: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "created_at": _format_utc(self.created_at),
            "now": _format_utc(self.now),
            "ready_at": _format_utc(self.ready_at),
            "required_days": self.required_days,
            "remaining_seconds": self.remaining_seconds,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class UnregistrationGateReport:
    task_id: str
    root: Path
    soak: SoakStatus
    registry_rel_path: Path
    registry_exists: bool
    operator_approved: bool
    blockers: list[str]

    @property
    def preconditions_ready(self) -> bool:
        return self.soak.ready and self.registry_exists

    @property
    def ready(self) -> bool:
        return self.preconditions_ready and self.operator_approved

    @property
    def action(self) -> str:
        if self.ready:
            return "ready_to_unregister"
        if self.preconditions_ready:
            return "request_operator_approval"
        return "do_not_unregister"

    def as_payload(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "ready": self.ready,
            "preconditions_ready": self.preconditions_ready,
            "action": self.action,
            "operator_approved": self.operator_approved,
            "root": str(self.root),
            "soak": self.soak.as_payload(),
            "registry": {
                "path": self.registry_rel_path.as_posix(),
                "exists": self.registry_exists,
            },
            "blockers": self.blockers,
        }


def parse_utc_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def evaluate_soak_window(
    *,
    created_at: datetime,
    now: datetime | None = None,
    required_soak_days: int = DEFAULT_REQUIRED_SOAK_DAYS,
) -> SoakStatus:
    if required_soak_days < 1:
        raise ValueError("required_soak_days must be positive")
    created_utc = created_at.astimezone(UTC)
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    ready_at = created_utc + timedelta(days=required_soak_days)
    remaining = max(0.0, (ready_at - now_utc).total_seconds())
    return SoakStatus(
        created_at=created_utc,
        now=now_utc,
        ready_at=ready_at,
        required_days=required_soak_days,
        remaining_seconds=remaining,
        ready=remaining == 0.0,
    )


def evaluate_unregistration_gate(
    *,
    root: Path = ROOT,
    created_at: datetime,
    now: datetime | None = None,
    required_soak_days: int = DEFAULT_REQUIRED_SOAK_DAYS,
    task_id: str = DEFAULT_TASK_ID,
    registry_rel_path: Path = DEFAULT_REGISTRY_REL_PATH,
    operator_approved: bool = False,
) -> UnregistrationGateReport:
    root = root.resolve()
    soak = evaluate_soak_window(
        created_at=created_at,
        now=now,
        required_soak_days=required_soak_days,
    )
    registry_exists = (root / registry_rel_path).is_file()
    blockers: list[str] = []

    if not soak.ready:
        blockers.append(
            f"{required_soak_days}-day soak incomplete; earliest unregister is {_format_utc(soak.ready_at)}."
        )
    if not registry_exists:
        blockers.append(
            f"Expected registry surface {registry_rel_path.as_posix()} is missing; do not unregister configs."
        )
    if soak.ready and registry_exists and not operator_approved:
        blockers.append("Explicit operator approval is required before unregistering disabled workers.")

    return UnregistrationGateReport(
        task_id=task_id,
        root=root,
        soak=soak,
        registry_rel_path=registry_rel_path,
        registry_exists=registry_exists,
        operator_approved=operator_approved,
        blockers=blockers,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID)
    parser.add_argument("--created-at", required=True, help="Task creation timestamp, e.g. 2026-05-05T00:00:00Z")
    parser.add_argument("--now", help="Override current UTC timestamp for deterministic checks")
    parser.add_argument("--required-soak-days", type=int, default=DEFAULT_REQUIRED_SOAK_DAYS)
    parser.add_argument("--registry-rel-path", type=Path, default=DEFAULT_REGISTRY_REL_PATH)
    parser.add_argument("--operator-approved", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = evaluate_unregistration_gate(
        root=args.root,
        created_at=parse_utc_timestamp(args.created_at),
        now=parse_utc_timestamp(args.now) if args.now else None,
        required_soak_days=args.required_soak_days,
        task_id=args.task_id,
        registry_rel_path=args.registry_rel_path,
        operator_approved=args.operator_approved,
    )
    print(json.dumps(report.as_payload(), indent=2, sort_keys=True))
    return 0 if report.ready else 1


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
