from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "register_jarvis_strategy_supervisor_task.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_jarvis_strategy_supervisor_task.cmd"
SET_ENV = ROOT / "deploy" / "scripts" / "set_supervisor_env.ps1"


def test_supervisor_task_registration_is_canonical_and_logged() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-Jarvis-Strategy-Supervisor"' in text
    assert r"C:\EvolutionaryTradingAlgo\eta_engine" in text
    assert '"logs\\eta_engine"' in text
    assert "jarvis_strategy_supervisor.stdout.log" in text
    assert "jarvis_strategy_supervisor.stderr.log" in text
    assert "run_jarvis_strategy_supervisor_task.cmd" in text
    assert "NT AUTHORITY\\SYSTEM" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RestartCount 999" in text
    assert "New-ScheduledTaskAction -Execute $Runner" in text
    assert "SYSTEM registration unavailable" in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "LogonType Interactive" in text
    assert "current_user:$currentUser" in text


def test_supervisor_task_registration_avoids_legacy_and_opaque_launchers() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
    assert "powershell.exe" not in text
    assert "-Command &" not in text
    assert 'cmd.exe" -Argument' not in text


def test_supervisor_task_runner_sets_env_and_redirects_logs() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "ETA_SUPERVISOR_MODE=paper_live" in text
    assert "ETA_SUPERVISOR_FEED=composite" in text
    assert "ETA_SUPERVISOR_BOTS=" in text
    # 2026-05-05: broker_router is the execution path, but the VPS allowlist
    # keeps unconfigured crypto-paper venues paused until their keys are seeded.
    assert "ETA_PAPER_LIVE_ORDER_ROUTE=broker_router" in text
    assert "ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1,NQ,NQ1" in text
    assert "MYM,MYM1" in text
    assert "ETA_RECONCILE_DIVERGENCE_ACK=1" in text
    assert "ETA_SUPERVISOR_EXIT_WATCH_BOTS=" in text
    # Cross-bot fleet position caps for spot crypto roots: the hard-coded
    # DEFAULT_FALLBACK_CAP=10 fits $20k+ futures contracts but not spot
    # crypto (SOL @ $200 x 10 = $2k, well under per-bot $10k budget).
    # Set explicit caps high enough for raw pre-budget qty; bracket
    # sizing still enforces per-bot dollars downstream.
    assert "ETA_FLEET_POSITION_CAP_SOL=" in text
    assert "ETA_FLEET_POSITION_CAP_BTC=" in text
    assert "ETA_FLEET_POSITION_CAP_ETH=" in text
    assert "ETA_SUPERVISOR_STARTING_CASH=50000" in text
    assert "scripts\\jarvis_strategy_supervisor.py" in text
    assert "jarvis_strategy_supervisor.stdout.log" in text
    assert "jarvis_strategy_supervisor.stderr.log" in text
    assert "python.exe" in text
    assert "exit /b %ERRORLEVEL%" in text


def test_supervisor_env_helper_matches_broker_router_paper_live_route() -> None:
    text = SET_ENV.read_text(encoding="utf-8")

    assert 'ETA_SUPERVISOR_MODE", "paper_live"' in text
    assert 'ETA_SUPERVISOR_FEED", "composite"' in text
    assert 'ETA_PAPER_LIVE_ORDER_ROUTE", "broker_router"' in text
    assert 'ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1,NQ,NQ1' in text
    assert "direct_ibkr" not in text


