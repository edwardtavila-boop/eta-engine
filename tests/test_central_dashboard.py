"""Central dashboard snapshot tests — P6_FUNNEL central_dashboard.

Covers:
* :func:`eta_engine.funnel.central_dashboard.build_snapshot` across empty /
  single-bot / multi-bot states with and without staking balances.
* Alert-level rollup (OK → WATCH → PAUSE → KILL) and worst-bot tracking.
* 95%-of-baseline and 90%-of-baseline note generation.
* :func:`dump_snapshot` + :func:`from_json` round-trip.
* :func:`render_text` formatting contract.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from eta_engine.funnel.central_dashboard import (
    CentralDashboardSnapshot,
    build_snapshot,
    dump_snapshot,
    from_json,
    render_text,
)
from eta_engine.funnel.equity_monitor import EquityMonitor


def _monitor_with(states: dict[str, tuple[float, float]]) -> EquityMonitor:
    """Build an EquityMonitor from {name: (baseline, equity)} tuples."""
    em = EquityMonitor()
    for name, (baseline, equity) in states.items():
        em.register_bot(name, baseline)
        em.update(name, equity=equity, pnl=equity - baseline)
    return em


# ---------------------------------------------------------------------------
# build_snapshot
# ---------------------------------------------------------------------------


def test_empty_portfolio_snapshot() -> None:
    em = EquityMonitor()
    snap = build_snapshot(em.get_portfolio_state())
    assert snap.bots == []
    assert snap.staking == []
    assert snap.total_equity_usd == 0.0
    assert snap.total_baseline_usd == 0.0
    assert snap.total_excess_usd == 0.0
    assert snap.portfolio_health == "OK"
    assert snap.any_kill_triggered is False


def test_single_bot_healthy() -> None:
    em = _monitor_with({"mnq": (10_000.0, 10_500.0)})
    snap = build_snapshot(em.get_portfolio_state())
    assert len(snap.bots) == 1
    assert snap.bots[0].equity_usd == 10_500.0
    assert snap.bots[0].baseline_usd == 10_000.0
    assert snap.total_excess_usd == 500.0
    assert snap.portfolio_health == "OK"
    assert snap.notes == []


def test_bot_below_95pct_baseline_emits_note() -> None:
    em = _monitor_with({"mnq": (10_000.0, 9_000.0)})
    snap = build_snapshot(em.get_portfolio_state())
    assert any("< 95% baseline" in n for n in snap.notes)


def test_portfolio_below_90pct_baseline_downgrades_to_watch() -> None:
    em = _monitor_with(
        {
            "mnq": (10_000.0, 8_000.0),
            "eth": (5_000.0, 4_000.0),
        }
    )
    snap = build_snapshot(em.get_portfolio_state())
    assert snap.portfolio_health == "WATCH"
    assert any("< 90% baseline" in n for n in snap.notes)


def test_kill_alert_flips_health_to_kill() -> None:
    em = _monitor_with({"mnq": (10_000.0, 10_500.0)})
    snap = build_snapshot(
        em.get_portfolio_state(),
        bot_details={"mnq": {"alert_level": "KILL", "dd_pct": 15.0}},
    )
    assert snap.any_kill_triggered is True
    assert snap.portfolio_health == "KILL"


def test_pause_alert_yields_pause_health() -> None:
    em = _monitor_with({"a": (10_000.0, 10_000.0), "b": (10_000.0, 10_000.0)})
    snap = build_snapshot(
        em.get_portfolio_state(),
        bot_details={
            "a": {"alert_level": "WATCH", "dd_pct": 2.0},
            "b": {"alert_level": "PAUSE", "dd_pct": 5.0},
        },
    )
    assert snap.portfolio_health == "PAUSE"
    assert snap.any_kill_triggered is False


def test_worst_bot_tracked_by_dd() -> None:
    em = _monitor_with({"a": (10_000.0, 9_500.0), "b": (10_000.0, 9_000.0)})
    snap = build_snapshot(
        em.get_portfolio_state(),
        bot_details={
            "a": {"dd_pct": 3.0, "alert_level": "WATCH"},
            "b": {"dd_pct": 7.5, "alert_level": "WATCH"},
        },
    )
    assert snap.worst_bot_name == "b"
    assert snap.worst_bot_dd_pct == 7.5


def test_staking_balances_populate_snapshot() -> None:
    em = _monitor_with({"mnq": (10_000.0, 10_100.0)})
    staking = [
        {"protocol": "lido", "asset": "stETH", "balance": 2500.0, "apy_pct": 3.5},
        {"protocol": "jito", "asset": "JitoSOL", "balance": 1500.0, "apy_pct": 7.0},
    ]
    snap = build_snapshot(
        em.get_portfolio_state(),
        staking_balances=staking,
    )
    assert len(snap.staking) == 2
    lido = snap.staking[0]
    assert lido.protocol == "lido"
    assert lido.asset == "stETH"
    assert lido.balance == 2500.0
    # Estimated yield = 2500 * 3.5 / 100 = 87.5
    assert lido.est_yield_per_year_usd == pytest.approx(87.5)


def test_withdrawn_cold_is_reported() -> None:
    em = _monitor_with({"mnq": (10_000.0, 10_100.0)})
    snap = build_snapshot(em.get_portfolio_state(), withdrawn_cold_usd=2_345.67)
    assert snap.total_withdrawn_cold_usd == 2_345.67


# ---------------------------------------------------------------------------
# dump + from_json round-trip
# ---------------------------------------------------------------------------


def test_dump_and_reload_round_trip(tmp_path: Path) -> None:
    em = _monitor_with({"mnq": (10_000.0, 10_200.0)})
    snap = build_snapshot(em.get_portfolio_state(), withdrawn_cold_usd=500.0)
    path = dump_snapshot(snap, tmp_path / "snapshot.json")
    assert path.exists()

    raw = path.read_text(encoding="utf-8")
    roundtrip = from_json(raw)
    assert isinstance(roundtrip, CentralDashboardSnapshot)
    assert roundtrip.total_equity_usd == snap.total_equity_usd
    assert roundtrip.total_withdrawn_cold_usd == 500.0

    # from_json also accepts a pre-parsed dict.
    roundtrip2 = from_json(json.loads(raw))
    assert roundtrip2.portfolio_health == snap.portfolio_health


# ---------------------------------------------------------------------------
# render_text
# ---------------------------------------------------------------------------


def test_render_text_has_key_lines() -> None:
    em = _monitor_with({"mnq": (10_000.0, 10_100.0)})
    snap = build_snapshot(
        em.get_portfolio_state(),
        staking_balances=[{"protocol": "lido", "asset": "stETH", "balance": 1000.0, "apy_pct": 3.5}],
    )
    text = render_text(snap)
    assert "CENTRAL DASHBOARD" in text
    assert "total equity" in text
    assert "mnq" in text
    assert "lido" in text
