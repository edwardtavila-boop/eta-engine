"""One-shot: bump roadmap_state.json to v0.1.22.

Closes out P6_FUNNEL (90% -> 100%). Two tasks land:

  * exchange_transfer -- funnel/transfer.py enhanced with TransferPolicy
                         (per-txn + daily-rolling limits, whitelist,
                         approval-threshold), TransferExecutor Protocol
                         (StubExecutor/DryRunExecutor/FailingExecutor),
                         TransferLedger, and TransferManager that routes
                         TransferRequest through policy -> executor ->
                         ledger. 18 new tests.

  * fiat_to_crypto -- funnel/fiat_to_crypto.py implements a state-
                      machine on-ramp pipeline (Fiat -> Provider
                      [Coinbase/Kraken/Strike/Gemini/Binance.US] ->
                      CryptoTarget [BTC/ETH/SOL/XRP/USDC/USDT]).
                      OnrampPolicy enforces (source, provider, target)
                      whitelist, per-txn and monthly USD limits, and
                      per-provider minimums. OnrampPipeline drives
                      INITIATED -> FIAT_DEPOSITED -> CONVERTING ->
                      CONVERTED -> WITHDRAWING -> COMPLETE (or FAILED)
                      with timestamped events, injected clock, and an
                      async OnrampExecutor boundary. 44 new tests.

Adds 62 tests (928 -> 990).
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
    sa["eta_engine_tests_passing"] = 990

    by_id = {p["id"]: p for p in state["phases"]}
    p6 = by_id["P6_FUNNEL"]
    p6["progress_pct"] = 100
    p6["status"] = "done"

    ex = _find_task(p6, "exchange_transfer")
    ex["status"] = "done"
    ex["note"] = (
        "funnel/transfer.py enhanced + 18 tests. Existing API "
        "(TransferRequest/TransferResult/execute_transfer/sweep_to_cold) "
        "preserved. Added TransferPolicy (per_txn_limit_usd, "
        "daily_limit_usd rolling-24h, approval_threshold_usd, "
        "whitelist dict[from_bot, set[to_bot]]). Added TransferExecutor "
        "Protocol with StubExecutor/DryRunExecutor/FailingExecutor "
        "implementations. TransferLedger records (EXECUTED|REJECTED|FAILED) "
        "outcomes with reason. TransferManager.execute() pipes "
        "TransferRequest -> policy.check -> executor.execute -> "
        "ledger.append, returning TransferResult and never raising "
        "policy violations upward (they become FAILED results)."
    )

    f2c = _find_task(p6, "fiat_to_crypto")
    f2c["status"] = "done"
    f2c["note"] = (
        "funnel/fiat_to_crypto.py + 44 tests. State-machine on-ramp: "
        "OnrampRequest (fiat_amount_usd>0, source, provider, target, "
        "venue_address>=4) -> OnrampState driven through stages "
        "INITIATED -> FIAT_DEPOSITED -> CONVERTING -> CONVERTED -> "
        "WITHDRAWING -> COMPLETE (or FAILED). OnrampPolicy enforces "
        "(FiatSource, OnrampProvider, CryptoTarget) whitelist + "
        "per-txn + monthly USD limits + per-provider minimums. "
        "OnrampExecutor Protocol (async place_order/withdraw) with "
        "StubOnrampExecutor default prices "
        "(BTC=68k, ETH=3.5k, SOL=180, XRP=0.6, USDC/USDT=1.0) and "
        "configurable slippage_bps + fail_orders/fail_withdrawals "
        "flags. OnrampPipeline: start/confirm_fiat/place_and_record_order/"
        "withdraw_to_venue/run; every transition records an OnrampEvent "
        "with injected clock timestamp; executor failures trap to "
        "FAILED with last_error set."
    )

    # New P6_FUNNEL shared artifact summary
    sa["eta_engine_p6_funnel"] = {
        "timestamp_utc": now,
        "modules": [
            "eta_engine/funnel/transfer.py (enhanced)",
            "eta_engine/funnel/fiat_to_crypto.py (new)",
        ],
        "new_test_files": [
            "tests/test_transfer_manager.py (18 tests)",
            "tests/test_fiat_to_crypto.py (44 tests)",
        ],
        "tests_new": 62,
        "transfer_policy": {
            "per_txn_limit_usd": "bounded per single transfer",
            "daily_limit_usd": "rolling 24h window; first tx at _T0, +25h tx executes (window rolls off)",
            "approval_threshold_usd": "transfers above this require requires_approval=True on the req",
            "whitelist": "empty dict == permissive; non-empty dict[from_bot, set[to_bot]] == strict",
        },
        "onramp_pipeline": {
            "stages": [
                "INITIATED",
                "FIAT_DEPOSITED",
                "CONVERTING",
                "CONVERTED",
                "WITHDRAWING",
                "COMPLETE",
                "FAILED",
            ],
            "providers": [
                "COINBASE",
                "KRAKEN",
                "STRIKE",
                "BINANCE_US",
                "GEMINI",
            ],
            "fiat_sources": [
                "BANK_WIRE",
                "ACH",
                "CARD",
                "ZELLE",
                "CASH_APP",
            ],
            "crypto_targets": [
                "BTC",
                "ETH",
                "SOL",
                "XRP",
                "USDC",
                "USDT",
            ],
        },
        "safety_guards": [
            "OnrampPolicy rejects non-whitelisted (src, provider, target)",
            "OnrampPolicy rejects over per_txn_limit_usd",
            "OnrampPolicy rejects under provider_min_usd",
            "Pipeline.start rejects monthly rollover breach via injected running_monthly_usd callback",
            "OnrampStageError on out-of-order transitions",
            "Executor exceptions caught and routed to FAILED with last_error set; pipeline does not leak exceptions",
        ],
        "notes": (
            "StubOnrampExecutor is deterministic (fixed prices + "
            "slippage_bps + monotonic counters) for reproducible tests. "
            "Pipeline takes an injected clock for deterministic event "
            "timestamps. No real API calls anywhere in this module."
        ),
    }

    # Overall stays at 99 (weighted-phase math unchanged; P6 was already
    # close to done).
    state["overall_progress_pct"] = 99

    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"bumped roadmap_state.json to v0.1.22 at {now}")
    print("  tests_passing: 928 -> 990 (+62)")
    print("  P6_FUNNEL: 90% -> 100% (exchange_transfer + fiat_to_crypto -> done)")
    print("  overall_progress_pct: 99")


if __name__ == "__main__":
    main()
