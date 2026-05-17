"""Tests for replaying shadow signals into counterfactual outcomes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from eta_engine.scripts import shadow_signal_outcome_audit as audit
from eta_engine.scripts import workspace_roots


def _bar(
    ts: datetime,
    *,
    open_: float = 100.0,
    high: float = 100.0,
    low: float = 100.0,
    close: float = 100.0,
) -> SimpleNamespace:
    return SimpleNamespace(timestamp=ts, open=open_, high=high, low=low, close=close, volume=1.0)


def _signal(
    *,
    side: str = "BUY",
    symbol: str = "NQ1",
    bot_id: str = "volume_profile_nq",
    ts: datetime,
    entry_price: float = 100.0,
    risk_price: float = 1.0,
) -> dict[str, object]:
    return {
        "ts": ts.isoformat(),
        "bot_id": bot_id,
        "signal_id": f"{bot_id}_{side}_{ts.timestamp()}",
        "symbol": symbol,
        "side": side,
        "route_target": "paper",
        "route_reason": "lifecycle_eval_paper",
        "lifecycle": "EVAL_PAPER",
        "extra": {
            "bar_ts": ts.isoformat(),
            "entry_price": entry_price,
            "risk_price": risk_price,
        },
    }


def test_shadow_signal_without_planned_context_uses_bar_derived_entry_and_risk() -> None:
    ts = datetime(2026, 5, 15, 13, 0, tzinfo=UTC)
    signal = _signal(ts=ts)
    signal["extra"] = {"bar_ts": ts.isoformat()}

    report = audit.build_report(
        shadow_signals=[signal],
        bars_by_symbol={
            "NQ1": [
                _bar(ts, high=100.25, low=99.75, close=100.0),
                _bar(ts + timedelta(minutes=5), high=101.1, low=100.1, close=101.0),
            ],
        },
        generated_at=ts,
    )

    outcome = report["outcomes"][0]
    stats = report["per_bot"]["volume_profile_nq"]

    assert outcome["status"] == "EVALUATED"
    assert outcome["exit_reason"] == "TARGET"
    assert outcome["realized_r"] == 1.0
    assert stats["evaluated_count"] == 1
    assert stats["missing_context"] == 0
    assert stats["verdict"] == "SMALL_SAMPLE_COUNTERFACTUAL"


def test_shadow_signal_uses_logged_stop_and_target_prices() -> None:
    ts = datetime(2026, 5, 15, 13, 30, tzinfo=UTC)
    signal = _signal(ts=ts, entry_price=100.0, risk_price=0.0)
    signal["extra"] = {
        "bar_ts": ts.isoformat(),
        "entry_price": 100.0,
        "stop_price": 99.0,
        "target_price": 102.0,
    }

    report = audit.build_report(
        shadow_signals=[signal],
        bars_by_symbol={
            "NQ1": [
                _bar(ts, high=100.25, low=99.75, close=100.0),
                _bar(ts + timedelta(minutes=5), high=102.2, low=100.5, close=102.0),
            ],
        },
        generated_at=ts,
    )

    outcome = report["outcomes"][0]

    assert outcome["exit_reason"] == "TARGET"
    assert outcome["stop_price"] == 99.0
    assert outcome["target_price"] == 102.0
    assert outcome["risk_price"] == 1.0
    assert outcome["realized_r"] == 2.0


def test_buy_shadow_signal_replays_to_target_without_claiming_broker_proof() -> None:
    ts = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)

    report = audit.build_report(
        shadow_signals=[_signal(ts=ts)],
        bars_by_symbol={
            "NQ1": [
                _bar(ts, high=100.25, low=99.75, close=100.0),
                _bar(ts + timedelta(minutes=5), high=101.1, low=100.1, close=101.0),
            ],
        },
        generated_at=ts,
        lookahead_bars=2,
    )

    outcome = report["outcomes"][0]
    stats = report["per_bot"]["volume_profile_nq"]

    assert outcome["exit_reason"] == "TARGET"
    assert outcome["realized_r"] == 1.0
    assert outcome["broker_backed"] is False
    assert outcome["promotion_proof"] is False
    assert stats["evaluated_count"] == 1
    assert stats["wins"] == 1
    assert stats["total_r"] == 1.0
    assert stats["latest_signal_ts"] == ts.isoformat()
    assert stats["latest_evaluated_ts"] == (ts + timedelta(minutes=5)).isoformat()
    assert stats["promotion_proof"] is False
    assert "not broker-backed" in report["summary"]["truth_note"]


def test_sell_shadow_signal_replays_to_stop() -> None:
    ts = datetime(2026, 5, 15, 15, 0, tzinfo=UTC)

    report = audit.build_report(
        shadow_signals=[_signal(ts=ts, side="SELL")],
        bars_by_symbol={
            "NQ1": [
                _bar(ts, high=100.1, low=99.8, close=100.0),
                _bar(ts + timedelta(minutes=5), high=101.2, low=99.5, close=100.8),
            ],
        },
        generated_at=ts,
        lookahead_bars=2,
    )

    outcome = report["outcomes"][0]
    stats = report["per_bot"]["volume_profile_nq"]

    assert outcome["exit_reason"] == "STOP"
    assert outcome["realized_r"] == -1.0
    assert stats["losses"] == 1
    assert stats["profit_factor"] == 0.0


def test_missing_bars_are_counted_without_evaluating_the_signal() -> None:
    ts = datetime(2026, 5, 15, 16, 0, tzinfo=UTC)

    report = audit.build_report(
        shadow_signals=[_signal(ts=ts, symbol="MISSING")],
        bars_by_symbol={},
        generated_at=ts,
    )

    stats = report["per_bot"]["volume_profile_nq"]

    assert report["summary"]["status"] == "NO_EVALUATED_OUTCOMES"
    assert stats["shadow_signal_count"] == 1
    assert stats["evaluated_count"] == 0
    assert stats["missing_bars"] == 1
    assert stats["missing_bar_datasets"] == 1
    assert stats["no_bar_after_signal"] == 0
    assert stats["latest_bar_coverage_end_ts"] == ""
    assert stats["verdict"] == "NO_EVALUATED_SIGNALS"


def test_shadow_signal_after_replay_coverage_tracks_stale_bar_gap() -> None:
    ts = datetime(2026, 5, 15, 16, 0, tzinfo=UTC)
    coverage_end = ts - timedelta(minutes=5)

    report = audit.build_report(
        shadow_signals=[_signal(ts=ts)],
        bars_by_symbol={
            "NQ1": [
                _bar(coverage_end - timedelta(minutes=5), close=99.5),
                _bar(coverage_end, close=100.0),
            ],
        },
        generated_at=ts,
    )

    outcome = report["outcomes"][0]
    stats = report["per_bot"]["volume_profile_nq"]

    assert outcome["status"] == "NO_BAR_AFTER_SIGNAL"
    assert outcome["bar_coverage_end_ts"] == coverage_end.isoformat()
    assert stats["missing_bars"] == 1
    assert stats["missing_bar_datasets"] == 0
    assert stats["no_bar_after_signal"] == 1
    assert stats["latest_bar_coverage_end_ts"] == coverage_end.isoformat()


def test_positive_counterfactual_sample_stays_explicitly_non_broker_backed() -> None:
    ts = datetime(2026, 5, 15, 17, 0, tzinfo=UTC)
    signals = [_signal(ts=ts + timedelta(minutes=5 * idx), entry_price=100.0) for idx in range(30)]
    bars = []
    for idx in range(31):
        bar_ts = ts + timedelta(minutes=5 * idx)
        bars.append(_bar(bar_ts, high=101.2, low=99.9, close=101.0))

    report = audit.build_report(
        shadow_signals=signals,
        bars_by_symbol={"NQ1": bars},
        generated_at=ts,
        lookahead_bars=1,
    )

    stats = report["per_bot"]["volume_profile_nq"]

    assert report["summary"]["status"] == "COUNTERFACTUAL_EDGE_SEEN"
    assert stats["evaluated_count"] == 30
    assert stats["verdict"] == "POSITIVE_COUNTERFACTUAL_EDGE"
    assert stats["broker_backed"] is False
    assert stats["promotion_proof"] is False


def test_filtered_shadow_outcome_probes_do_not_clobber_canonical_latest() -> None:
    assert (
        audit._output_path_for_filters(audit.DEFAULT_OUT, bot="volume_profile_nq", symbol=None).name
        == "shadow_signal_outcomes_filtered_latest.json"
    )
    assert (
        audit._output_path_for_filters(audit.DEFAULT_OUT, bot=None, symbol="NQ1").name
        == "shadow_signal_outcomes_filtered_latest.json"
    )
    assert audit._output_path_for_filters(audit.DEFAULT_OUT, bot=None, symbol=None) == audit.DEFAULT_OUT

    explicit = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\custom_shadow_outcomes.json")
    assert audit._output_path_for_filters(explicit, bot="volume_profile_nq", symbol="NQ1") == explicit


def test_cli_rejects_output_path_outside_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "shadow_signal_outcomes_latest.json"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        audit,
        "_current_shadow_signals",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("shadow signals should not load")),
    )

    with pytest.raises(SystemExit) as exc:
        audit.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2
