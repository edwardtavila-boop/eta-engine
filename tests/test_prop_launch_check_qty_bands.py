"""Wave-25o tests: per-qty-band breakdown + vol-regime filter candidate.

Verifies that ``_check_launch_candidates`` in prop_launch_check:

1. Classifies a bot meeting all 5 hard criteria as a strict candidate.
2. Classifies a bot whose qty<1 band meets the launch profile (but
   aggregate fails) as a ``vol_regime_filter_candidate``.
3. Leaves a plain churning bot in ``rejected`` with no filter flag.
4. Surfaces the per-qty-band stats on every record so the operator can
   see the split directly in the Sunday-EOD CLI output.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the workspace root is on sys.path so the module imports cleanly
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import prop_launch_check as mod  # noqa: E402


def _make_row(
    *,
    bot_id: str,
    realized_r: float,
    realized_pnl: float,
    qty: float = 1.0,
) -> dict:
    """Build a minimal close-record dict matching load_close_records output."""
    return {
        "bot_id": bot_id,
        "realized_r": realized_r,
        "realized_pnl": realized_pnl,
        "qty": qty,
        "extra": {},
    }


def _patch_loader(monkeypatch, rows_by_bot: dict[str, list[dict]]) -> None:
    """Patch load_close_records + DIAMOND_BOTS so the scan sees ONLY our fixture."""
    from eta_engine.feeds import capital_allocator
    from eta_engine.scripts import closed_trade_ledger

    monkeypatch.setattr(capital_allocator, "DIAMOND_BOTS", tuple(sorted(rows_by_bot)))

    def _fake_load(bot_filter=None, data_sources=None, **kwargs):  # noqa: ARG001
        if bot_filter is None:
            return [r for rows in rows_by_bot.values() for r in rows]
        return list(rows_by_bot.get(bot_filter, []))

    monkeypatch.setattr(closed_trade_ledger, "load_close_records", _fake_load)


@pytest.fixture(autouse=True)
def _no_asym_audit(monkeypatch, tmp_path) -> None:
    """Default: no qty-asymmetry receipt on disk (start clean)."""
    monkeypatch.setattr(mod, "WORKSPACE_ROOT", tmp_path)


def test_strict_candidate_meets_all_five_criteria(monkeypatch) -> None:
    """A bot with n>=50, all positive, WR>=50%, !ASYM lands in candidates."""
    rows = [_make_row(bot_id="winner", realized_r=1.0, realized_pnl=50.0) for _ in range(60)]
    _patch_loader(monkeypatch, {"winner": rows})

    result = mod._check_launch_candidates()

    assert result["n_candidates"] == 1
    assert result["candidates"][0]["bot_id"] == "winner"
    assert result["n_filter_candidates"] == 0


def test_vol_regime_filter_candidate_classified_when_qty_lt1_band_passes(monkeypatch) -> None:
    """Wave-25o: aggregate fails (qty=1 churn) but qty<1 band launch-worthy."""
    # Mimic mnq_futures_sage's bifurcated book:
    #   - qty=1.0 (normal-vol, wide stops): 30 churners avg -$30/each
    #   - qty=0.5 (high-vol, tight stops): 25 winners avg +$25/each
    qty1_rows = [
        _make_row(bot_id="sage", realized_r=-0.5, realized_pnl=-30.0, qty=1.0)
        for _ in range(30)
    ]
    qty_half_rows = [
        _make_row(bot_id="sage", realized_r=+6.0, realized_pnl=+25.0, qty=0.5)
        for _ in range(25)
    ]
    _patch_loader(monkeypatch, {"sage": qty1_rows + qty_half_rows})

    result = mod._check_launch_candidates()

    # Aggregate fails: cum_USD = -$900 + $625 = -$275, cum_R = -15 + 150 = 135 (R-pos)
    # but USD-neg → strict_candidate=False.
    assert result["n_candidates"] == 0

    # Filter candidate fires: qty<1 band has n=25, WR=100%, cum_USD=+625
    assert result["n_filter_candidates"] == 1
    fc = result["filter_candidates"][0]
    assert fc["bot_id"] == "sage"
    assert fc["vol_regime_filter_candidate"] is True
    assert fc["qty_band_half"]["n"] == 25
    assert fc["qty_band_half"]["wr"] == 100.0
    assert fc["qty_band_half"]["cum_usd"] == 625.0
    assert fc["qty_band_full"]["n"] == 30
    assert fc["qty_band_full"]["wr"] == 0.0


def test_plain_rejected_bot_has_no_filter_flag(monkeypatch) -> None:
    """A bot that's bad across both bands ends up in rejected without filter."""
    qty1_rows = [
        _make_row(bot_id="loser", realized_r=-0.5, realized_pnl=-30.0, qty=1.0)
        for _ in range(30)
    ]
    qty_half_rows = [
        _make_row(bot_id="loser", realized_r=-0.5, realized_pnl=-15.0, qty=0.5)
        for _ in range(25)
    ]
    _patch_loader(monkeypatch, {"loser": qty1_rows + qty_half_rows})

    result = mod._check_launch_candidates()

    assert result["n_candidates"] == 0
    assert result["n_filter_candidates"] == 0
    assert len(result["rejected_top5"]) == 1
    assert result["rejected_top5"][0]["vol_regime_filter_candidate"] is False


def test_filter_candidate_requires_min_20_half_band_samples(monkeypatch) -> None:
    """Edge case: qty<1 band 100% WR but only 15 samples → NOT a filter candidate."""
    qty1_rows = [
        _make_row(bot_id="too_small_half", realized_r=-0.5, realized_pnl=-30.0, qty=1.0)
        for _ in range(40)
    ]
    qty_half_rows = [
        _make_row(bot_id="too_small_half", realized_r=+6.0, realized_pnl=+25.0, qty=0.5)
        for _ in range(15)  # below the 20-sample threshold
    ]
    _patch_loader(monkeypatch, {"too_small_half": qty1_rows + qty_half_rows})

    result = mod._check_launch_candidates()

    assert result["n_filter_candidates"] == 0
    # Still surfaces band stats in rejected output
    assert result["rejected_top5"][0]["qty_band_half"]["n"] == 15


