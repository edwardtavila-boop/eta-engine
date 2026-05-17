from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import mnq_latency_scorecard as scorecard
from eta_engine.scripts import session_scorecard_mnq


def _write_alerts(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _close_row(
    *,
    ts: datetime,
    realized_r: float,
    realized_pnl: float,
    symbol: str = "MNQ",
    entry_fill_age_s: float | None = None,
    entry_fill_latency_source: str = "",
    entry_fill_age_precision: str = "",
    fill_to_adopt_delay_s: float | None = None,
) -> dict:
    extra = {
        "symbol": symbol,
        "realized_pnl": realized_pnl,
    }
    if entry_fill_age_s is not None:
        extra["entry_fill_age_s"] = entry_fill_age_s
    if entry_fill_latency_source:
        extra["entry_fill_latency_source"] = entry_fill_latency_source
    if entry_fill_age_precision:
        extra["entry_fill_age_precision"] = entry_fill_age_precision
    if fill_to_adopt_delay_s is not None:
        extra["fill_to_adopt_delay_s"] = fill_to_adopt_delay_s
    return {
        "ts": ts.isoformat(),
        "bot_id": "mnq_live_alpha",
        "signal_id": f"sig-{int(ts.timestamp())}",
        "realized_r": realized_r,
        "data_source": "live",
        "extra": extra,
    }


def test_build_scorecard_green_with_recent_mnq_closes(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    _write_alerts(
        alerts,
        {"ts": (now - timedelta(hours=1)).isoformat(), "event": "runtime_start"},
    )

    captured: dict[str, object] = {}

    def fake_load_close_records(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return [
            _close_row(ts=now - timedelta(hours=2), realized_r=1.0, realized_pnl=125.0),
            _close_row(ts=now - timedelta(hours=3), realized_r=-0.5, realized_pnl=-62.5),
            _close_row(ts=now - timedelta(hours=3), realized_r=9.0, realized_pnl=500.0, symbol="MES"),
        ]

    monkeypatch.setattr(scorecard.closed_trade_ledger, "load_close_records", fake_load_close_records)

    summary, exit_code = scorecard.build_scorecard(hours=24, alerts_path=alerts, now=now)

    assert exit_code == 0
    assert summary["status"] == "GREEN"
    assert summary["fill_age_exceeded_count"] == 0
    assert summary["close_count"] == 2
    assert summary["realized_pnl"] == 62.5
    assert summary["avg_r"] == 0.25
    assert summary["win_rate"] == 0.5
    assert captured["data_sources"] == scorecard.MODE_TO_DATA_SOURCES["live"]
    assert captured["since_days"] == 1


def test_build_scorecard_yellow_on_one_fill_age_exceeded(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    _write_alerts(
        alerts,
        {
            "ts": (now - timedelta(minutes=30)).isoformat(),
            "event": "warning",
            "headline": "FILL_AGE_EXCEEDED on mnq_live_alpha",
        },
    )
    monkeypatch.setattr(scorecard.closed_trade_ledger, "load_close_records", lambda **_: [])

    summary, exit_code = scorecard.build_scorecard(hours=24, alerts_path=alerts, now=now)

    assert exit_code == 1
    assert summary["status"] == "YELLOW"
    assert summary["fill_age_exceeded_count"] == 1


def test_build_scorecard_red_on_two_fill_age_exceeded(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    _write_alerts(
        alerts,
        {
            "ts": (now - timedelta(minutes=30)).isoformat(),
            "event": "warning",
            "payload": {"reason": "FILL_AGE_EXCEEDED"},
        },
        {
            "ts": (now - timedelta(minutes=10)).isoformat(),
            "event": "warning",
            "headline": "FILL_AGE_EXCEEDED on mnq_live_beta",
        },
    )
    monkeypatch.setattr(scorecard.closed_trade_ledger, "load_close_records", lambda **_: [])

    summary, exit_code = scorecard.build_scorecard(hours=24, alerts_path=alerts, now=now)

    assert exit_code == 2
    assert summary["status"] == "RED"
    assert summary["fill_age_exceeded_count"] == 2


def test_build_scorecard_yellow_on_structured_over_one_bar_latency(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    _write_alerts(alerts, {"ts": now.isoformat(), "event": "runtime_start"})
    monkeypatch.setattr(
        scorecard.closed_trade_ledger,
        "load_close_records",
        lambda **_: [
            _close_row(
                ts=now - timedelta(hours=1),
                realized_r=0.5,
                realized_pnl=62.5,
                entry_fill_age_s=301.0,
                entry_fill_latency_source="broker_router_fill_result",
                entry_fill_age_precision="broker_fill_ts",
                fill_to_adopt_delay_s=20.0,
            ),
        ],
    )

    summary, exit_code = scorecard.build_scorecard(hours=24, alerts_path=alerts, now=now)

    assert exit_code == 1
    assert summary["status"] == "YELLOW"
    assert summary["fill_age_exceeded_count"] == 0
    assert summary["latency_telemetry_close_count"] == 1
    assert summary["over_1_bar_count"] == 1
    assert summary["over_2_bar_count"] == 0
    assert summary["recent_slow_fills"][0]["entry_fill_latency_source"] == "broker_router_fill_result"
    assert summary["recent_slow_fills"][0]["entry_fill_age_precision"] == "broker_fill_ts"
    assert summary["recent_slow_fills"][0]["fill_to_adopt_delay_s"] == 20.0


def test_build_scorecard_red_on_structured_over_two_bar_latency(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    _write_alerts(alerts, {"ts": now.isoformat(), "event": "runtime_start"})
    monkeypatch.setattr(
        scorecard.closed_trade_ledger,
        "load_close_records",
        lambda **_: [
            _close_row(
                ts=now - timedelta(hours=1),
                realized_r=-0.2,
                realized_pnl=-25.0,
                entry_fill_age_s=601.0,
                entry_fill_latency_source="broker_router_fill_result",
            ),
        ],
    )

    summary, exit_code = scorecard.build_scorecard(hours=24, alerts_path=alerts, now=now)

    assert exit_code == 2
    assert summary["status"] == "RED"
    assert summary["fill_age_exceeded_count"] == 0
    assert summary["over_1_bar_count"] == 1
    assert summary["over_2_bar_count"] == 1


def test_build_scorecard_uses_canonical_alert_path_by_default(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    _write_alerts(alerts, {"ts": now.isoformat(), "event": "runtime_start"})
    monkeypatch.setattr(scorecard.workspace_roots, "default_alerts_log_path", lambda: alerts)
    monkeypatch.setattr(scorecard.closed_trade_ledger, "load_close_records", lambda **_: [])

    summary, exit_code = scorecard.build_scorecard(hours=24, now=now)

    assert exit_code == 0
    assert summary["alerts_path"] == str(alerts)


def test_main_accepts_legacy_compatibility_flags(monkeypatch, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    _write_alerts(alerts, {"ts": now.isoformat(), "event": "runtime_start"})
    monkeypatch.setattr(scorecard.workspace_roots, "default_alerts_log_path", lambda: alerts)
    monkeypatch.setattr(scorecard.closed_trade_ledger, "load_close_records", lambda **_: [])
    monkeypatch.setattr(scorecard, "build_scorecard", lambda **_: ({"status": "GREEN"}, 0))

    rc = scorecard.main(
        [
            "--json",
            "--journal",
            "docs/journals/live/mnq_journal.jsonl",
            "--paper-baseline",
            "docs/mnq_v2_trades.json",
        ]
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "GREEN"}


def test_session_scorecard_wrapper_delegates_to_canonical_main() -> None:
    assert session_scorecard_mnq.main is scorecard.main
