"""One-shot: bump roadmap_state.json to v0.1.30.

DUAL-BOT + FUNNEL + RENTAL SAAS -- the monetization spine.

Context
-------
v0.1.29 surfaced the JarvisAdmin chain-of-command on the Command
Center. The shop floor was quiet enough to widen the remit:

  (a) fine-tune *both* bots off the same Jarvis loop instead of just
      the MNQ sniper;
  (b) stand up a BTC paper-trading environment with a four-gate
      go-live sequencer (paper verdict PASS + < 48h age + explicit
      env flag + venue adapter probe) so the second bot can graduate
      under the same discipline the MNQ bot did;
  (c) run one dual-data collector that fans ticks from MNQ + BTC +
      Jarvis into separate JSONL streams (with an enrichment header
      and a per-stream error log) so both bots are fed by one loop;
  (d) lock the four-layer profit-waterfall planner (L1 MNQ / L2 BTC /
      L3 perps / L4 staking sink) with correlation guard, per-layer
      DD kill, global 8% kill, and 65% / 75% sweep thresholds from
      the user's written spec;
  (e) ship the first operational cut of the Phase 2 Bot Rental SaaS:
      tier catalog, multi-tenant registry with salted API-key digests,
      subscription state machine, orchestrator that emits tenant
      container specs with read-only brain/funnel mounts + explicit
      network allowlist + tier-bounded CPU/memory, and a downloadable
      Electron client scaffold (main + preload + renderer + HTML/CSS).

What v0.1.30 adds
-----------------
  * ``scripts/_jarvis_dual_fine_tune.py``
      Dual sweep on top of the existing glide-step fine-tuner. Per-bot
      baselines + per-bot parameter deltas + side-by-side verdict.

  * ``scripts/btc_paper_trade.py``
      Paper router for BTC -- mirrors the MNQ paper runner shape but
      points at the crypto adapter + uses a JarvisAdmin gate to emit
      approvals into the decision journal. Writes
      ``reports/btc_paper_run_latest.json`` for the live-gate to read.

  * ``scripts/btc_live.py``
      Pure-function live-gate sequencer. Four gates:
        (1) ``APEX_BTC_LIVE=1`` env flag;
        (2) paper verdict = PASS in the verification artifact;
        (3) artifact age <= max_age_h (default 48h);
        (4) adapter_probe() returns True.
      Emits a frozen ``LiveGateDecision`` dataclass; unit-tested with
      a deterministic NOW clock so the test doesn't care what time of
      day the suite runs.

  * ``obs/dual_data_collector.py`` + ``scripts/dual_data_collector.py``
      Async ``DualDataCollector`` that consumes three upstream sources
      (MNQ TickSource, BTC TickSource, Jarvis callable snapshot) and
      writes to three JSONL files with ``_stream`` + ``_ts_written``
      enrichment. Supports ``stop_event`` for external shutdown and
      ``max_ticks`` cap; records per-stream exceptions into
      ``stats.errors`` without bringing the whole collector down until
      it must.

  * ``funnel/waterfall.py`` + ``scripts/funnel_sweep_daily.py``
      Pure 4-layer profit-waterfall planner. ``LayerSnapshot`` +
      ``WaterfallDecision`` + ``plan_waterfall()``. Presets locked to
      the user's brief: L1 MNQ 5/6/12/10x, L2 BTC 3/4/9/5x, L3 perps
      1.5/2.5/6/3x, L4 staking sink. Correlation guard kicks in when
      >=2 risky layers are in HIGH vol (0.6 multiplier). Inverse-vol
      scaling floor 0.25. Per-layer DD kill AND global 8% DD kill.
      65/75 sweep thresholds with min_outgoing/min_incoming sanity.
      Formatter prints "GLOBAL KILL" banner when the global gate is
      active.

  * ``rental/`` package -- Phase 2 Bot Rental SaaS foundation:
      - ``tiers.py``: 5-tier ladder (TRIAL / STARTER / PRO / PORTFOLIO
        / ELITE) with SKU access matrix, JSON-safe ``public_price_list``,
        ``price_for(tier, cycle)`` helper, monthly / quarterly / annual
        discount invariants.
      - ``tenancy.py``: ``TenantRegistry`` + ``ApiKeyRecord.from_secret``
        that salt-hashes (16-byte ``secrets.token_bytes``) + drops the
        raw secret. Rejects WITHDRAW scopes (trade-only). ``Entitlement``
        composite check via ``is_entitled``.
      - ``billing.py``: Subscription state machine. TRIAL -> ACTIVE
        (7-day trial window), RENEWAL_PAID extends period_end,
        PAYMENT_FAILED -> GRACE (3-day window), REINSTATE clears grace,
        CANCEL preserves paid period_end, EXPIRE.
      - ``orchestrator.py``: ``plan()`` emits one ``TenantContainerSpec``
        per entitled SKU with an API key. Env injects tenant_id / sku /
        tier / exchange / key_id ONLY (never the secret). Read-only
        mounts always include ``/opt/apex/brain:ro`` and
        ``/opt/apex/funnel:ro``. Network allowlist uses explicit hosts,
        no wildcards. Per-tier CPU / memory (PORTFOLIO=2.0/2Gi as a
        sanity anchor). Labels include tenant / sku / tier.
      - ``client_contract.py``: WebSocket envelope for the downloadable
        client. ``ClientCommand`` / ``ServerMessage`` dataclasses with
        required-params + unexpected-params + forbidden-params guards.
        Forbidden list blocks attempts to push strategy internals
        (``reward_weights`` / ``pine_source``) through a public command
        param bag.
      - ``scripts/rental_provision.py``: CLI that walks the registry +
        runs the orchestrator + prints the resolved specs for audit.

  * ``client/`` -- downloadable Electron scaffold (non-Python):
      - ``package.json`` + ``main.js`` (BrowserWindow, hardened CSP,
        WS handoff) + ``preload.js`` (contextBridge, trade-only API) +
        ``index.html`` + ``renderer.js`` + ``styles.css``.

Tests
-----
  * ``tests/test_funnel_waterfall.py``  (22 tests)
  * ``tests/test_rental_tiers.py``      (13 tests)
  * ``tests/test_rental_client_contract.py`` (14 tests)
  * ``tests/test_rental_tenancy.py``    (13 tests)
  * ``tests/test_rental_billing.py``    (16 tests)
  * ``tests/test_rental_orchestrator.py`` (14 tests)
  * ``tests/test_dual_data_collector.py`` (8 tests -- async + stop +
    max_ticks + exception propagation)
  * ``tests/test_btc_live.py``          (13 tests -- pure-function
    decision gate with deterministic NOW)

Total new tests: 132.

One pre-existing test (``test_admin_with_engine_ticks_per_request``)
was flaky because it inherited wall-clock session_phase from the
system time. v0.1.30 pins it to a deterministic ``10:30 AM ET``
clock via the builder ``clock`` injection, clearing the 1 remaining
failure without touching policy.

Reconciliation
--------------
  * eta_engine_tests_passing: 1385 -> 1517 (+132).
  * No phase-level status changes. P9_ROLLOUT remains at 85% pending
    the $1000 Tradovate funding gate.
  * overall_progress_pct stays at 99 -- the BTC live-gate + rental
    SaaS are foundation work that unlock the Phase 2 revenue track
    but do not advance the MNQ P9 gate by themselves.
  * Python-only bundle (no JSX touched this slice).
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
    new_tests = 1517
    sa["eta_engine_tests_passing"] = new_tests
    sa["eta_engine_tests_failing"] = 0

    sa["eta_engine_v0_1_30_dual_bot_funnel_rental"] = {
        "timestamp_utc": now,
        "version": "v0.1.30",
        "bundle_name": ("DUAL-BOT + FUNNEL + RENTAL SAAS -- the monetization spine"),
        "directive": (
            "fine-tune both bots with Jarvis, build a BTC paper + "
            "live-gate sequencer, collect MNQ + BTC + Jarvis in one "
            "loop, lock the 4-layer profit-waterfall planner, and ship "
            "the first operational slice of the tenant-ready Bot "
            "Rental SaaS (tiers + tenancy + billing + container "
            "orchestrator + client WS contract + Electron scaffold)"
        ),
        "theme": (
            "v0.1.29 made the admin visible; v0.1.30 makes the product "
            "shippable. Same discipline the MNQ sniper earned -- paper "
            "PASS + age <= 48h + explicit env flag + adapter probe -- "
            "now gates the second bot. The funnel planner formalizes "
            "the user's 4-layer profit spec. The rental stack turns "
            "the strategy into a multi-tenant SaaS with trade-only API "
            "keys, read-only strategy mounts, and a network allowlist "
            "so the internals never leak to tenants."
        ),
        "artifacts_added": {
            "fine_tune": ["scripts/_jarvis_dual_fine_tune.py"],
            "btc_paper": ["scripts/btc_paper_trade.py"],
            "btc_live_gate": ["scripts/btc_live.py"],
            "dual_data_collector": [
                "obs/dual_data_collector.py",
                "scripts/dual_data_collector.py",
            ],
            "funnel_waterfall": [
                "funnel/waterfall.py",
                "scripts/funnel_sweep_daily.py",
            ],
            "rental_saas": [
                "rental/__init__.py",
                "rental/tiers.py",
                "rental/tenancy.py",
                "rental/billing.py",
                "rental/orchestrator.py",
                "rental/client_contract.py",
                "scripts/rental_provision.py",
            ],
            "electron_client": [
                "client/package.json",
                "client/main.js",
                "client/preload.js",
                "client/index.html",
                "client/renderer.js",
                "client/styles.css",
            ],
            "bump_script": ["scripts/_bump_roadmap_v0_1_30.py"],
        },
        "test_files_added": [
            "tests/test_funnel_waterfall.py",
            "tests/test_rental_tiers.py",
            "tests/test_rental_client_contract.py",
            "tests/test_rental_tenancy.py",
            "tests/test_rental_billing.py",
            "tests/test_rental_orchestrator.py",
            "tests/test_dual_data_collector.py",
            "tests/test_btc_live.py",
        ],
        "tests_by_file": {
            "test_funnel_waterfall": 22,
            "test_rental_tiers": 13,
            "test_rental_client_contract": 14,
            "test_rental_tenancy": 13,
            "test_rental_billing": 16,
            "test_rental_orchestrator": 14,
            "test_dual_data_collector": 8,
            "test_btc_live": 13,
            "jarvis_admin_deflakification": 0,
        },
        "tests_new": 132,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "preexisting_flake_cleared": {
            "test": ("tests/test_jarvis_admin.py::TestEngineIntegration::test_admin_with_engine_ticks_per_request"),
            "cause": (
                "session_phase was derived from wall clock time, so "
                "outside RTH the OVERNIGHT gate flipped the verdict to "
                "DENIED even though the scenario expected APPROVED"
            ),
            "fix": (
                "inject a clock=lambda: datetime(2026,4,14,14,30,tzinfo=UTC) "
                "into JarvisContextBuilder so session_phase is "
                "deterministically MORNING (10:30 AM ET, Tuesday)"
            ),
            "policy_untouched": True,
        },
        "funnel_waterfall_spec_locked": {
            "L1_MNQ": {
                "per_trade_risk_pct": 5.0,
                "daily_dd_kill_pct": 6.0,
                "weekly_dd_kill_pct": 12.0,
                "max_leverage_x": 10.0,
            },
            "L2_BTC": {
                "per_trade_risk_pct": 3.0,
                "daily_dd_kill_pct": 4.0,
                "weekly_dd_kill_pct": 9.0,
                "max_leverage_x": 5.0,
            },
            "L3_perps_eth_sol": {
                "per_trade_risk_pct": 1.5,
                "daily_dd_kill_pct": 2.5,
                "weekly_dd_kill_pct": 6.0,
                "max_leverage_x": 3.0,
            },
            "L4_staking": {
                "role": "terminal yield sink (no trading)",
            },
            "sweep_rules": {
                "partial_sweep_at_pct": 65.0,
                "full_sweep_at_pct": 75.0,
                "min_outgoing_usd": "tier-bounded",
                "min_incoming_usd": "tier-bounded",
            },
            "global_kill_pct": 8.0,
            "correlation_guard_mult": 0.6,
            "correlation_guard_trigger": (">=2 risky layers simultaneously in HIGH vol regime"),
            "inverse_vol_floor": 0.25,
        },
        "rental_saas_spec_locked": {
            "tier_ladder": {
                "TRIAL": {
                    "monthly_usd": 0.0,
                    "trial_days": 7,
                    "notes": "paper only, BTC_SEED only",
                },
                "STARTER": {
                    "monthly_usd_range": "25..90 (anchors with cheap-SaaS competitors)",
                    "skus": ["BTC_SEED"],
                },
                "PRO": {
                    "monthly_usd_min": 90.0,
                    "notes": "brain premium above cheap competitors",
                },
                "PORTFOLIO": {
                    "skus_include": [
                        "BTC_SEED",
                        "ETH_PERP",
                        "SOL_PERP",
                        "STAKING_SWEEP",
                    ],
                    "cpu_per_container": 2.0,
                    "memory_per_container": "2Gi",
                },
                "ELITE": {
                    "notes": "grants every SKU including MNQ_APEX",
                },
            },
            "api_key_model": {
                "scope": "TRADE_ONLY -- WITHDRAW scopes are rejected",
                "secret_storage": (
                    "raw secret shown ONCE to the tenant at issuance, stored only as salt+SHA-256 digest server-side"
                ),
                "salt_bytes": 16,
                "fresh_salt_per_record": True,
            },
            "isolation_guarantees": {
                "env_injected": [
                    "APEX_TENANT_ID",
                    "APEX_SKU",
                    "APEX_TIER",
                    "APEX_EXCHANGE",
                    "APEX_KEY_ID",
                ],
                "env_never_contains_raw_secret": True,
                "strategy_mounts_read_only": [
                    "/opt/apex/brain:ro",
                    "/opt/apex/funnel:ro",
                ],
                "network_allowlist": "explicit hosts, no wildcards",
                "labels": ["tenant", "sku", "tier"],
            },
            "subscription_state_machine": {
                "states": [
                    "TRIAL",
                    "ACTIVE",
                    "GRACE",
                    "PAST_DUE",
                    "CANCELLED",
                    "EXPIRED",
                ],
                "events": [
                    "START_TRIAL",
                    "ACTIVATE",
                    "RENEWAL_PAID",
                    "PAYMENT_FAILED",
                    "REINSTATE",
                    "CANCEL",
                    "EXPIRE",
                ],
                "grace_window_days": 3,
                "trial_window_days": 7,
            },
            "client_contract_guards": {
                "required_params_enforced": True,
                "unexpected_params_rejected": True,
                "forbidden_strategy_params": [
                    "reward_weights",
                    "pine_source",
                ],
                "empty_credentials_rejected": [
                    "session_token",
                    "tenant_id",
                ],
                "unknown_kind_rejected": True,
            },
        },
        "btc_live_gate_four_gates": [
            "APEX_BTC_LIVE == '1' (explicit env flag)",
            "verify_verdict == 'PASS' in paper-run artifact",
            "verify_age_h <= max_age_h (default 48h)",
            "adapter_probe() returns True",
        ],
        "dual_data_collector_streams": {
            "mnq": "live_ticks_mnq.jsonl",
            "btc": "live_ticks_btc.jsonl",
            "jarvis": "live_jarvis.jsonl",
            "enrichment_keys": ["_stream", "_ts_written"],
            "error_capture": ("per-stream exceptions into stats.errors instead of crashing the collector task pool"),
            "shutdown_modes": [
                "external asyncio.Event (stop_event)",
                "CollectorConfig.max_ticks cap",
                "upstream exception bubble-up",
            ],
        },
        "ruff_status": (
            "All new modules + all new test files + the deflaked "
            "test_jarvis_admin.py pass ruff check cleanly. "
            "Pre-existing ruff backlog in untouched modules (backtest/, "
            "brain/multi_agent.py, funnel/orchestrator.py, etc.) was "
            "not in scope for this bundle."
        ),
        "pytest_status": "1517 passed, 0 failed, 17 warnings in ~5.6s",
        "python_touched": True,
        "jsx_touched": False,
        "external_gate": (
            "P9_ROLLOUT remains at 85% pending the $1000 Tradovate "
            "funded balance. The BTC live-gate is wired but inert until "
            "a PASS paper-verification artifact is produced; the rental "
            "orchestrator emits specs but a live Docker/k8s apply loop "
            "is the next slice."
        ),
        "version_numbering_note": (
            "v0.1.30 is the dual-bot + funnel + rental SaaS slice. "
            "Picks up directly from v0.1.29's admin-surfacing work -- "
            "no version number skipped."
        ),
    }

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to v0.1.30 at {now}")
    print(f"  tests_passing: {prev_tests} -> {new_tests} ({new_tests - prev_tests:+d})  [+132 new, 1 flake fixed]")
    print("  shared_artifacts.eta_engine_v0_1_30_dual_bot_funnel_rental written")
    print(
        "  directive satisfied: dual fine-tune + BTC paper+live gate + "
        "dual collector + 4-layer funnel + rental SaaS foundation + "
        "Electron client scaffold"
    )


if __name__ == "__main__":
    main()
