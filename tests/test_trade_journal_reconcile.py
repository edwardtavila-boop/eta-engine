from __future__ import annotations

from eta_engine.scripts import workspace_roots
from eta_engine.scripts import _trade_journal_reconcile as mod


def test_trade_journal_reconcile_defaults_to_canonical_btc_live_journal() -> None:
    assert mod.DEFAULT_BTC == workspace_roots.ETA_BTC_LIVE_DECISIONS_PATH


def test_btc_freshness_ignores_dev_runtime_churn_without_live_starts() -> None:
    level, detail = mod._check_btc_journal_freshness(
        btc_records=[],
        alerts_window=[
            {"event": "runtime_start", "ts": 1_800_000_000.0, "payload": {"live": False}},
            {"event": "runtime_start", "ts": 1_800_000_001.0, "payload": {}},
        ],
        stale_hours=36.0,
        now_ts=1_800_003_600.0,
    )

    assert level == "GREEN"
    assert "paper/dev=2" in detail


def test_btc_freshness_warns_when_live_runtime_has_no_btc_journal() -> None:
    level, detail = mod._check_btc_journal_freshness(
        btc_records=[],
        alerts_window=[
            {"event": "runtime_start", "ts": 1_800_000_000.0, "payload": {"live": True}},
        ],
        stale_hours=36.0,
        now_ts=1_800_003_600.0,
    )

    assert level == "YELLOW"
    assert "LIVE runtime active" in detail
