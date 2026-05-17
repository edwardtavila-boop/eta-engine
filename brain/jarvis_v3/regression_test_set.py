"""Regression test set (Wave-16, 2026-04-27).

A curated suite of historical decisions JARVIS got RIGHT or WRONG.
Any new candidate policy must continue to:
  * Approve every PASS case (decisions JARVIS got right that should
    keep being approved)
  * Deny every FAIL case (decisions JARVIS originally took that
    blew up -- the new policy must NOT re-approve them)

This is a regression-test-set in the software-testing sense: the
floor of behavior new policies must meet.

Cases are added two ways:
  * Manually by the operator after a notable trade
  * Automatically when postmortem.generate_postmortem flags a
    catastrophic loss (severity == "catastrophic") -- those cases
    are auto-added as FAIL cases
  * Manually when a clean +2R or better trade happens in clear
    conditions -- those are PASS cases

Pure stdlib + persistent JSON.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import workspace_roots

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_CASES_PATH = workspace_roots.ETA_JARVIS_INTEL_STATE_DIR / "regression_cases.json"


class CaseKind(StrEnum):
    PASS_CASE = "PASS_CASE"  # must stay APPROVED
    FAIL_CASE = "FAIL_CASE"  # must stay DENIED/DEFERRED


@dataclass
class RegressionCase:
    """One curated case."""

    case_id: str
    kind: CaseKind
    signal_id: str
    proposal_payload: dict
    realized_r: float
    rationale: str = ""
    added_at: str = ""
    added_by: str = "operator"


@dataclass
class CaseResult:
    """One case's evaluation under a candidate policy."""

    case_id: str
    kind: str
    expected_to_approve: bool  # True for PASS_CASE
    actual_verdict: str
    passed: bool
    note: str = ""


@dataclass
class RegressionReport:
    n_cases: int
    n_passed: int
    n_failed: int
    pass_rate: float
    failed_cases: list[CaseResult] = field(default_factory=list)
    all_results: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "n_passed": self.n_passed,
            "n_failed": self.n_failed,
            "pass_rate": self.pass_rate,
            "failed_cases": [asdict(c) for c in self.failed_cases],
            "all_results": [asdict(c) for c in self.all_results],
        }


# ─── Suite ────────────────────────────────────────────────────────


class RegressionSuite:
    """Persistent curated set of regression cases."""

    def __init__(self, *, cases_path: Path = DEFAULT_CASES_PATH) -> None:
        self.cases_path = cases_path
        self._cases: dict[str, RegressionCase] = {}
        self._load()

    @classmethod
    def default(cls) -> RegressionSuite:
        return cls()

    def add_case(
        self,
        *,
        case_id: str,
        kind: CaseKind,
        signal_id: str,
        proposal_payload: dict,
        realized_r: float,
        rationale: str = "",
        added_by: str = "operator",
    ) -> RegressionCase:
        case = RegressionCase(
            case_id=case_id,
            kind=kind,
            signal_id=signal_id,
            proposal_payload=dict(proposal_payload),
            realized_r=float(realized_r),
            rationale=rationale,
            added_at=datetime.now(UTC).isoformat(),
            added_by=added_by,
        )
        self._cases[case_id] = case
        self._save()
        return case

    def remove_case(self, case_id: str) -> RegressionCase | None:
        case = self._cases.pop(case_id, None)
        if case is not None:
            self._save()
        return case

    def list_cases(self) -> list[RegressionCase]:
        return list(self._cases.values())

    def evaluate(
        self,
        policy_fn: Callable[[dict], str],
    ) -> RegressionReport:
        """Run ``policy_fn`` on every case, classify pass/fail.

        ``policy_fn(proposal_payload) -> str`` returns the verdict
        (APPROVED / CONDITIONAL / DEFERRED / DENIED).
        """
        results: list[CaseResult] = []
        for case in self._cases.values():
            try:
                verdict = str(policy_fn(case.proposal_payload))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "regression: policy raised on %s (%s)",
                    case.case_id,
                    exc,
                )
                verdict = "ERROR"

            expected_approve = case.kind == CaseKind.PASS_CASE
            actual_approve = verdict.upper() in {"APPROVED", "CONDITIONAL"}
            passed = expected_approve == actual_approve
            note = ""
            if not passed:
                if expected_approve:
                    note = f"PASS_CASE regressed: expected APPROVED, got {verdict}"
                else:
                    note = (
                        f"FAIL_CASE regressed: expected DENIED/DEFERRED, "
                        f"got {verdict} (this case lost {case.realized_r:+.2f}R)"
                    )
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    kind=case.kind.value,
                    expected_to_approve=expected_approve,
                    actual_verdict=verdict,
                    passed=passed,
                    note=note,
                )
            )

        n = len(results)
        n_passed = sum(1 for r in results if r.passed)
        return RegressionReport(
            n_cases=n,
            n_passed=n_passed,
            n_failed=n - n_passed,
            pass_rate=round(n_passed / max(n, 1), 4),
            failed_cases=[r for r in results if not r.passed],
            all_results=results,
        )

    # ── Persistence ──────────────────────────────────────────

    def _load(self) -> None:
        if not self.cases_path.exists():
            return
        try:
            data = json.loads(self.cases_path.read_text(encoding="utf-8"))
            for cid, raw in data.items():
                self._cases[cid] = RegressionCase(
                    case_id=raw["case_id"],
                    kind=CaseKind(raw["kind"]),
                    signal_id=raw.get("signal_id", ""),
                    proposal_payload=raw.get("proposal_payload", {}),
                    realized_r=float(raw.get("realized_r", 0.0)),
                    rationale=raw.get("rationale", ""),
                    added_at=raw.get("added_at", ""),
                    added_by=raw.get("added_by", "operator"),
                )
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("regression: load failed (%s)", exc)

    def _save(self) -> None:
        try:
            self.cases_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                cid: {
                    "case_id": c.case_id,
                    "kind": c.kind.value,
                    "signal_id": c.signal_id,
                    "proposal_payload": c.proposal_payload,
                    "realized_r": c.realized_r,
                    "rationale": c.rationale,
                    "added_at": c.added_at,
                    "added_by": c.added_by,
                }
                for cid, c in self._cases.items()
            }
            self.cases_path.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("regression: save failed (%s)", exc)
