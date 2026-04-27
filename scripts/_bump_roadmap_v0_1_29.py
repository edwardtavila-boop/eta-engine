"""One-shot: bump roadmap_state.json to v0.1.29.

JARVIS ADMIN COMMAND CENTER INTEGRATION -- surface the chain-of-command.

Context
-------
v0.1.25 shipped the JARVIS bundle (context + checklist + autopilot watchdog + ...).
v0.1.27 shipped the FINAL REVISION optimization pipeline and, in passing,
introduced ``brain.jarvis_admin`` (the central approval authority) and
wired the autopilot watchdog through it as the POC subsystem.
v0.1.28 shipped JARVIS LIVE SUPERVISION (supervisor + daemon) so the
admin cannot silently go stale.

v0.1.29 makes the admin *visible*. The Command Center now renders the
full org-chart -- every autonomous subsystem across eta_engine,
mnq_bot v3 framework, the_firm 6-agent council, and the
guards/operator tier -- plus a rolling tail of recent approval
decisions. Operators can see at a glance which subsystems report to
Jarvis and what he has been approving.

What v0.1.29 adds
-----------------
  * ``.claude/skills/firm-tracker/references/artifact_template.jsx``
    (the single-source-of-truth for the Command Center JSX used by the
    ``firm-tracker`` skill in the mnq_bot repo). Two new panel
    components + one new data constant mounted into the Jarvis tab:

      - ``JARVIS_ADMIN`` constant: ``{ version, commandTree, recentAudit }``.
        * ``commandTree`` = 4 groups x 19 subsystems total:
            Bot Fleet (eta_engine): crypto_seed, eth_perp, mnq, nq.
            Framework (mnq_bot v3):    autopilot, firm_engine,
                                       court_of_appeals, confluence,
                                       webhook, meta_orch.
            The Firm (6 agents):       quant, red_team, risk, macro,
                                       micro, pm.
            Guards & Operator:         gate_chain, autopilot_watchdog,
                                       operator.
        * ``recentAudit`` seeded with the watchdog POC entry so the
          panel is never empty.
      - ``ChainOfCommandPanel({ admin })`` -- authority banner ("J" avatar
        + "JARVIS -- sole authority" caption + "every autonomous subsystem
        requests approval before acting" subtitle) followed by the 4-group
        subsystem grid with {label, mode, tier pill}.
      - ``AdminAuditTailPanel({ audit })`` -- verdict-pill + subsystem-arrow-
        action + reason_code/reason + size_cap_mult + HH:MM:SS timestamp
        per record. Empty-state copy wired in.

    Mounted as a new row inside ``JarvisTab``: ``3fr 2fr`` grid between
    the v2 second row (Alerts / Trajectory / Playbook) and the
    2-column (10 Principles / Bundle Status) section. The Bundle Status
    panel title and shippedVersion bumped to v0.1.29.

  * Bundle metadata reconciliation in the JSX:
      - ``bundle.moduleCount`` 10 -> 11 (jarvis_admin added to the list)
      - ``bundle.testCount``   180 -> 1385 (now the full eta_engine
        suite since the admin + watchdog-wiring tests live inside it)
      - ``bundle.shippedVersion`` "v0.1.26" -> "v0.1.29"
      - ``bundle.moduleNames`` list updated with the jarvis_admin entry
        and an (admin-wired, 21 tests) annotation on autopilot_watchdog.

  * JSX parse-validated via ``@babel/parser`` with
    ``plugins: ['jsx'], errorRecovery: false`` (1567 lines, 79,474 bytes).

Reconciliation
--------------
  * tests_passing: 1385 -> 1385 (no change). The JarvisAdmin module
    itself (46 tests) and the autopilot_watchdog admin wiring tests (3)
    were already in the tree and already counted under v0.1.28's
    1385-passing baseline. v0.1.29 is pure surfacing -- Command Center
    JSX only, no Python touched.
  * No phase-level status changes. P9_ROLLOUT remains at 85% pending
    the $1000 Tradovate funding gate.
  * overall_progress_pct: 99 (unchanged).

Dashboard regeneration
----------------------
The ``firm-tracker`` skill reads the artifact template, substitutes
live data from reports, and writes
``Base/firm_command_center.jsx``. After this bump, running the skill
will render the Chain of Command panel automatically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    new_tests = 1385  # unchanged -- JSX-only bundle
    sa["eta_engine_tests_passing"] = new_tests

    sa["eta_engine_v0_1_29_jarvis_admin_cc"] = {
        "timestamp_utc": now,
        "version": "v0.1.29",
        "bundle_name": ("JARVIS ADMIN COMMAND CENTER INTEGRATION -- surface the chain-of-command"),
        "directive": (
            "make jarvis the admin of all my projects -- make everyone report to him throughout my framework"
        ),
        "theme": (
            "v0.1.27 wired JarvisAdmin as the central approval authority; "
            "v0.1.28 kept it live under supervision; v0.1.29 makes it "
            "visible. The Command Center now renders the org-chart and a "
            "rolling tail of approval decisions so operators can see the "
            "authority relationships at a glance."
        ),
        "artifacts_touched": [
            ("mnq_bot/.claude/skills/firm-tracker/references/artifact_template.jsx"),
        ],
        "jsx_components_added": {
            "data_constant": {
                "name": "JARVIS_ADMIN",
                "shape": "{ version, commandTree, recentAudit }",
                "commandTree_groups": 4,
                "commandTree_subsystem_count": 19,
                "recentAudit_seed_count": 1,
            },
            "panels": [
                {
                    "name": "ChainOfCommandPanel",
                    "props": "{ admin }",
                    "renders": (
                        "authority banner ('J' avatar + JARVIS caption) + "
                        "4-group subsystem grid with {label, mode, tier pill}"
                    ),
                },
                {
                    "name": "AdminAuditTailPanel",
                    "props": "{ audit }",
                    "renders": ("rolling audit tail: verdict pill + subsystem->action + reason + size cap + timestamp"),
                },
            ],
            "mount_site": (
                "JarvisTab() -- new row between v2 second row "
                "(Alerts/Trajectory/Playbook) and the 2-column "
                "(10 Principles / Bundle Status) section, grid 3fr:2fr"
            ),
            "helpers_added": ["tierColor()", "verdictColor()"],
        },
        "commandTree": {
            "Bot Fleet (eta_engine)": [
                "crypto_seed",
                "eth_perp",
                "mnq",
                "nq",
            ],
            "Framework (mnq_bot v3)": [
                "autopilot",
                "firm_engine",
                "court_of_appeals",
                "confluence",
                "webhook",
                "meta_orch",
            ],
            "The Firm (6 agents)": [
                "quant",
                "red_team",
                "risk",
                "macro",
                "micro",
                "pm",
            ],
            "Guards & Operator": [
                "gate_chain",
                "autopilot_watchdog",
                "operator",
            ],
        },
        "bundle_metadata_updates": {
            "moduleCount": {"before": 10, "after": 11},
            "testCount": {
                "before": 180,
                "after": 1385,
                "note": (
                    "moved from the v0.1.26 Jarvis-bundle-only counter "
                    "(180) to the full eta_engine suite (1385) since "
                    "the admin module + its tests now live inside that "
                    "suite and the bundle panel is the natural place to "
                    "surface overall test health."
                ),
            },
            "shippedVersion": {
                "before": "v0.1.26",
                "after": "v0.1.29",
            },
            "moduleNames_additions": [
                "brain/jarvis_admin  (v0.1.29 -- chain-of-command authority, 46 tests)",
            ],
            "moduleNames_annotations_added": [
                "obs/autopilot_watchdog  (admin-wired, 21 tests)",
            ],
        },
        "jsx_parse_verification": {
            "tool": "@babel/parser",
            "plugins": ["jsx"],
            "error_recovery": False,
            "result": "PARSE OK",
            "bytes": 79474,
            "lines": 1567,
        },
        "python_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "tests_new": new_tests - prev_tests,
        "external_gate": (
            "P9_ROLLOUT remains at 85% pending $1000 Tradovate funded "
            "balance -- required to issue API credentials (app_id, "
            "secret, client_id). Admin command-center panel is ready to "
            "show live audit data the moment JarvisAdmin starts logging "
            "approvals against real trades."
        ),
        "version_numbering_note": (
            "v0.1.27 was consumed by the FINAL REVISION optimization "
            "pipeline and v0.1.28 by JARVIS LIVE SUPERVISION before the "
            "admin command-center panels landed; v0.1.29 is the next "
            "available slot and belongs to the admin surfacing work."
        ),
    }

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to v0.1.29 at {now}")
    print(f"  tests_passing: {prev_tests} -> {new_tests} ({new_tests - prev_tests:+d})  [JSX-only bundle]")
    print("  shared_artifacts.eta_engine_v0_1_29_jarvis_admin_cc written")
    print("  directive satisfied: 'make jarvis the admin ... everyone reports to him'")
    print("  Command Center now renders Chain of Command + Admin Audit Tail")


if __name__ == "__main__":
    main()
