"""Tests for anomaly_watcher — proactive loss-streak detector."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")


def _ts(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def test_scan_empty_when_no_trades(tmp_path: Path) -> None:
    """No trades → no anomalies."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    hits = anomaly_watcher.scan(
        trade_closes_path=tmp_path / "missing.jsonl",
        hits_log=tmp_path / "hits.jsonl",
    )
    assert hits == []


def test_scan_detects_3_loss_streak(tmp_path: Path) -> None:
    """3 consecutive losses → loss_streak anomaly fires."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "bleeder", "realized_r": -0.5, "ts": _ts(3)},
            {"bot_id": "bleeder", "realized_r": -0.7, "ts": _ts(2)},
            {"bot_id": "bleeder", "realized_r": -0.4, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    streak_hits = [h for h in hits if h.pattern == "loss_streak"]
    assert len(streak_hits) == 1
    assert streak_hits[0].bot_id == "bleeder"
    assert streak_hits[0].extras["streak"] == 3
    assert streak_hits[0].suggested_skill == "jarvis-anomaly-investigator"


def test_scan_streak_breaks_on_win(tmp_path: Path) -> None:
    """A win in the middle resets the streak counter."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "ok_bot", "realized_r": -0.5, "ts": _ts(4)},
            {"bot_id": "ok_bot", "realized_r": -0.7, "ts": _ts(3)},
            {"bot_id": "ok_bot", "realized_r": 1.0, "ts": _ts(2)},  # WIN breaks streak
            {"bot_id": "ok_bot", "realized_r": -0.4, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    streak_hits = [h for h in hits if h.pattern == "loss_streak"]
    assert streak_hits == []  # most recent is 1 loss after a win, no streak


def test_scan_detects_loss_rate(tmp_path: Path) -> None:
    """5+ losses in last 8 trades → loss_rate anomaly fires."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    # 8 trades, 6 losses + 2 wins
    trades = []
    for i, r in enumerate([-0.5, 1.0, -0.5, -0.5, 1.0, -0.5, -0.5, -0.5]):
        trades.append({"bot_id": "drift_bot", "realized_r": r, "ts": _ts(8 - i)})
    _write_trades(path, trades)
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    rate_hits = [h for h in hits if h.pattern == "loss_rate"]
    assert len(rate_hits) == 1
    assert rate_hits[0].extras["losses_in_window"] == 6


def test_scan_dedups_same_anomaly_within_window(tmp_path: Path) -> None:
    """Second scan with same data → no new hits (dedup)."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "b", "realized_r": -0.5, "ts": _ts(3)},
            {"bot_id": "b", "realized_r": -0.6, "ts": _ts(2)},
            {"bot_id": "b", "realized_r": -0.4, "ts": _ts(1)},
        ],
    )
    hits_log = tmp_path / "hits.jsonl"

    first = anomaly_watcher.scan(trade_closes_path=path, hits_log=hits_log)
    second = anomaly_watcher.scan(trade_closes_path=path, hits_log=hits_log)
    assert len(first) >= 1
    assert second == []


def test_scan_writes_to_hits_log(tmp_path: Path) -> None:
    """Each new hit appends a JSONL line to the log for operator review."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "b", "realized_r": -0.5, "ts": _ts(3)},
            {"bot_id": "b", "realized_r": -0.6, "ts": _ts(2)},
            {"bot_id": "b", "realized_r": -0.4, "ts": _ts(1)},
        ],
    )
    hits_log = tmp_path / "hits.jsonl"
    hits = anomaly_watcher.scan(trade_closes_path=path, hits_log=hits_log)
    assert hits_log.exists()
    lines = hits_log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == len(hits)
    for line in lines:
        rec = json.loads(line)
        assert "pattern" in rec
        assert "key" in rec


def test_recent_hits_filters_by_window(tmp_path: Path) -> None:
    """recent_hits(since_hours=N) only returns entries newer than N hours."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    hits_log = tmp_path / "hits.jsonl"
    now = datetime.now(UTC)
    with hits_log.open("w", encoding="utf-8") as fh:
        for hours_ago in (0.5, 1.0, 50, 100):
            ts = (now - timedelta(hours=hours_ago)).isoformat()
            fh.write(
                json.dumps(
                    {
                        "asof": ts,
                        "pattern": "loss_streak",
                        "key": f"k{hours_ago}",
                        "bot_id": "b",
                        "severity": "warn",
                        "detail": "x",
                        "suggested_skill": "x",
                        "extras": {},
                    }
                )
                + "\n"
            )
    recent = anomaly_watcher.recent_hits(since_hours=24, hits_log=hits_log)
    assert len(recent) == 2  # 0.5h and 1h, not the 50h+ ones


