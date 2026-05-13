"""Tests for the wave-19 diamond demotion gate."""
# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _trade(bot_id: str, r: float, days_ago: float = 0,
           idx: int = 0, base_dt: datetime | None = None) -> dict:
    """Build a synthetic trade-close record."""
    if base_dt is None:
        base_dt = datetime.now(UTC)
    ts = (base_dt - timedelta(days=days_ago)).isoformat()
    return {
        "bot_id": bot_id,
        "signal_id": f"{bot_id}_{idx}",
        "ts": ts,
        "realized_r": r,
    }


# ────────────────────────────────────────────────────────────────────
# Hard demotion criteria
# ────────────────────────────────────────────────────────────────────


def test_D1_temporal_decay_demotes_silent_bot() -> None:
    """A bot with 0 active days in last 14 days = DEMOTE_CANDIDATE."""
    from eta_engine.scripts import diamond_demotion_gate as dg

    # All trades 30+ days ago — no recent activity
    trades = [_trade("silent", r=0.5, days_ago=30 + i, idx=i) for i in range(20)]
    sc = dg._score_bot("silent", trades)
    assert sc.verdict == "DEMOTE_CANDIDATE"
    assert any("D1_TEMPORAL_DECAY" in f for f in sc.hard_failures)


def test_D2_r_bleed_demotes_decayed_strategy() -> None:
    """A bot whose last 50 trades net < -5R = DEMOTE_CANDIDATE."""
    from eta_engine.scripts import diamond_demotion_gate as dg

    # 50 recent losing trades averaging -0.3R each → -15R cumulative
    trades = [
        _trade("bleeder", r=-0.3, days_ago=i * 0.1, idx=i)
        for i in range(50)
    ]
    sc = dg._score_bot("bleeder", trades)
    assert sc.verdict == "DEMOTE_CANDIDATE"
    assert any("D2_R_BLEED" in f for f in sc.hard_failures)


# ────────────────────────────────────────────────────────────────────
# Soft watch criteria
# ────────────────────────────────────────────────────────────────────


def test_W1_low_sample_growth_warns_when_few_recent_trades() -> None:
    """<10 new trades in last 14 days = WATCH (insufficient activity)."""
    from eta_engine.scripts import diamond_demotion_gate as dg

    # 5 recent trades + 50 older ones (R-bleed safe)
    trades = []
    for i in range(50):
        trades.append(_trade("slow", r=0.5, days_ago=20 + i, idx=i))
    for i in range(5):
        trades.append(_trade("slow", r=0.5, days_ago=i, idx=100 + i))
    sc = dg._score_bot("slow", trades)
    assert sc.verdict == "WATCH"
    assert any("W1_LOW_SAMPLE_GROWTH" in f for f in sc.soft_failures)


def test_W2_r_drift_warns_when_recent_avg_r_eroded() -> None:
    """Recent avg_R < +0.05 but cum_R still above floor = WATCH."""
    from eta_engine.scripts import diamond_demotion_gate as dg

    # 50 recent trades averaging +0.02R (above -5R floor, below +0.05 drift)
    trades = [
        _trade("drift", r=0.02, days_ago=i * 0.1, idx=i)
        for i in range(50)
    ]
    sc = dg._score_bot("drift", trades)
    assert sc.verdict == "WATCH"
    assert any("W2_R_DRIFT" in f for f in sc.soft_failures)


# ────────────────────────────────────────────────────────────────────
# KEEP verdict
# ────────────────────────────────────────────────────────────────────


def test_KEEP_when_all_criteria_pass() -> None:
    """Healthy diamond: active recently, positive recent R, ≥10 new trades."""
    from eta_engine.scripts import diamond_demotion_gate as dg

    trades = [
        _trade("healthy", r=0.5 if i % 2 == 0 else -0.2,
               days_ago=i * 0.2, idx=i)
        for i in range(60)
    ]
    sc = dg._score_bot("healthy", trades)
    assert sc.verdict == "KEEP"
    assert sc.hard_failures == []
    assert sc.soft_failures == []


# ────────────────────────────────────────────────────────────────────
# Hard wins over soft
# ────────────────────────────────────────────────────────────────────


def test_hard_failure_overrides_soft() -> None:
    """If a bot fails BOTH hard and soft, the verdict is DEMOTE_CANDIDATE,
    not WATCH (hard criteria are more severe)."""
    from eta_engine.scripts import diamond_demotion_gate as dg

    # Bleeder (D2 hard) + low-growth (W1 soft simultaneously)
    trades = [
        _trade("doubly_bad", r=-0.5, days_ago=20 + i, idx=i)
        for i in range(15)
    ]
    sc = dg._score_bot("doubly_bad", trades)
    assert sc.verdict == "DEMOTE_CANDIDATE"


# ────────────────────────────────────────────────────────────────────
# Snapshot file write
# ────────────────────────────────────────────────────────────────────


def test_run_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    """run() reads canonical ledger paths and persists summary."""
    from eta_engine.scripts import diamond_demotion_gate as dg

    can_path = tmp_path / "canonical.jsonl"
    leg_path = tmp_path / "legacy.jsonl"
    can_path.write_text("\n".join(json.dumps(r) for r in [
        _trade("m2k_sweep_reclaim", r=0.5 if i % 2 == 0 else -0.2,
                days_ago=i * 0.1, idx=i)
        for i in range(60)
    ]), encoding="utf-8")
    leg_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(dg, "TRADE_CLOSES_CANONICAL", can_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(dg, "TRADE_CLOSES_LEGACY", leg_path)  # type: ignore[attr-defined]
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(dg, "OUT_LATEST", out_path)  # type: ignore[attr-defined]

    summary = dg.run()
    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in on_disk
    assert "scorecards" in on_disk
    assert "verdict_counts" in on_disk
    assert on_disk["n_diamonds"] == summary["n_diamonds"]
