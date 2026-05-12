"""Tests for diamond_sizing_audit — codified wave-8 sizing forensic."""
# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# ────────────────────────────────────────────────────────────────────
# _classify_sizing — the verdict bands
# ────────────────────────────────────────────────────────────────────


def test_classify_sizing_breached_when_one_stopout_exceeds_floor() -> None:
    """A bot whose worst-trade $/R EXCEEDS the USD floor is SIZING_BREACHED
    — a single full-R stopout already trips the watchdog."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    verdict, ratio = sa._classify_sizing(
        usd_per_r_max_abs=250.0, threshold_usd=-200.0,
    )
    assert verdict == "SIZING_BREACHED"
    assert ratio == 0.8


def test_classify_sizing_fragile_when_under_two_stopouts() -> None:
    """1 <= n_stopouts < 2: SIZING_FRAGILE."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    verdict, ratio = sa._classify_sizing(
        usd_per_r_max_abs=150.0, threshold_usd=-200.0,
    )
    assert verdict == "SIZING_FRAGILE"
    assert ratio is not None
    assert 1.0 <= ratio < 2.0


def test_classify_sizing_tight_when_under_four_stopouts() -> None:
    """2 <= n_stopouts < 4: SIZING_TIGHT."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    verdict, ratio = sa._classify_sizing(
        usd_per_r_max_abs=80.0, threshold_usd=-200.0,
    )
    assert verdict == "SIZING_TIGHT"
    assert ratio is not None
    assert 2.0 <= ratio < 4.0


def test_classify_sizing_ok_when_at_least_four_stopouts() -> None:
    """n_stopouts >= 4: SIZING_OK (operator has room to evaluate edge)."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    verdict, ratio = sa._classify_sizing(
        usd_per_r_max_abs=10.0, threshold_usd=-200.0,
    )
    assert verdict == "SIZING_OK"
    assert ratio is not None
    assert ratio >= 4.0


def test_classify_sizing_insufficient_data_when_missing_threshold() -> None:
    from eta_engine.scripts import diamond_sizing_audit as sa

    verdict, ratio = sa._classify_sizing(usd_per_r_max_abs=50.0, threshold_usd=None)
    assert verdict == "INSUFFICIENT_DATA"
    assert ratio is None


def test_classify_sizing_insufficient_data_when_no_pnl_samples() -> None:
    from eta_engine.scripts import diamond_sizing_audit as sa

    verdict, ratio = sa._classify_sizing(usd_per_r_max_abs=None, threshold_usd=-200.0)
    assert verdict == "INSUFFICIENT_DATA"
    assert ratio is None


# ────────────────────────────────────────────────────────────────────
# _score_bot — end-to-end with synthetic trade rows
# ────────────────────────────────────────────────────────────────────


def _trade(bot_id: str, r: float, pnl: float | None,
           qty: float = 1.0, idx: int = 0) -> dict:
    """Build a minimal trade-close record."""
    row: dict = {
        "bot_id": bot_id,
        "signal_id": f"{bot_id}_{idx}",
        "ts": f"2026-05-{(idx % 28) + 1:02d}T14:00:00+00:00",
        "realized_r": r,
        "extra": {"qty": qty},
    }
    if pnl is not None:
        row["extra"]["realized_pnl"] = pnl
    return row


def test_score_bot_clean_pnl_samples_classifies() -> None:
    """Bot with consistent $50/R per trade, $200 floor → 4 stopouts → OK."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    trades = [
        _trade("test", r=1.0, pnl=50.0, idx=i)
        for i in range(10)
    ]
    sc = sa._score_bot("test", trades, threshold_usd=-200.0)
    assert sc.n_trades_with_pnl == 10
    assert sc.usd_per_r_max_abs == 50.0
    assert sc.verdict == "SIZING_OK"
    assert sc.n_stopouts_to_breach == 4.0


def test_score_bot_excludes_quarantined_rows() -> None:
    """diamond_data_sanitizer marks scale-bug rows with
    _sanitizer_quarantined=True. The audit must ignore their dollars."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    trades = []
    # 10 good trades with clean PnL
    for i in range(10):
        trades.append(_trade("test", r=1.0, pnl=20.0, idx=i))
    # 5 quarantined trades with poisoned PnL — must be excluded
    for i in range(10, 15):
        bad = _trade("test", r=1.0, pnl=99999.0, idx=i)
        bad["_sanitizer_quarantined"] = True
        trades.append(bad)
    sc = sa._score_bot("test", trades, threshold_usd=-200.0)
    # Only the 10 good rows feed the sizing stats
    assert sc.n_trades_with_pnl == 10
    assert sc.usd_per_r_max_abs == 20.0  # not 99999


