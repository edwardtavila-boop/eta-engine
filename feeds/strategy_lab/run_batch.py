"""Strategy Lab — headless CLI runner for automated batch testing.

Runs a hardcoded list of MNQ + BTC EMA-cross specs through the
``WalkForwardEngine`` and writes one report per spec under
``reports/lab_reports/``. Quick way to smoke-test the lab end-to-end.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eta_engine.feeds.strategy_lab.engine import (  # noqa: E402  (sys.path side-effect above)
    WalkForwardEngine,
    save_lab_report,
)

SPECS: list[dict] = [
    {
        "id": "ema_cross_v1",
        "symbol": "MNQ",
        "entry": "ema_cross",
        "atr_period": 14,
        "stop_loss": "atr*1.5",
        "take_profit": "atr*3.0",
    },
    {
        "id": "ema_cross_v2",
        "symbol": "MNQ",
        "entry": "ema_cross",
        "atr_period": 21,
        "stop_loss": "atr*2.0",
        "take_profit": "atr*4.0",
    },
    {
        "id": "ema_cross_v3",
        "symbol": "BTC",
        "entry": "ema_cross",
        "atr_period": 14,
        "stop_loss": "atr*1.5",
        "take_profit": "atr*3.0",
    },
]

BAR_DIR = Path("C:/EvolutionaryTradingAlgo/data")
OUT_DIR = Path("C:/EvolutionaryTradingAlgo/reports/lab_reports")


def main() -> None:
    engine = WalkForwardEngine(bar_dir=BAR_DIR)
    results: list[dict] = []
    for spec in SPECS:
        result = engine.run(spec, symbol=spec["symbol"])
        save_lab_report(result, OUT_DIR)
        results.append(
            {
                "id": spec["id"],
                "passed": result.passed,
                "trades": result.total_trades,
                "sharpe": result.sharpe,
            }
        )
        verdict = "PASS" if result.passed else "FAIL"
        print(
            f"{spec['id']}: {verdict} ({result.total_trades}t, Sharpe={result.sharpe})",
        )

    passed = sum(1 for r in results if r["passed"])
    print(f"\nBatch complete: {passed}/{len(results)} passed")


if __name__ == "__main__":
    main()
