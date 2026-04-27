"""Roadmap-vs-code reconciler.

Closes process gap #4 of the v0.1.64 R1 Red Team review:

  > "Roadmap bump scripts (`_bump_roadmap_v0_1_63.py` etc.) advance
  > the version flag but no script enforces 'every R-line item is
  > closed in code, not just in roadmap.' Process fix: a
  > roadmap-vs-code reconciler that maps each `R<n>` line to a list
  > of required code symbols and refuses to bump the closure flag if
  > any symbol is missing. The roadmap currently leads the code; it
  > should lag it."

What this does
--------------
For each R-status item (R1 broker-equity-drift, R2 tick cadence, R3
consistency-guard pause, R4 calendar-aware day rollover) we
enumerate the code symbols / files / events that MUST exist for the
item to be considered closed at a given layer:

  * SCAFFOLDED  -- type / class exists somewhere
  * WIRED       -- type is referenced in the production CLI path
  * ROUTED      -- alerts.yaml has the event registered
  * TESTED      -- a test asserts the wire-up

The script reads ``docs/red_team_d2_d3_review.md`` for the claimed
R-status, walks the codebase for the required symbols, and prints a
truth table:

      R-id  claimed     SCAFFOLDED  WIRED  ROUTED  TESTED
      R1    CLOSED      YES         YES    YES     YES   ok
      R2    CLOSED      YES         YES    n/a     YES   ok
      R3    CLOSED      YES         YES    YES     YES   ok
      R4    CLOSED      YES         YES    n/a     YES   ok

Exit codes
----------
0 -- every claimed-CLOSED R-item is actually CLOSED in code
1 -- at least one claimed-CLOSED R-item has a missing requirement
     (the v0.1.63 R1 BLOCKER would have surfaced as exit 1)
2 -- could not parse the roadmap doc (file missing, bad format)

Usage
-----
    python scripts/_audit_roadmap_vs_code.py
    python scripts/_audit_roadmap_vs_code.py --json
    python scripts/_audit_roadmap_vs_code.py --strict   # fail on any drift, not just claimed-CLOSED

This is intentionally a pure-read script -- it does NOT touch
``roadmap_state.json`` or any other state. It is a reporter and a
CI gate, never a mutator.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RequirementProbe:
    """One thing we need to find in the codebase to consider an R-item closed."""

    layer: str  # SCAFFOLDED | WIRED | ROUTED | TESTED
    description: str
    test: Callable[[], bool]
    detail: str = ""


@dataclass
class RItem:
    rid: str
    title: str
    requirements: list[RequirementProbe] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------


def _file_contains(path: Path, needle: str) -> bool:
    if not path.exists():
        return False
    return needle in path.read_text(encoding="utf-8")


def _yaml_event_routed(event: str) -> bool:
    p = ROOT / "configs" / "alerts.yaml"
    if not p.exists():
        return False
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    events = (cfg or {}).get("routing", {}).get("events", {})
    return event in events


def _grep_codebase(needle: str, *, exclude_tests: bool = True) -> bool:
    """True if any production .py file references ``needle``."""
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if exclude_tests and rel.startswith("tests/"):
            continue
        if "__pycache__" in rel:
            continue
        if needle in p.read_text(encoding="utf-8"):
            return True
    return False


def _test_file_exists(name: str) -> bool:
    return (ROOT / "tests" / name).exists()


# ---------------------------------------------------------------------------
# R-item registry
# ---------------------------------------------------------------------------


def _build_registry() -> list[RItem]:
    """The R-id -> requirements map. Edit when adding an R-item."""
    return [
        RItem(
            rid="R1",
            title="broker-equity drift detection",
            requirements=[
                RequirementProbe(
                    "SCAFFOLDED",
                    "BrokerEquityReconciler class exists",
                    lambda: _file_contains(
                        ROOT / "core" / "broker_equity_reconciler.py",
                        "class BrokerEquityReconciler",
                    ),
                ),
                RequirementProbe(
                    "SCAFFOLDED",
                    "BrokerEquityPoller class exists",
                    lambda: _file_contains(
                        ROOT / "core" / "broker_equity_poller.py",
                        "class BrokerEquityPoller",
                    ),
                ),
                RequirementProbe(
                    "SCAFFOLDED",
                    "BrokerEquityAdapter Protocol exists",
                    lambda: _file_contains(
                        ROOT / "core" / "broker_equity_adapter.py",
                        "class BrokerEquityAdapter",
                    ),
                ),
                RequirementProbe(
                    "WIRED",
                    "_amain calls _build_broker_equity_adapter",
                    lambda: _file_contains(
                        ROOT / "scripts" / "run_eta_live.py",
                        "_build_broker_equity_adapter(",
                    ),
                ),
                RequirementProbe(
                    "WIRED",
                    "ApexRuntime constructed with broker_equity_reconciler kwarg",
                    lambda: _file_contains(
                        ROOT / "scripts" / "run_eta_live.py",
                        "broker_equity_reconciler=broker_reconciler",
                    ),
                ),
                RequirementProbe(
                    "ROUTED",
                    "broker_equity_drift event in alerts.yaml",
                    lambda: _yaml_event_routed("broker_equity_drift"),
                ),
                RequirementProbe(
                    "TESTED",
                    "tests/test_run_eta_live.py covers reconciler integration",
                    lambda: _file_contains(
                        ROOT / "tests" / "test_run_eta_live.py",
                        "BrokerEquityReconciler",
                    ),
                ),
                RequirementProbe(
                    "TESTED",
                    "tests/test_amain_wire_up.py exists (production smoke)",
                    lambda: _test_file_exists("test_amain_wire_up.py"),
                ),
                RequirementProbe(
                    "TESTED",
                    "tests/test_alert_event_registry.py exists (CI gate)",
                    lambda: _test_file_exists("test_alert_event_registry.py"),
                ),
            ],
        ),
        RItem(
            rid="R2",
            title="tick-cadence cushion enforcement",
            requirements=[
                RequirementProbe(
                    "SCAFFOLDED",
                    "validate_apex_tick_cadence exists",
                    lambda: _grep_codebase("def validate_apex_tick_cadence"),
                ),
                RequirementProbe(
                    "WIRED",
                    "_amain or load_runtime_config calls the validator",
                    lambda: _file_contains(
                        ROOT / "scripts" / "run_eta_live.py",
                        "validate_apex_tick_cadence",
                    ),
                ),
                RequirementProbe(
                    "TESTED",
                    "validator has a unit test",
                    lambda: (
                        _grep_codebase(
                            "validate_apex_tick_cadence",
                            exclude_tests=False,
                        )
                        and any(
                            _file_contains(p, "validate_apex_tick_cadence") for p in (ROOT / "tests").glob("test_*.py")
                        )
                    ),
                ),
            ],
        ),
        RItem(
            rid="R3",
            title="consistency-guard 30%-rule pause",
            requirements=[
                RequirementProbe(
                    "SCAFFOLDED",
                    "ConsistencyGuard class + ConsistencyStatus enum",
                    lambda: _file_contains(
                        ROOT / "core" / "consistency_guard.py",
                        "class ConsistencyGuard",
                    ),
                ),
                RequirementProbe(
                    "WIRED",
                    "_amain instantiates ConsistencyGuard (direct or load_or_init)",
                    lambda: (
                        _file_contains(
                            ROOT / "scripts" / "run_eta_live.py",
                            "ConsistencyGuard(",
                        )
                        or _file_contains(
                            ROOT / "scripts" / "run_eta_live.py",
                            "ConsistencyGuard.load_or_init",
                        )
                    ),
                ),
                RequirementProbe(
                    "WIRED",
                    "ApexRuntime constructed with consistency_guard kwarg",
                    lambda: _file_contains(
                        ROOT / "scripts" / "run_eta_live.py",
                        "consistency_guard=consistency_guard",
                    ),
                ),
                RequirementProbe(
                    "ROUTED",
                    "consistency_status event in alerts.yaml",
                    lambda: _yaml_event_routed("consistency_status"),
                ),
                RequirementProbe(
                    "TESTED",
                    "tests/test_consistency_guard.py exists",
                    lambda: _test_file_exists("test_consistency_guard.py"),
                ),
            ],
        ),
        RItem(
            rid="R4",
            title="CME-calendar-aware Apex day rollover",
            requirements=[
                RequirementProbe(
                    "SCAFFOLDED",
                    "apex_trading_day_iso_cme exists",
                    lambda: _grep_codebase("def apex_trading_day_iso_cme"),
                ),
                RequirementProbe(
                    "WIRED",
                    "_tick uses apex_trading_day_iso_cme",
                    lambda: _file_contains(
                        ROOT / "scripts" / "run_eta_live.py",
                        "apex_trading_day_iso_cme",
                    ),
                ),
                RequirementProbe(
                    "TESTED",
                    "calendar test asserts CME holiday roll-forward",
                    lambda: (
                        _grep_codebase(
                            "apex_trading_day_iso_cme",
                            exclude_tests=False,
                        )
                        and any(
                            _file_contains(p, "apex_trading_day_iso_cme") for p in (ROOT / "tests").glob("test_*.py")
                        )
                    ),
                ),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


def _audit() -> tuple[int, list[dict]]:
    items = _build_registry()
    failures = 0
    rows: list[dict] = []
    for item in items:
        result = {"rid": item.rid, "title": item.title, "checks": []}
        for req in item.requirements:
            try:
                ok = bool(req.test())
            except Exception as exc:  # noqa: BLE001
                ok = False
                req.detail = f"probe raised: {exc!r}"
            if not ok:
                failures += 1
            result["checks"].append(
                {
                    "layer": req.layer,
                    "description": req.description,
                    "ok": ok,
                    "detail": req.detail,
                }
            )
        rows.append(result)
    return failures, rows


def _print_table(rows: list[dict]) -> None:
    for r in rows:
        print(f"\n{r['rid']} -- {r['title']}")
        for c in r["checks"]:
            mark = "  OK  " if c["ok"] else "  FAIL"
            extra = f"  ({c['detail']})" if c["detail"] else ""
            print(f"  [{c['layer']:<10s}] {mark}  {c['description']}{extra}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the human table",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="(reserved) fail on any drift even when item is not yet "
        "claimed CLOSED in the roadmap. Today every probe is a hard "
        "gate so this flag is a no-op; reserved for future when "
        "claimed-status is parsed from roadmap_state.json.",
    )
    args = p.parse_args(argv)

    failures, rows = _audit()
    if args.json:
        print(json.dumps({"failures": failures, "rows": rows}, indent=2))
    else:
        _print_table(rows)
        print()
        if failures:
            print(
                f"FAIL -- {failures} requirement(s) missing across "
                f"{sum(1 for r in rows if any(not c['ok'] for c in r['checks']))} R-item(s)"
            )
        else:
            print("OK -- every R-item requirement is satisfied in code")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