def test_score_bot_excludes_near_zero_r_samples() -> None:
    """Trades with |R| < MIN_ABS_R_FOR_RATIO can't contribute to $/R
    (would divide-explode). They're dropped from sizing stats."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    trades = [
        _trade("test", r=0.001, pnl=1000.0, idx=0),  # tiny R, huge pnl
        _trade("test", r=0.0, pnl=500.0, idx=1),     # exactly zero
        *[_trade("test", r=1.0, pnl=30.0, idx=i + 10) for i in range(10)],
    ]
    sc = sa._score_bot("test", trades, threshold_usd=-200.0)
    assert sc.n_trades_with_pnl == 10  # only the 10 valid-R trades
    assert sc.usd_per_r_max_abs == 30.0


def test_score_bot_insufficient_data_below_min_trades() -> None:
    """Fewer than MIN_TRADES_FOR_VERDICT real-PnL rows → INSUFFICIENT_DATA."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    trades = [_trade("test", r=1.0, pnl=50.0, idx=i) for i in range(3)]
    sc = sa._score_bot("test", trades, threshold_usd=-200.0)
    assert sc.verdict == "INSUFFICIENT_DATA"


def test_score_bot_uses_worst_trade_not_mean() -> None:
    """The verdict uses the WORST single-trade $/R (so a black-swan
    outlier triggers BREACHED even if the mean looks fine)."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    # 9 small trades + 1 outlier: mean $19, worst $250
    trades = [_trade("test", r=1.0, pnl=10.0, idx=i) for i in range(9)]
    trades.append(_trade("test", r=1.0, pnl=250.0, idx=9))
    sc = sa._score_bot("test", trades, threshold_usd=-200.0)
    assert sc.usd_per_r_max_abs == 250.0
    assert sc.verdict == "SIZING_BREACHED"  # worst-trade trips floor
    # The mean still computed for operator visibility but doesn't drive verdict
    assert sc.usd_per_r_avg is not None
    assert sc.usd_per_r_avg < sc.usd_per_r_max_abs


def test_score_bot_handles_negative_pnl() -> None:
    """$/R magnitude is what matters — losses count for the breach test
    just like gains do (and usually are the actual stopout direction).

    Boundary note: n_breach=2.0 exactly falls into SIZING_TIGHT
    (the FRAGILE band is strictly < 2.0).  Use $/R=150 to test FRAGILE
    explicitly (200/150 = 1.33 → FRAGILE)."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    # Losing trades: each -1R, -$150 → $/R = +$150 (negative-on-negative)
    trades = [_trade("test", r=-1.0, pnl=-150.0, idx=i) for i in range(10)]
    sc = sa._score_bot("test", trades, threshold_usd=-200.0)
    assert sc.usd_per_r_max_abs == 150.0
    # 200 / 150 = 1.33 → SIZING_FRAGILE (1 <= ratio < 2)
    assert sc.verdict == "SIZING_FRAGILE"


# ────────────────────────────────────────────────────────────────────
# Snapshot write
# ────────────────────────────────────────────────────────────────────


def test_run_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    """run() must persist a summary to OUT_LATEST."""
    from eta_engine.scripts import diamond_sizing_audit as sa

    can_path = tmp_path / "canonical.jsonl"
    leg_path = tmp_path / "legacy.jsonl"
    _write_jsonl(can_path, [
        _trade("m2k_sweep_reclaim", r=1.0, pnl=20.0, idx=i)
        for i in range(10)
    ])
    _write_jsonl(leg_path, [])
    monkeypatch.setattr(sa, "TRADE_CLOSES_CANONICAL", can_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(sa, "TRADE_CLOSES_LEGACY", leg_path)  # type: ignore[attr-defined]
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(sa, "OUT_LATEST", out_path)  # type: ignore[attr-defined]
    summary = sa.run()

    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in on_disk
    assert "statuses" in on_disk
    assert "verdict_counts" in on_disk
    assert on_disk["n_diamonds"] == summary["n_diamonds"]