def test_recent_hits_returns_empty_when_log_missing(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    hits = anomaly_watcher.recent_hits(hits_log=tmp_path / "no_log.jsonl")
    assert hits == []


def test_scan_handles_garbage_records(tmp_path: Path) -> None:
    """Non-JSON / malformed lines don't crash the scan."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write('{"bot_id":"good","realized_r":-0.5,"ts":"' + _ts(3) + '"}\n')
        fh.write('{"bot_id":"good","realized_r":-0.5,"ts":"' + _ts(2) + '"}\n')
        fh.write('{"bot_id":"good","realized_r":-0.5,"ts":"' + _ts(1) + '"}\n')
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    assert any(h.pattern == "loss_streak" for h in hits)


def test_loss_streak_severity_escalates_with_count(tmp_path: Path) -> None:
    """5+ consecutive losses → critical, 3-4 → warn."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "severe", "realized_r": -0.5, "ts": _ts(6)},
            {"bot_id": "severe", "realized_r": -0.5, "ts": _ts(5)},
            {"bot_id": "severe", "realized_r": -0.5, "ts": _ts(4)},
            {"bot_id": "severe", "realized_r": -0.5, "ts": _ts(3)},
            {"bot_id": "severe", "realized_r": -0.5, "ts": _ts(2)},
            {"bot_id": "severe", "realized_r": -0.5, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    streak = next(h for h in hits if h.pattern == "loss_streak")
    assert streak.severity == "critical"
    assert streak.extras["streak"] == 6


# ---------------------------------------------------------------------------
# Positive + meta-level detectors (added 2026-05-12)
# ---------------------------------------------------------------------------


def test_scan_detects_5_win_streak(tmp_path: Path) -> None:
    """5 consecutive wins fire win_streak with severity=info."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "winner", "realized_r": 0.8, "ts": _ts(5)},
            {"bot_id": "winner", "realized_r": 1.1, "ts": _ts(4)},
            {"bot_id": "winner", "realized_r": 0.9, "ts": _ts(3)},
            {"bot_id": "winner", "realized_r": 1.4, "ts": _ts(2)},
            {"bot_id": "winner", "realized_r": 0.7, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    wins = [h for h in hits if h.pattern == "win_streak"]
    assert len(wins) == 1
    assert wins[0].bot_id == "winner"
    assert wins[0].severity == "info"
    assert wins[0].extras["streak"] == 5
    assert abs(wins[0].extras["total_r"] - 4.9) < 0.01


def test_win_streak_breaks_on_loss(tmp_path: Path) -> None:
    """A loss in the middle prevents the win_streak from firing."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "winner", "realized_r": 0.8, "ts": _ts(6)},
            {"bot_id": "winner", "realized_r": 1.1, "ts": _ts(5)},
            {"bot_id": "winner", "realized_r": -0.4, "ts": _ts(4)},  # breaker
            {"bot_id": "winner", "realized_r": 0.9, "ts": _ts(3)},
            {"bot_id": "winner", "realized_r": 1.4, "ts": _ts(2)},
            {"bot_id": "winner", "realized_r": 0.7, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    # Only 3 wins after the break — below WIN_STREAK_THRESHOLD=5
    wins = [h for h in hits if h.pattern == "win_streak"]
    assert wins == []


def test_scan_detects_suspicious_win(tmp_path: Path) -> None:
    """Single trade R >= 5.0 fires suspicious_win with severity=warn."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "lottery", "realized_r": 7.3, "ts": _ts(2)},
            {"bot_id": "lottery", "realized_r": 0.4, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    sus = [h for h in hits if h.pattern == "suspicious_win"]
    assert len(sus) == 1
    assert sus[0].bot_id == "lottery"
    assert sus[0].severity == "warn"
    assert sus[0].extras["r"] == 7.3
    assert sus[0].suggested_skill == "jarvis-anomaly-investigator"


def test_suspicious_win_ignores_normal_wins(tmp_path: Path) -> None:
    """+2R or +3R wins are normal — don't fire suspicious_win."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "normal", "realized_r": 2.5, "ts": _ts(2)},
            {"bot_id": "normal", "realized_r": 3.2, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    sus = [h for h in hits if h.pattern == "suspicious_win"]
    assert sus == []


def test_scan_detects_fleet_hot_day(tmp_path: Path) -> None:
    """Today's total >= +3R across fleet -> fleet_hot_day with severity=info."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    today_iso = datetime.now(UTC).isoformat()
    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "realized_r": 1.5, "ts": today_iso},
            {"bot_id": "b", "realized_r": 2.0, "ts": today_iso},
            {"bot_id": "c", "realized_r": 0.8, "ts": today_iso},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    hot = [h for h in hits if h.pattern == "fleet_hot_day"]
    assert len(hot) == 1
    assert hot[0].severity == "info"
    assert hot[0].bot_id == "__fleet__"
    assert abs(hot[0].extras["total_r"] - 4.3) < 0.01
    assert hot[0].extras["n_trades"] == 3


def test_scan_detects_fleet_drawdown(tmp_path: Path) -> None:
    """Today's total <= -3R -> fleet_drawdown with severity=critical."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    today_iso = datetime.now(UTC).isoformat()
    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "a", "realized_r": -1.5, "ts": today_iso},
            {"bot_id": "b", "realized_r": -2.0, "ts": today_iso},
            {"bot_id": "c", "realized_r": -0.8, "ts": today_iso},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    dd = [h for h in hits if h.pattern == "fleet_drawdown"]
    assert len(dd) == 1
    assert dd[0].severity == "critical"
    assert dd[0].bot_id == "__fleet__"
    assert dd[0].suggested_skill == "jarvis-drawdown-response"


def test_fleet_total_ignores_old_trades(tmp_path: Path) -> None:
    """Trades from yesterday's date don't contribute to today's fleet total."""
    from datetime import UTC, datetime, timedelta

    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    # Build a "yesterday" timestamp string
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    today = datetime.now(UTC).isoformat()

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            # +5R yesterday — should NOT count
            {"bot_id": "a", "realized_r": 5.0, "ts": yesterday},
            # +0.5R today — well below +3R hot-day threshold
            {"bot_id": "b", "realized_r": 0.5, "ts": today},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    hot = [h for h in hits if h.pattern == "fleet_hot_day"]
    assert hot == []


def test_scan_detects_stale_bot(tmp_path: Path) -> None:
    """Bot whose most-recent trade is >48h old fires stale_bot."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            # 72h ago — well past STALE_BOT_HOURS=48
            {"bot_id": "ghost", "realized_r": -0.3, "ts": _ts(72)},
            {"bot_id": "ghost", "realized_r": 0.5, "ts": _ts(70)},
            # active bot with recent activity
            {"bot_id": "active", "realized_r": 0.2, "ts": _ts(2)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    stale = [h for h in hits if h.pattern == "stale_bot"]
    assert len(stale) == 1
    assert stale[0].bot_id == "ghost"
    assert stale[0].severity == "warn"
    assert stale[0].extras["hours_silent"] >= 48


def test_stale_bot_ignores_active_bots(tmp_path: Path) -> None:
    """Bots with trades in the last 48h are not stale."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "active", "realized_r": -0.3, "ts": _ts(40)},
            {"bot_id": "active", "realized_r": 0.5, "ts": _ts(2)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    stale = [h for h in hits if h.pattern == "stale_bot"]
    assert stale == []


def test_scan_detects_prop_firm_approaching_daily_loss(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """An account at 80% of daily loss fires prop_firm_daily_loss_approaching."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    warn_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.AccountState(
            account_id="blusky-50K-launch",
            starting_balance=50_000.0,
            current_balance=48_800.0,
            peak_balance=50_000.0,
            day_pnl_usd=-1_200.0,
            today_date="2026-05-12",
            n_trades_today=4,
            open_contracts=0,
        ),
        daily_loss_remaining=300.0,
        daily_loss_pct_used=0.80,
        trailing_dd_remaining=800.0,
        profit_to_target=3_000.0,
        pct_to_target=0.0,
        severity="warn",
        blockers=[],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [warn_snap])

    # Empty trade_closes (we just want the prop-firm detector to fire)
    hits = anomaly_watcher.scan(
        trade_closes_path=tmp_path / "no_trades.jsonl",
        hits_log=tmp_path / "hits.jsonl",
    )
    pf_hits = [h for h in hits if h.pattern == "prop_firm_daily_loss_approaching"]
    assert len(pf_hits) == 1
    assert pf_hits[0].bot_id == "blusky-50K-launch"
    assert pf_hits[0].severity == "warn"
    assert pf_hits[0].suggested_skill == "jarvis-drawdown-response"


def test_scan_detects_prop_firm_approaching_at_critical_severity(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """90%+ daily loss → critical severity."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    crit_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.AccountState(
            account_id="blusky-50K-launch",
            starting_balance=50_000.0,
            current_balance=48_500.0,
            peak_balance=50_000.0,
            day_pnl_usd=-1_425.0,
            today_date="2026-05-12",
            n_trades_today=5,
            open_contracts=0,
        ),
        daily_loss_remaining=75.0,
        daily_loss_pct_used=0.95,
        trailing_dd_remaining=500.0,
        profit_to_target=3_000.0,
        pct_to_target=0.0,
        severity="critical",
        blockers=["daily_loss_95%"],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [crit_snap])

    hits = anomaly_watcher.scan(
        trade_closes_path=tmp_path / "no_trades.jsonl",
        hits_log=tmp_path / "hits.jsonl",
    )
    pf_hits = [h for h in hits if h.pattern == "prop_firm_daily_loss_approaching"]
    assert len(pf_hits) == 1
    assert pf_hits[0].severity == "critical"


def test_scan_prop_firm_silent_when_far_from_limits(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Below 75% used → no fire (avoid false alarms on small days)."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher
    from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

    ok_snap = g.AccountSnapshot(
        rules=g.REGISTRY["blusky-50K-launch"],
        state=g.AccountState(
            account_id="blusky-50K-launch",
            starting_balance=50_000.0,
            current_balance=50_200.0,
            peak_balance=50_200.0,
            day_pnl_usd=200.0,
            today_date="2026-05-12",
            n_trades_today=2,
            open_contracts=0,
        ),
        daily_loss_remaining=1_500.0,
        daily_loss_pct_used=0.0,
        trailing_dd_remaining=2_000.0,
        profit_to_target=2_800.0,
        pct_to_target=0.0667,
        severity="ok",
        blockers=[],
    )
    monkeypatch.setattr(g, "aggregate_status", lambda **kw: [ok_snap])

    hits = anomaly_watcher.scan(
        trade_closes_path=tmp_path / "no_trades.jsonl",
        hits_log=tmp_path / "hits.jsonl",
    )
    pf_hits = [h for h in hits if h.pattern.startswith("prop_firm")]
    assert pf_hits == []


def test_one_broken_detector_does_not_block_others(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """If a single detector raises, the rest of the pass continues."""
    from eta_engine.brain.jarvis_v3 import anomaly_watcher

    def boom(_: Any) -> list:  # noqa: ANN401
        raise RuntimeError("simulated detector crash")

    # Break the win-streak detector
    monkeypatch.setattr(anomaly_watcher, "_detect_win_streak", boom)

    path = tmp_path / "tc.jsonl"
    _write_trades(
        path,
        [
            {"bot_id": "b", "realized_r": -0.5, "ts": _ts(3)},
            {"bot_id": "b", "realized_r": -0.6, "ts": _ts(2)},
            {"bot_id": "b", "realized_r": -0.4, "ts": _ts(1)},
        ],
    )
    hits = anomaly_watcher.scan(
        trade_closes_path=path,
        hits_log=tmp_path / "hits.jsonl",
    )
    # loss_streak still fires despite win_streak being broken
    assert any(h.pattern == "loss_streak" for h in hits)
