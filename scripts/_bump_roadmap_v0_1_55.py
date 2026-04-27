"""One-shot: bump roadmap_state.json to v0.1.55.

SAFETY / OPS -- order-state reconciler, chaos-drill coverage matrix,
mobile push (Pushover + Telegram), JARVIS cost telemetry, VPS host
runbook. 60 new tests.

Why this bundle exists
----------------------
v0.1.52-v0.1.54 extended alpha + integrity. v0.1.55 closes the ops
surface -- the things that keep an Apex eval alive when the Firm is
not at the keyboard.

Scorecard findings addressed
----------------------------
  1. "On reconnect, local order state is re-derived from venue state
     without a divergence audit -- a double-fill is invisible until
     PnL reconciles the next day." -> ``core.order_state_reconcile``
     pairs local vs venue state, emits a typed action per order
     (NOOP / MARK_FILLED / MARK_PARTIAL / MARK_CANCELLED / ACCEPT_VENUE
     / RESOLVE_MISSING). Conservative mode is the only mode on LIVE.
  2. "Only 4 of 14 safety surfaces have chaos drills." ->
     ``scripts._chaos_drill_matrix`` enumerates all 16 surfaces and
     writes a [PASS]/[GAP] matrix. CI-gated via --fail-under.
  3. "Kill switch fires but the operator's laptop lid is closed." ->
     ``obs.mobile_push`` adds Pushover + Telegram with severity
     gating (CRITICAL + KILL only, by default).
  4. "Model-tier routing is policy-owned but the operator can't see
     which task categories eat the $50/month budget." ->
     ``brain.jarvis_cost_attribution`` accumulates per-category
     CostEvent + emits weekly bucket-sorted Markdown.
  5. "There is a deploy/README.md but no host-picking runbook for a
     cold operator." -> ``deploy/HOST_RUNBOOK.md`` covers provider
     pick, provisioning, SSH lock, UFW, systemd linger, Cloudflare
     tunnel, smoke tests, and LIVE-flip gates.

What ships
----------
  * ``core/order_state_reconcile.py`` + 13 tests
  * ``scripts/_chaos_drill_matrix.py`` + 13 tests
  * ``obs/mobile_push.py`` + 15 tests
  * ``brain/jarvis_cost_attribution.py`` + 19 tests
  * ``deploy/HOST_RUNBOOK.md`` -- operator runbook

Design choices
--------------
  * **Conservative reconciler default.** local-only orders with venue
    missing are MARK_CANCELLED by default, never inferred alive.
    Prevents double-fill on reconnect.
  * **Chaos-drill matrix is a single source of truth.** Adding a new
    safety module without adding its row trips CI via --fail-under.
  * **Mobile-push severity threshold = CRITICAL.** Operator is not
    woken up for INFO / WARN chatter. Override per-bus.
  * **Cost ledger uses SONNET-equivalent units.** $ price per token
    can change without invalidating historical events.
  * **HOST_RUNBOOK is stepwise + idempotent.** Every provisioning
    step is re-runnable. Gates (4.1-4.5) must pass before LIVE flip.

Delta
-----
  * tests_passing: 2658 -> 2718 (+60)
  * Four new modules + operator runbook
  * Ruff-clean on every new file
  * No phase-level status change (overall_progress_pct stays at 99)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.55"
NEW_TESTS_ABS = 2718


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_55_safety_ops"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "SAFETY / OPS -- order-state reconciler, chaos-drill "
            "coverage matrix, mobile push (Pushover + Telegram), "
            "JARVIS cost telemetry, VPS host runbook. 60 new tests."
        ),
        "theme": (
            "Closes the ops surface. Double-fills on reconnect, "
            "undrilled safety modules, sleeping operator, "
            "invisible model-tier burn, cold-operator VPS bring-up "
            "-- all addressed."
        ),
        "operator_directive_quote": (
            "the bot must survive an Apex eval even when I'm "
            "asleep. No silent double-fills. No undrilled safety "
            "surface. No mobile-push blind spot. No mystery "
            "budget burn."
        ),
        "artifacts_added": {
            "core": ["core/order_state_reconcile.py"],
            "obs": ["obs/mobile_push.py"],
            "brain": ["brain/jarvis_cost_attribution.py"],
            "scripts": [
                "scripts/_chaos_drill_matrix.py",
                "scripts/_bump_roadmap_v0_1_55.py",
            ],
            "deploy": ["deploy/HOST_RUNBOOK.md"],
            "tests": [
                "tests/test_core_order_state_reconcile.py",
                "tests/test_scripts_chaos_drill_matrix.py",
                "tests/test_obs_mobile_push.py",
                "tests/test_brain_jarvis_cost_attribution.py",
            ],
        },
        "design_notes": {
            "conservative_reconciler": (
                "Local-only orders with venue missing are "
                "MARK_CANCELLED by default, never inferred alive. "
                "Conservative mode is the only mode on LIVE."
            ),
            "chaos_matrix_single_source": (
                "All 16 safety surfaces enumerated. Adding a new "
                "safety module without its matrix row trips CI "
                "via --fail-under."
            ),
            "mobile_push_severity_gated": (
                "Default min_severity=CRITICAL. Operator not woken for INFO / WARN chatter. Per-bus override."
            ),
            "cost_ledger_sonnet_equiv_units": (
                "$ price per token can change without "
                "invalidating historical events. We store tokens "
                "+ tier; unit conversion is a pure function of "
                "the current COST_RATIO table."
            ),
            "host_runbook_stepwise": (
                "Every provisioning step is idempotent and "
                "re-runnable. Gates 4.1-4.5 must pass before "
                "LIVE flip. No surprise operator burn."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Safety / Ops ships: order-state reconciler "
                    "(conservative mode) + chaos-drill coverage "
                    "matrix + mobile push (Pushover + Telegram) + "
                    "JARVIS cost telemetry + VPS host runbook. "
                    "Four new modules, one runbook, 60 new tests, "
                    "ruff-clean."
                ),
                "tests_delta": NEW_TESTS_ABS - prev_tests,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})",
    )


if __name__ == "__main__":
    main()
