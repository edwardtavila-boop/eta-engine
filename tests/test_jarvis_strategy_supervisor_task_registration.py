from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "register_jarvis_strategy_supervisor_task.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_jarvis_strategy_supervisor_task.cmd"


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
    assert 'New-ScheduledTaskAction -Execute $Runner' in text


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
    assert "ETA_RECONCILE_DIVERGENCE_ACK=1" in text
    assert "ETA_SUPERVISOR_STARTING_CASH=50000" in text
    assert "scripts\\jarvis_strategy_supervisor.py" in text
    assert "jarvis_strategy_supervisor.stdout.log" in text
    assert "jarvis_strategy_supervisor.stderr.log" in text
    assert "python.exe" in text
    assert "exit /b %ERRORLEVEL%" in text


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
        # The deflated-Sharpe survivor (sh_def +1.98).
        "volume_profile_mnq",
        # Top mid-tier survivor.
        "rsi_mr_mnq",
        # Crypto-futures research-candidate.
        "mbt_funding_basis",
        # Commodity sweep_reclaim family (all positive expR_net in audit).
        # ym_sweep_reclaim removed 2026-05-07 18:05 EDT after observing
        # YM @ ~$250k notional couldn't fit $10k per-bot budget;
        # supervisor logged "budget cap produced qty=0" 3 times in 5
        # minutes. Re-pin only with MYM variant or budget-cap exception.
        "mes_sweep_reclaim",
        "m2k_sweep_reclaim",
        "eur_sweep_reclaim",
        "gc_sweep_reclaim",
        "cl_sweep_reclaim",
        # Incumbents kept for monitoring / kaizen-recommended SCALE_UP.
        "volume_profile_btc",
        "mnq_anchor_sweep",
        "mnq_futures_sage",
    }
    # Bots intentionally NOT in the pin -- documented in the bot lists above
    # and in the runner cmd's prelude comments.
    assert "ng_sweep_reclaim" not in bots, (
        "ng_sweep_reclaim has rollover-artifact bar data; do not pin until "
        "NG1 1h is re-fetched on canonical rollover-adjusted source."
    )
    assert "ym_sweep_reclaim" not in bots, (
        "ym_sweep_reclaim cannot fit $10k per-bot budget at YM ~$250k "
        "notional; ATR sizing rounds to 0 contracts. Re-pin only with "
        "MYM variant or budget-cap exception."
    )
    assert "sol_optimized" not in bots, (
        "sol_optimized has only 17 trades in the audit; too small for live "
        "capital allocation."
    )
    assert "mbt_sweep_reclaim" not in bots, (
        "mbt_sweep_reclaim shows zero trades in the audit; awaits "
        "MBT 1h bar-data hydration."
    )
    assert "met_sweep_reclaim" not in bots
    assert "mbt_overnight_gap" not in bots
    # Round-1 + round-2 + round-3 retires must not appear:
    for retired in (
        "vwap_mr_mnq", "vwap_mr_nq", "funding_rate_btc", "mbt_zfade",
        "btc_optimized", "mnq_sweep_reclaim", "zn_sweep_reclaim",
        "btc_crypto_scalp", "btc_hybrid_sage", "cross_asset_mnq",
        "crypto_seed", "btc_ensemble_2of3", "vwap_mr_btc",
        "nq_futures_sage",
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
