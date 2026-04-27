"""One-shot: bump roadmap_state.json to v0.1.37.

HIGH_VOL EXCLUSION GATE -- operationalises the v0.1.32 cross-regime
sign-flip finding.

Context
-------
v0.1.32 shipped the cross-regime OOS validation harness, which produced
a FAIL verdict on its first run:

    HIGH_VOL  IS +0.216R  ->  OOS -0.559R  (sign flip, deg +358.7%)

The validation report's recommendation was literally "exclude this
regime". v0.1.32 surfaced the finding but the live policy was still
sizing into HIGH_VOL at 0.5x risk -- the half-discount predates the
OOS evidence and is no longer defensible.

v0.1.37 closes that gap.

What v0.1.37 adds
-----------------
  * ``strategies/regime_exclusion.py`` (~170 lines, new)

    Single source of truth for which regimes the live policy must
    refuse to size into. Loads from
    ``docs/cross_regime/regime_exclusions.json`` if present, falls
    through to a hard-coded default that includes HIGH_VOL (per the
    OOS verdict) and CRISIS (structural unmodellable spreads).

    API:
      - ``is_regime_excluded(label) -> ExclusionDecision`` -- gate
        check with a human-readable reason.
      - ``excluded_regimes() -> dict[str, str]`` -- full map.
      - ``write_default_config(force=False) -> Path`` -- bootstrap
        the JSON for hand-editing.

    Behaviour:
      - mtime-keyed cache; edits picked up live on next call.
      - Corrupt or missing JSON falls back to defaults with a single
        stderr warning -- never raises.
      - Unknown regime labels fail OPEN (typo doesn't silently kill
        all sizing).
      - Case-insensitive lookup.

  * ``strategies/eta_policy.py`` (patch)

    ``_risk_mult()`` now consults ``is_regime_excluded()`` before
    applying any other multipliers. If the regime is in the
    exclusion set, returns 0.0 immediately. The legacy LOW_VOL
    hard-zero is preserved (structural rule, not OOS-derived) and
    the previous HIGH_VOL=0.5x branch is removed (replaced by the
    exclusion gate).

  * ``docs/cross_regime/regime_exclusions.json`` (new)

    Hand-editable runtime config. Operators can add/remove regimes
    here without a code change or restart. Carries spec_id,
    generated_at_utc, source attribution, and notes.

  * ``tests/test_strategies_regime_exclusion.py`` (~220 lines, +24 tests)

    Coverage:
      - default exclusions: HIGH_VOL, CRISIS excluded; TRENDING,
        RANGING, LOW_VOL not excluded via this gate
      - case-insensitive lookups
      - ExclusionDecision __bool__ enables idiomatic gate usage
      - disk override: nested {excluded_regimes:{}} and flat dict
        formats both accepted
      - corrupt JSON / non-dict payload -> graceful fallback to
        defaults
      - mtime cache invalidation works (rewrite + os.utime forward)
      - write_default_config: writes when absent, no-op when
        present, force=True overwrites
      - eta_policy._risk_mult integration: HIGH_VOL/CRISIS -> 0.0,
        TRENDING/RANGING pass through, LOW_VOL legacy zero still
        applies, kill_switch + session_closed still override, vol_z
        penalty preserved on non-excluded regimes
      - HIGH_VOL can be re-enabled by writing an empty exclusion map
        (proves the override mechanism works in both directions)

Why this matters
----------------
Before v0.1.37, the live policy was DEFINITIONALLY overfit-fragile in
HIGH_VOL: the OOS data said the edge sign-flips, but the policy still
sized at 50% there. v0.1.37 enforces the OOS verdict at the strategy
boundary. Re-enabling HIGH_VOL now requires a documented config edit
(which the operator is forced to confront when they make it) AND the
expectation that they re-run cross_regime_validation first.

Acceptance criteria
-------------------
  * ruff clean on regime_exclusion.py + eta_policy.py + new test file
  * 1795 / 1795 pytest pass (verified 2026-04-17)
  * No HIGH_VOL signal in eta_policy can produce risk_mult > 0 with
    default config
  * Operator override path proven: empty exclusion map restores the
    previous behaviour
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
    new_tests = 1795  # measured from full pytest sweep 2026-04-17
    sa["eta_engine_tests_passing"] = new_tests

    sa["eta_engine_v0_1_37_high_vol_exclusion"] = {
        "timestamp_utc": now,
        "version": "v0.1.37",
        "bundle_name": ("HIGH_VOL EXCLUSION GATE -- operationalising the cross_regime sign-flip finding"),
        "directive": ("ok be a support for the main apex predator and automate the rest of the phases"),
        "theme": (
            "v0.1.32 produced a FAIL verdict (HIGH_VOL sign-flip "
            "overfit, IS +0.216R -> OOS -0.559R) but eta_policy "
            "was still sizing HIGH_VOL at 0.5x risk. v0.1.37 ships "
            "the exclusion gate, the editable runtime config, the "
            "policy integration, and 24 new tests proving the gate "
            "fires hard on HIGH_VOL and can be reversibly disabled "
            "without a code change."
        ),
        "artifacts_added": {
            "modules": ["strategies/regime_exclusion.py"],
            "patches": ["strategies/eta_policy.py"],
            "configs": ["docs/cross_regime/regime_exclusions.json"],
            "tests": ["tests/test_strategies_regime_exclusion.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_37.py"],
        },
        "regime_exclusion_module": {
            "spec_id": "REGIME_EXCLUSION_v1",
            "default_excluded_regimes": ["HIGH_VOL", "CRISIS"],
            "config_path": ("docs/cross_regime/regime_exclusions.json"),
            "cache_strategy": ("mtime-keyed; edits picked up live on next call, no restart required"),
            "fail_open_policy": (
                "unknown regime labels are NEVER excluded -- a typo in the classifier must not silently kill all sizing"
            ),
            "graceful_fallback": (
                "corrupt JSON, non-dict payload, missing file all "
                "fall through to hard-coded defaults with a single "
                "stderr warning; never raises"
            ),
            "supported_config_shapes": [
                '{"excluded_regimes": {label: reason}}',
                "{label: reason}  (flat)",
            ],
        },
        "eta_policy_patch": {
            "function": "_risk_mult",
            "before": ("HIGH_VOL: mult *= 0.5  (half-discount, pre-OOS)"),
            "after": (
                "exclusion gate consulted first; HIGH_VOL/CRISIS "
                "-> return 0.0; LOW_VOL legacy structural zero "
                "preserved; vol_z>2.5 penalty preserved"
            ),
            "import_added": ("from eta_engine.strategies.regime_exclusion import is_regime_excluded"),
        },
        "test_coverage": {
            "fast_tests_added": 24,
            "test_classes": [
                "TestDefaultExclusions (8 tests)",
                "TestDiskOverride (5 tests)",
                "TestWriteDefaultConfig (3 tests)",
                "TestRiskMultIntegration (8 tests)",
            ],
            "key_assertions": [
                "HIGH_VOL excluded by default",
                "CRISIS excluded by default",
                "TRENDING/RANGING NOT excluded",
                "case-insensitive label lookup",
                "ExclusionDecision __bool__ works",
                "JSON override replaces defaults",
                "flat-dict format accepted",
                "corrupt JSON falls through",
                "non-dict payload falls through",
                "mtime cache invalidates on rewrite + os.utime",
                "write_default_config respects existing file",
                "force=True overwrites",
                "_risk_mult HIGH_VOL -> 0.0",
                "_risk_mult CRISIS -> 0.0",
                "_risk_mult TRENDING -> base_mult unchanged",
                "_risk_mult RANGING -> base_mult unchanged",
                "_risk_mult LOW_VOL -> 0.0 (legacy preserved)",
                "kill_switch overrides any regime",
                "session_closed overrides any regime",
                "HIGH_VOL re-enable via empty exclusion map proven",
                "vol_z>2.5 penalty preserved on non-excluded regimes",
            ],
        },
        "ruff_clean_on": [
            "strategies/regime_exclusion.py",
            "strategies/eta_policy.py",
            "tests/test_strategies_regime_exclusion.py",
        ],
        "operational_impact": {
            "live_sizing_change": (
                "HIGH_VOL signals now produce risk_mult=0.0 "
                "(previously 0.5*base_mult). This eliminates the "
                "post-OOS overfit exposure."
            ),
            "reversibility": (
                "Operator can edit regime_exclusions.json to "
                "remove HIGH_VOL after re-validation; next "
                "_risk_mult call picks up the change without "
                "restart"
            ),
            "expected_re_enable_criteria": (
                "cross_regime_validation must show HIGH_VOL OOS "
                "expectancy >= 0.15R AND OOS trades >= 20 AND "
                "degradation <= 60% before exclusion is removed"
            ),
        },
        "phase_reconciliation": {
            "P3_PROOF.regime_validation": (
                "v0.1.32 added the harness; v0.1.37 enforces its "
                "verdict at the strategy boundary -- the loop is "
                "now closed: validation produces a finding, the "
                "policy obeys it"
            ),
            "overall_progress_pct": 99,
            "status": "unchanged -- still funding-gated on P9_ROLLOUT",
        },
        "python_touched": True,
        "jsx_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "delta_tests": new_tests - prev_tests,
    }

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print("roadmap_state.json bumped to v0.1.37")
    print(f"  tests: {prev_tests} -> {new_tests}")
    print("  excluded_regimes (default): HIGH_VOL, CRISIS")


if __name__ == "__main__":
    main()
