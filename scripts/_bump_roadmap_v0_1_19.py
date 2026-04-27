"""One-shot: bump roadmap_state.json to v0.1.19.

Closes out P8_COMPLY (72% -> 100%). Two in-progress tasks land:

  * two_fa                -- core/two_factor.py (TOTP + hardware-key registry
                             + cold-wallet gate) with 25 tests. Stdlib-only
                             RFC 6238 (no pyotp dep). CopyPolicy matrix:
                             TOTP_ONLY / HARDWARE_ONLY / TOTP_OR_HARDWARE /
                             BOTH. Gate covers 6 ops: withdraw_cold,
                             stake_withdraw, cross_wallet_transfer,
                             promote_strategy_to_live, register_new_api_key,
                             disable_kill_switch.
  * cftc_nfa_compliance   -- core/cftc_nfa_compliance.py (9-rule pre-trade
                             checklist) with 19 tests. BLOCKING rules:
                             OWNS_ACCOUNT, NO_EXTERNAL_CAPITAL,
                             NO_POOL_MANAGEMENT, NO_SELF_MATCH,
                             APEX_NO_CROSS_HEDGE, APEX_NEWS_BLACKOUT,
                             NFA_2_29_PROMOTIONAL. ADVISORY rules:
                             NO_LAYER_CANCEL, APEX_ONE_ACCOUNT.

Adds 44 tests (821 -> 865).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def _find_task(phase: dict, task_id: str) -> dict:
    for t in phase["tasks"]:
        if t.get("id") == task_id:
            return t
    raise KeyError(f"task {task_id} not found in phase {phase.get('id')}")


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    sa["eta_engine_tests_passing"] = 865

    by_id = {p["id"]: p for p in state["phases"]}
    p8 = by_id["P8_COMPLY"]
    p8["progress_pct"] = 100
    p8["status"] = "done"

    two_fa = _find_task(p8, "two_fa")
    two_fa["status"] = "done"
    two_fa["note"] = (
        "core/two_factor.py + 25 tests. Stdlib-only RFC 6238 TOTP "
        "(HMAC-SHA1 over 30s steps, 6 digits) -- no pyotp dep. "
        "TotpSecret wraps a SECRETS ref; str() redacts. HardwareKey "
        "stores only public WebAuthn metadata (kid/aaguid/uv). "
        "SecurityRegistry holds policy = TOTP_ONLY | HARDWARE_ONLY | "
        "TOTP_OR_HARDWARE | BOTH. gate_cold_wallet_op() raises "
        "TwoFactorRequiredError on missing claim, TwoFactorFailedError "
        "on bad claim. Gated ops: withdraw_cold, stake_withdraw, "
        "cross_wallet_transfer, promote_strategy_to_live, "
        "register_new_api_key, disable_kill_switch. RFC 6238 test "
        "vectors pass; +/- 1 window tolerates 30s clock drift."
    )

    cftc = _find_task(p8, "cftc_nfa_compliance")
    cftc["status"] = "done"
    cftc["note"] = (
        "core/cftc_nfa_compliance.py + 19 tests. 9 rules wired: "
        "CFTC.OWNS_ACCOUNT / NO_EXTERNAL_CAPITAL / NO_POOL_MANAGEMENT "
        "/ NO_SELF_MATCH (BLOCKING), CFTC.NO_LAYER_CANCEL (ADVISORY >1Hz), "
        "APEX.ONE_ACCOUNT_PER_TRADE (ADVISORY), APEX.NO_CROSS_HEDGE / "
        "APEX.NEWS_BLACKOUT (BLOCKING), NFA.2_29_PROMOTIONAL (BLOCKING "
        "when disclaimer missing). check_compliance(ctx) returns "
        "ComplianceCheckResult with model_validator enforcing that "
        "passed=True can never coexist with a BLOCKING violation."
    )

    # New compliance-layer shared artifact summary
    sa["eta_engine_p8_comply"] = {
        "timestamp_utc": now,
        "completed_tasks": ["two_fa", "cftc_nfa_compliance"],
        "new_modules": [
            "eta_engine/core/two_factor.py",
            "eta_engine/core/cftc_nfa_compliance.py",
        ],
        "new_test_files": [
            "tests/test_two_factor.py (25 tests)",
            "tests/test_cftc_nfa_compliance.py (19 tests)",
        ],
        "tests_new": 44,
        "policy_matrix": {
            "TOTP_ONLY": "TOTP must verify.",
            "HARDWARE_ONLY": "hardware_kid must be registered.",
            "TOTP_OR_HARDWARE": "either proof suffices.",
            "BOTH": "both required simultaneously.",
        },
        "gated_ops": [
            "withdraw_cold",
            "stake_withdraw",
            "cross_wallet_transfer",
            "promote_strategy_to_live",
            "register_new_api_key",
            "disable_kill_switch",
        ],
        "cftc_rules_blocking": [
            "CFTC.OWNS_ACCOUNT",
            "CFTC.NO_EXTERNAL_CAPITAL",
            "CFTC.NO_POOL_MANAGEMENT",
            "CFTC.NO_SELF_MATCH",
            "APEX.NO_CROSS_HEDGE",
            "APEX.NEWS_BLACKOUT",
            "NFA.2_29_PROMOTIONAL",
        ],
        "cftc_rules_advisory": [
            "CFTC.NO_LAYER_CANCEL",
            "APEX.ONE_ACCOUNT_PER_TRADE",
        ],
        "notes": (
            "Compliance layer is now closed. two_factor gates ALL "
            "cold-wallet value-movement ops; check_compliance() is the "
            "canonical pre-trade gate for the risk_engine order path."
        ),
    }

    # P8 done. Overall weighted progress ticks up a point.
    state["overall_progress_pct"] = 99

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.19 at {now}")
    print("  tests_passing: 821 -> 865 (+44)")
    print("  P8_COMPLY: 72% -> 100% (two_fa, cftc_nfa_compliance -> done)")
    print("  overall_progress_pct: 99 (compliance layer CLOSED)")


if __name__ == "__main__":
    main()
