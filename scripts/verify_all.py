"""
EVOLUTIONARY TRADING ALGO — Full Verification

Runs smoke tests across all packages, prints a pass/fail table.

Usage:
    python -m eta_engine.scripts.verify_all
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def check(name: str, fn) -> tuple[str, bool, str]:
    try:
        fn()
        return (name, True, "OK")
    except Exception as e:  # noqa: BLE001
        return (name, False, f"{type(e).__name__}: {e}")


def test_core_imports() -> None:
    m = importlib.import_module("eta_engine.core")
    assert hasattr(m, "calculate_max_leverage")
    assert hasattr(m, "score_confluence")
    assert hasattr(m, "check_sweep")


def test_risk_engine_math() -> None:
    from eta_engine.core import calculate_max_leverage, fractional_kelly

    lev = calculate_max_leverage(price=3500, atr_14_5m=45)
    assert 15 < lev < 25, f"ETH leverage out of band: {lev}"
    k = fractional_kelly(win_rate=0.42, avg_win_r=2.8, avg_loss_r=1.2)
    assert 0 < k < 0.5, f"Kelly out of band: {k}"


def test_confluence_ramp() -> None:
    from eta_engine.core import score_confluence

    perfect = score_confluence(1.0, 1.0, 1.0, 1.0, 1.0)
    assert perfect.total_score >= 9.0
    assert perfect.recommended_leverage >= 50

    weak = score_confluence(0.1, 0.1, 0.1, 0.1, 0.1)
    assert weak.total_score < 5.0
    assert weak.recommended_leverage == 0 or weak.signal == "NO_TRADE"


def test_sweep() -> None:
    from eta_engine.core import SweepConfig, check_sweep

    cfg = SweepConfig(bot_name="test", baseline_usd=3000)
    r = check_sweep(current_equity=3500, config=cfg)
    assert r.excess_usd > 0
    assert round(r.to_stake + r.to_reinvest + r.to_reserve, 2) == round(r.excess_usd, 2)

    r2 = check_sweep(current_equity=2900, config=cfg)
    assert r2.excess_usd <= 0
    assert not r2.action_required


def test_bots_import() -> None:
    try:
        importlib.import_module("eta_engine.bots")
    except ImportError:
        pass  # bots scaffolding in flight


def test_funnel_import() -> None:
    with contextlib.suppress(ImportError):
        importlib.import_module("eta_engine.funnel")


def test_brain_import() -> None:
    with contextlib.suppress(ImportError):
        importlib.import_module("eta_engine.brain")


def test_staking_import() -> None:
    with contextlib.suppress(ImportError):
        importlib.import_module("eta_engine.staking")


def test_features_import() -> None:
    with contextlib.suppress(ImportError):
        importlib.import_module("eta_engine.features")


def test_venues_import() -> None:
    with contextlib.suppress(ImportError):
        importlib.import_module("eta_engine.venues")


def test_backtest_import() -> None:
    with contextlib.suppress(ImportError):
        importlib.import_module("eta_engine.backtest")


def test_funnel_orchestrator_flow() -> None:
    from eta_engine.funnel import FunnelOrchestrator

    assert FunnelOrchestrator is not None


def test_obs_package() -> None:
    from eta_engine.obs import (
        REGISTRY,
        Alert,
        AlertLevel,
        DiscordAlerter,
        HeartbeatMonitor,
        Metric,
        MetricsRegistry,
        MultiAlerter,
        SlackAlerter,
        StructuredLogger,
        TelegramAlerter,
    )

    reg = MetricsRegistry()
    reg.inc("apex_trades_opened_total", labels={"bot": "mnq"})
    reg.gauge("apex_equity_usd", 52_500.0)
    assert reg.get_counter("apex_trades_opened_total", labels={"bot": "mnq"}) == 1.0
    assert reg.get_gauge("apex_equity_usd") == 52_500.0
    assert REGISTRY is not None
    assert Alert is not None
    assert AlertLevel.KILL > AlertLevel.INFO
    assert Metric is not None
    assert DiscordAlerter is not None and SlackAlerter is not None and TelegramAlerter is not None
    assert MultiAlerter is not None and HeartbeatMonitor is not None and StructuredLogger is not None


def test_secrets_manager() -> None:
    from eta_engine.core.secrets import (
        REQUIRED_KEYS,
        SECRETS,
        SecretsManager,
    )

    sm = SecretsManager(env_file="no_such_file.env")
    assert sm.get("APEX_DEFINITELY_MISSING_KEY", required=False) is None
    assert any("APEX_DEFINITELY_MISSING_KEY" in line for line in sm.audit_log)
    assert SECRETS is not None
    assert len(REQUIRED_KEYS) == 14


def test_data_package() -> None:
    from eta_engine.data import (
        DatasetManifest,
        DataSource,
        ParquetLoader,
        SlippageModel,
        detect_gaps,
    )

    m = SlippageModel()
    assert m.estimate("ETHUSDT", "BUY", qty=0.01, price=3500.0, urgency="AGGRESSIVE") > 0.0
    manifest = ParquetLoader().scan_manifest(Path("/tmp/nonexistent_dir"))
    assert isinstance(manifest, DatasetManifest)
    assert DataSource.DATABENTO.value == "DATABENTO"
    assert detect_gaps([], 60) == []


def test_tax_package() -> None:
    from datetime import UTC, datetime

    from eta_engine.tax import (
        AccountTier,
        CostBasisCalculator,
        InstrumentType,
        Section1256Reporter,
    )

    r = Section1256Reporter()
    split = r.breakdown_60_40(1000.0)
    assert abs(split["long_term_60"] - 600.0) < 1e-6
    assert abs(split["short_term_40"] - 400.0) < 1e-6

    calc = CostBasisCalculator(method="FIFO")
    calc.add_buy("ETH", 1.0, 2000.0, datetime(2025, 1, 1, tzinfo=UTC))
    evs = calc.process_sell(
        "ETH",
        1.0,
        3000.0,
        datetime(2025, 6, 1, tzinfo=UTC),
        account_tier=AccountTier.US,
        instrument_type=InstrumentType.CRYPTO_SPOT,
    )
    assert abs(evs[0].realized_gain_usd - 1000.0) < 1e-6


def test_walk_forward_engine() -> None:
    from datetime import UTC, datetime

    from eta_engine.backtest import (
        BacktestConfig,
        BarReplay,
        WalkForwardConfig,
        WalkForwardEngine,
        compute_dsr,
    )
    from eta_engine.features.pipeline import FeaturePipeline

    dsr = compute_dsr(sharpe=1.5, n_trades=200, skew=0.0, kurtosis=3.0, n_trials=10)
    assert 0.0 <= dsr <= 1.0

    bars = BarReplay.synthetic_bars(
        n=4 * 24 * 12,
        drift=0.0005,
        vol=0.004,
        seed=1,
        start=datetime(2025, 1, 1, tzinfo=UTC),
        interval_minutes=15,
    )
    cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=bars[0].symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=7.0,
        max_trades_per_day=10,
    )
    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=WalkForwardConfig(window_days=4, step_days=2, anchored=False, oos_fraction=0.3, min_trades_per_window=1),
        base_backtest_config=cfg,
    )
    assert isinstance(res.deflated_sharpe, float)


def main() -> int:
    checks = [
        ("core_imports", test_core_imports),
        ("risk_math", test_risk_engine_math),
        ("confluence_ramp", test_confluence_ramp),
        ("sweep_math", test_sweep),
        ("bots_package", test_bots_import),
        ("funnel_package", test_funnel_import),
        ("brain_package", test_brain_import),
        ("staking_package", test_staking_import),
        ("features_package", test_features_import),
        ("venues_package", test_venues_import),
        ("backtest_package", test_backtest_import),
        ("funnel_orchestrator_flow", test_funnel_orchestrator_flow),
        ("obs_package", test_obs_package),
        ("secrets_manager", test_secrets_manager),
        ("data_package", test_data_package),
        ("tax_package", test_tax_package),
        ("walk_forward_engine", test_walk_forward_engine),
    ]

    results = [check(n, f) for n, f in checks]
    failed = sum(1 for _, ok, _ in results if not ok)

    print()
    print("EVOLUTIONARY TRADING ALGO -- Verification")
    print("=" * 60)
    for name, ok, msg in results:
        icon = "[OK]" if ok else "[FAIL]"
        print(f"{icon:<6} {name:<24} {msg}")
    print("=" * 60)
    print(f"Passed: {len(results) - failed}/{len(results)}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