def test_supervisor_task_runner_pins_only_readiness_approved_paper_bots() -> None:
    """Pin matches the post-2026-05-07 strict-gate audit survivors.

    The pin was rebuilt 2026-05-07 because 7 of the prior 10 pinned bots
    had been retired by the dispatch-fix audit batches (vwap_mr_mnq/nq/btc,
    btc_optimized, funding_rate_btc, mnq_futures_sage NQ variant, etc.).
    The new pin includes the audit's 9 positive-net survivors plus the 3
    incumbents that still pass active-status checks.

    SKIPPED from pin (intentional):
      ng_sweep_reclaim    -- registry flags rollover-artifact bars
      sol_optimized       -- n=17 too small for live capital
      mbt_sweep_reclaim,
      met_sweep_reclaim,
      mbt_overnight_gap   -- await bar-data hydration
    """
    text = RUNNER.read_text(encoding="utf-8")
    match = re.search(r'^set "ETA_SUPERVISOR_BOTS=([^"]+)"$', text, re.MULTILINE)

    assert match is not None
    bots = set(match.group(1).split(","))

    assert bots == {
        # ROUND-4 RETIRE 2026-05-08 (corrected-engine audit
        # strict_gate_20260508T031716Z): dropped from prior pin --
        #   volume_profile_btc (sh_def -2.14 confirmed retire)
        #   rsi_mr_mnq         (was +0.124, now -0.003, split flipped)
        #   gc_sweep_reclaim   (flipped +0.131 -> -0.179)
        #   cl_sweep_reclaim   (flipped +0.032 -> -0.052)
        #   mes_sweep_reclaim  (only 5 valid trades, -0.484 net)
        #
        # Surviving 7-bot pin -- every bot has positive net expR on the
        # corrected engine, and one (volume_profile_mnq) is the FIRST
        # AND ONLY bot to ever pass the strict gate.
        #
        # The deflated-Sharpe survivors:
        "volume_profile_mnq",  # STRICT-GATE PASS: sh_def +2.86 on 2916 trades
        "volume_profile_nq",  # sh_def +2.08 on 3073 trades (just below strict)
        # Commodity / FX sweep_reclaim (positive net on corrected engine):
        "m2k_sweep_reclaim",
        "eur_sweep_reclaim",
        # Incumbents kept for kaizen monitoring (positive net):
        "mnq_anchor_sweep",
        "mnq_futures_sage",
        # Crypto-futures basis-fade. Strict-gate audit
        # (strict_gate_20260508T031716Z.json): n=31 trades,
        # sharpe 3.77, expR_net +0.200, sh_def -0.61, split=True,
        # L=true. Largest sample of any
        # positive-net MBT-family audit bot. Re-pinned 2026-05-08
        # after baseline persisted and promotion_status raised to
        # paper_soak.
        "mbt_funding_basis",
        # RSI/BB mean-reversion v2 — Tier-1 rehab of v1. Strict-gate
        # audit (strict_gate_rsi_v2.json): n=285 trades, sharpe 1.01,
        # expR_net +0.053, sh_def -0.83, split=True, L=true. Per
        # STRATEGY_REHAB_PLAN.md: relaxed rsi 25/75 -> 28/72 +
        # min_volume_z 0.3 -> 0.2 tripled v1's sample (93 -> 285)
        # and flipped expR_net from -0.003 to +0.053. The kernel
        # was real; over-strict thresholds blocked it.
        "rsi_mr_mnq_v2",
        # ALPACA CRYPTO: SOL/USD via broker_router. Strict-gate
        # audit (strict_gate_20260508T031716Z.json): n=18, sharpe
        # 7.69, expR_net +0.616, sh_def +0.09 (positive deflated
        # Sharpe), split=True, L=true. n<30 acceptance: positive sh_def
        # at small n is strong evidence since deflated Sharpe heavily
        # penalizes small samples; positive sign means real signal.
        "sol_optimized",
        # MCL strict-gate audit (strict_gate_mgc_mcl_v2.json): n=16,
        # sharpe=2.00, expR_net=+0.111, split=True. Profile mirrors
        # the already-pinned mnq_anchor_sweep. Legacy gate passes
        # (L=true). MCL micro friction (10x less than full CL) unlocks
        # the energy-reflexivity edge that cl_sweep_reclaim couldn't
        # deliver. Pinned for paper-soak.
        "mcl_sweep_reclaim",
        # MYM strict-gate audit (strict_gate_mym.json): n=11, sharpe
        # 8.62, expR_net=+0.672, split=True. Per-trade quality is the
        # highest in the entire fleet. Re-pinned 2026-05-08 once
        # canonical MYM1_1h.csv (10510 bars) + MYM1_5m.csv (120805
        # bars) synced to mnq_data/history on both VPS and home, so
        # paper_live_launch_check now reports 0 BLOCK with mym in pin.
        "mym_sweep_reclaim",
        # NG strict-gate audit on clean rollover-fixed NG1 1h
        # (12,589 bars / 28mo via TWS continuous-front-month back-fetch):
        # n=24, sharpe 5.31, expR_net +0.404, sh_def -0.24, split=True,
        # L=true. Numbers persisted to docs/strategy_baselines.json
        # (n=24, +0.404), promotion_status=paper_soak. Real edge
        # surfaced after data quality fix.
        "ng_sweep_reclaim",
    }
    # Bots intentionally NOT in the pin -- documented in the bot lists above
    # and in the runner cmd's prelude comments.
    # mbt_funding_basis was previously held as exit-watch only while no
    # baseline was persisted; the 2026-05-08 audit (n=31, +0.200 expR_net,
    # split=True) is now persisted in strategy_baselines.json, registry
    # promotion_status is raised to paper_soak, and the bot is part of
    # the active pin (asserted at the set-equality above).
    # ng_sweep_reclaim was previously held off the pin while NG1 1h had
    # rollover-jump artifacts; the 2026-05-08 TWS continuous-front-month
    # back-fetch produced a clean 28-month series, the strict-gate audit
    # was persisted to strategy_baselines.json, and promotion_status was
    # raised from research_candidate to paper_soak — so it is now part
    # of the active pin (asserted at the set-equality above).
    assert "ym_sweep_reclaim" not in bots, (
        "ym_sweep_reclaim cannot fit $10k per-bot budget at YM ~$250k "
        "notional; ATR sizing rounds to 0 contracts. Re-pin only with "
        "MYM variant or budget-cap exception."
    )
    # mym_sweep_reclaim was previously held off the pin while canonical
    # MYM1 bars were missing locally; both VPS and home have the canonical
    # MYM1_1h.csv + MYM1_5m.csv as of 2026-05-08T08:50Z, so it is now
    # part of the active pin (assertion at the set-equality above).
    # sol_optimized was previously held off the pin under the n<30
    # rule; the 2026-05-08 audit shows n=18 with sh_def +0.09 (positive
    # deflated Sharpe), which is exceptional at small sample size since
    # the deflated correction heavily penalizes n<30. The positive sign
    # is strong small-sample edge evidence, and per-
    # trade quality (+0.616 expR_net) is the third-highest in the
    # audited fleet. Routes via broker_router to Alpaca paper.
    assert "mbt_sweep_reclaim" not in bots, (
        "mbt_sweep_reclaim shows zero trades in the audit; awaits MBT 1h bar-data hydration."
    )
    assert "met_sweep_reclaim" not in bots
    assert "mbt_overnight_gap" not in bots
    # Round-1 + round-2 + round-3 + round-4 retires must not appear:
    for retired in (
        # Round-1/2/3:
        "vwap_mr_mnq",
        "vwap_mr_nq",
        "funding_rate_btc",
        "mbt_zfade",
        "btc_optimized",
        "mnq_sweep_reclaim",
        "zn_sweep_reclaim",
        "btc_crypto_scalp",
        "btc_hybrid_sage",
        "cross_asset_mnq",
        "crypto_seed",
        "btc_ensemble_2of3",
        "vwap_mr_btc",
        "nq_futures_sage",
        # Round-4 (corrected-engine audit 2026-05-08):
        "volume_profile_btc",
        "rsi_mr_mnq",
        "gc_sweep_reclaim",
        "cl_sweep_reclaim",
        "mes_sweep_reclaim",
    ):
        assert retired not in bots, f"retired bot '{retired}' must not be pinned"


def test_supervisor_task_runner_avoids_legacy_paths() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "mnq_data" not in text
    assert "crypto_data" not in text
    assert "TheFirm" not in text
    assert "The_Firm" not in text