def test_band_breakdown_records_qty_extracted_from_extra_when_top_level_missing(monkeypatch) -> None:
    """_row_qty falls back to extra.qty when the top-level field is absent."""
    rows = []
    for _ in range(25):
        rows.append({
            "bot_id": "extra_qty",
            "realized_r": +6.0,
            "realized_pnl": +25.0,
            # NO top-level qty
            "extra": {"qty": 0.5},
        })
    # Pad qty=1 band with churners so the aggregate fails
    for _ in range(40):
        rows.append({
            "bot_id": "extra_qty",
            "realized_r": -0.5,
            "realized_pnl": -30.0,
            "extra": {"qty": 1.0},
        })
    _patch_loader(monkeypatch, {"extra_qty": rows})

    result = mod._check_launch_candidates()

    # The extra.qty fallback should give us 25 in the half band, 40 in the full band.
    assert result["n_filter_candidates"] == 1
    fc = result["filter_candidates"][0]
    assert fc["qty_band_half"]["n"] == 25
    assert fc["qty_band_full"]["n"] == 40


def test_strict_candidate_wins_over_filter_candidate(monkeypatch) -> None:
    """If a bot passes strict criteria, it must NOT also be flagged as filter."""
    # All trades positive, all qty=1: aggregate passes → strict.
    rows = [_make_row(bot_id="dual", realized_r=+1.0, realized_pnl=+50.0, qty=1.0) for _ in range(60)]
    # Plus 25 qty<1 winners
    rows += [_make_row(bot_id="dual", realized_r=+6.0, realized_pnl=+25.0, qty=0.5) for _ in range(25)]
    _patch_loader(monkeypatch, {"dual": rows})

    result = mod._check_launch_candidates()

    assert result["n_candidates"] == 1
    assert result["n_filter_candidates"] == 0  # mutually exclusive
    assert result["candidates"][0]["vol_regime_filter_candidate"] is False


def _minimal_action_inputs() -> dict:
    return {
        "dryrun": {"sections": []},
        "lifecycle": {
            "counts": {
                "EVAL_LIVE": 0,
                "EVAL_PAPER": 10,
                "FUNDED_LIVE": 0,
                "RETIRED": 0,
            },
            "by_state": {},
        },
        "leaderboard": {"n_prop_ready": 2},
        "channels": {"telegram": True, "discord": False, "generic": False},
        "drawdown": {"signal": "OK"},
        "supervisor": {"missing": False, "age_seconds": 1},
        "candidates": {"n_candidates": 1, "filter_candidates": [], "rejected_top5": []},
    }


def test_action_list_blocks_live_promotion_before_july_8() -> None:
    """Before the date floor, prop_launch_check must recommend paper-only drills."""
    inputs = _minimal_action_inputs()

    actions = mod._build_action_list(
        inputs["dryrun"],
        inputs["lifecycle"],
        inputs["leaderboard"],
        inputs["channels"],
        inputs["drawdown"],
        supervisor=inputs["supervisor"],
        candidates=inputs["candidates"],
        live_capital_calendar={
            "live_capital_allowed_by_date": False,
            "not_before": "2026-07-08",
            "days_until_live_capital": 48,
        },
    )

    joined = "\n".join(actions)
    assert "NO LIVE CAPITAL BEFORE 2026-07-08" in joined
    assert "Do not promote bots to EVAL_LIVE/FUNDED_LIVE" in joined
    assert "Promote at least one PROP_READY bot to EVAL_LIVE" not in joined


def test_action_list_allows_live_promotion_after_calendar_floor() -> None:
    """After the date floor, the old lifecycle action can reappear if gates allow."""
    inputs = _minimal_action_inputs()

    actions = mod._build_action_list(
        inputs["dryrun"],
        inputs["lifecycle"],
        inputs["leaderboard"],
        inputs["channels"],
        inputs["drawdown"],
        supervisor=inputs["supervisor"],
        candidates=inputs["candidates"],
        live_capital_calendar={
            "live_capital_allowed_by_date": True,
            "not_before": "2026-07-08",
            "days_until_live_capital": 0,
        },
    )

    joined = "\n".join(actions)
    assert "NO LIVE CAPITAL BEFORE" not in joined
    assert "Promote at least one PROP_READY bot to EVAL_LIVE" in joined


def test_action_list_uses_corrected_mnq_futures_sage_filter_guidance() -> None:
    """mnq_futures_sage must point at the corrected partial-profit experiment."""
    inputs = _minimal_action_inputs()
    inputs["candidates"] = {
        "n_candidates": 0,
        "filter_candidates": [
            {
                "bot_id": "mnq_futures_sage",
                "qty_band_half": {"n": 25, "wr": 100.0, "cum_usd": 625.0},
                "qty_band_full": {"n": 30, "wr": 0.0, "cum_usd": -900.0},
            }
        ],
        "rejected_top5": [],
    }

    actions = mod._build_action_list(
        inputs["dryrun"],
        inputs["lifecycle"],
        inputs["leaderboard"],
        inputs["channels"],
        inputs["drawdown"],
        supervisor=inputs["supervisor"],
        candidates=inputs["candidates"],
    )

    joined = "\n".join(actions)
    assert "partial_profit_enabled=false" in joined
    assert "MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC_CORRECTION_2026_05_13.md" in joined
    assert "vol_low_size_mult=0.0" not in joined
