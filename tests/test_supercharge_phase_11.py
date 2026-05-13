"""Tests for the supercharge Phase 11 additions:

- l2_supervisor_state_persister (closes the reconciliation loop)
- l2_seed_news_calendar (2026 H1+H2 FOMC/NFP/CPI/ECB/witching seed)
- Integration: persister output is readable by l2_reconciliation
- Supervisor wiring: FILLED status path triggers record_fill with
  entry-leg fill details (slip, commission, fill price)
"""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import l2_reconciliation as recon
from eta_engine.scripts import l2_seed_news_calendar as seed
from eta_engine.scripts import l2_supervisor_state_persister as persister
from eta_engine.scripts.l2_news_blackout import is_in_blackout

if TYPE_CHECKING:
    import pytest

# ────────────────────────────────────────────────────────────────────
# l2_supervisor_state_persister — basic write/read
# ────────────────────────────────────────────────────────────────────


def test_persister_writes_dict_positions(tmp_path: Path) -> None:
    target = tmp_path / "supervisor.json"
    positions = [
        {"bot_id": "ETA-L2-BookImbalance-MNQ", "symbol": "MNQ", "side": "LONG", "qty": 1},
        {"bot_id": "ETA-L2-Microprice-NQ", "symbol": "NQ", "side": "SHORT", "qty": 2},
    ]
    result = persister.persist_open_positions(positions, _path=target)
    assert result.ok is True
    assert result.n_positions == 2
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["n_open"] == 2
    assert len(payload["positions"]) == 2
    assert payload["positions"][0]["side"] == "LONG"


def test_persister_handles_dataclass_positions(tmp_path: Path) -> None:
    @dataclass
    class Pos:
        bot_id: str
        symbol: str
        side: str
        qty: int

    target = tmp_path / "supervisor.json"
    result = persister.persist_open_positions([Pos("ETA-Bot-A", "MNQ", "LONG", 3)], _path=target)
    assert result.ok is True
    assert result.n_positions == 1
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["positions"][0]["bot_id"] == "ETA-Bot-A"


def test_persister_drops_malformed_records(tmp_path: Path) -> None:
    """Records with missing fields or invalid types are silently dropped,
    rest persist normally — never crash supervisor."""
    target = tmp_path / "supervisor.json"
    positions = [
        {"bot_id": "valid", "symbol": "MNQ", "side": "LONG", "qty": 1},
        {"bot_id": "missing_qty", "symbol": "ES", "side": "LONG"},
        {"bot_id": "bad_side", "symbol": "ES", "side": "SIDEWAYS", "qty": 1},
        {"bot_id": "zero_qty", "symbol": "ES", "side": "LONG", "qty": 0},
        None,
        "not a record",
    ]
    result = persister.persist_open_positions(positions, _path=target)
    assert result.ok is True
    assert result.n_positions == 1  # only the first is valid


def test_persister_drops_negative_qty(tmp_path: Path) -> None:
    target = tmp_path / "supervisor.json"
    result = persister.persist_open_positions(
        [{"bot_id": "x", "symbol": "MNQ", "side": "LONG", "qty": -5}], _path=target
    )
    assert result.ok is True
    assert result.n_positions == 0


def test_persister_empty_list_writes_empty_payload(tmp_path: Path) -> None:
    """Persister with no positions writes the file with n_open=0 — the
    state itself is still a valid heartbeat (flat is a valid state)."""
    target = tmp_path / "supervisor.json"
    result = persister.persist_open_positions([], _path=target)
    assert result.ok is True
    assert result.n_positions == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["n_open"] == 0
    assert payload["positions"] == []


def test_persister_lowercase_side_normalizes(tmp_path: Path) -> None:
    target = tmp_path / "supervisor.json"
    result = persister.persist_open_positions(
        [{"bot_id": "x", "symbol": "MNQ", "side": "long", "qty": 1}], _path=target
    )
    assert result.ok is True
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["positions"][0]["side"] == "LONG"


def test_persister_order_side_normalizes_to_position_side(tmp_path: Path) -> None:
    target = tmp_path / "supervisor.json"
    result = persister.persist_open_positions(
        [
            {"bot_id": "long_bot", "symbol": "MNQ", "side": "BUY", "qty": 1},
            {"bot_id": "short_bot", "symbol": "MCL", "side": "SELL", "qty": 1},
        ],
        _path=target,
    )
    assert result.ok is True
    assert result.n_positions == 2
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert [row["side"] for row in payload["positions"]] == ["LONG", "SHORT"]


def test_persister_atomic_overwrite(tmp_path: Path) -> None:
    """A second call replaces the file in place — readers always see
    one of two whole snapshots, never a half-written file."""
    target = tmp_path / "supervisor.json"
    persister.persist_open_positions([{"bot_id": "a", "symbol": "MNQ", "side": "LONG", "qty": 1}], _path=target)
    persister.persist_open_positions([{"bot_id": "b", "symbol": "ES", "side": "SHORT", "qty": 2}], _path=target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["n_open"] == 1
    assert payload["positions"][0]["bot_id"] == "b"
    # Tmp sidecar should be cleaned up by os.replace
    assert not (tmp_path / "supervisor.json.tmp").exists()


def test_persister_read_back(tmp_path: Path) -> None:
    target = tmp_path / "supervisor.json"
    persister.persist_open_positions([{"bot_id": "x", "symbol": "MNQ", "side": "LONG", "qty": 1}], _path=target)
    data = persister.read_persisted_state(_path=target)
    assert data is not None
    assert data["n_open"] == 1


def test_persister_read_back_missing_returns_none(tmp_path: Path) -> None:
    assert persister.read_persisted_state(_path=tmp_path / "nope.json") is None


# ────────────────────────────────────────────────────────────────────
# l2_supervisor_state_persister — staleness
# ────────────────────────────────────────────────────────────────────


def test_persister_staleness_fresh(tmp_path: Path) -> None:
    target = tmp_path / "supervisor.json"
    write_time = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    persister.persist_open_positions([], _path=target, _now=write_time)
    age = persister.staleness_seconds(_path=target, _now=write_time + timedelta(seconds=15))
    assert age is not None
    assert 10 < age < 20


def test_persister_staleness_missing_returns_none(tmp_path: Path) -> None:
    age = persister.staleness_seconds(_path=tmp_path / "nope.json")
    assert age is None


def test_persister_staleness_malformed_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "supervisor.json"
    target.write_text("{not valid json", encoding="utf-8")
    assert persister.staleness_seconds(_path=target) is None


# ────────────────────────────────────────────────────────────────────
# Integration: persister output → reconciliation
# ────────────────────────────────────────────────────────────────────


def test_persister_output_readable_by_reconciliation(tmp_path: Path) -> None:
    """The end-to-end contract: the file the persister writes must be
    parseable by l2_reconciliation.load_supervisor_positions.  If this
    test fails the reconciliation loop is broken."""
    target = tmp_path / "supervisor.json"
    persister.persist_open_positions(
        [
            {"bot_id": "ETA-L2-BookImbalance-MNQ", "symbol": "MNQ", "side": "LONG", "qty": 1},
            {"bot_id": "ETA-L2-Microprice-NQ", "symbol": "NQ", "side": "SHORT", "qty": 2},
        ],
        _path=target,
    )
    parsed = recon.load_supervisor_positions(_path=target)
    assert len(parsed) == 2
    assert {p.symbol for p in parsed} == {"MNQ", "NQ"}
    assert all(isinstance(p.qty, int) for p in parsed)


def test_reconciliation_detects_phantom_when_persister_says_open(tmp_path: Path) -> None:
    """If the supervisor's persisted state has a position but broker
    fill log is empty, reconciliation should report PHANTOM_BELIEF."""
    state_path = tmp_path / "supervisor.json"
    broker_path = tmp_path / "broker_fills.jsonl"  # missing
    persister.persist_open_positions(
        [
            {"bot_id": "ghost-bot", "symbol": "MNQ", "side": "LONG", "qty": 1},
        ],
        _path=state_path,
    )
    report = recon.reconcile(_supervisor_path=state_path, _broker_path=broker_path)
    assert report.n_discrepancies == 1
    assert report.discrepancies[0].verdict == "PHANTOM_BELIEF"


def test_reconciliation_in_sync_after_persist(tmp_path: Path) -> None:
    """When persister state matches reconstructed broker positions,
    reconciliation reports IN_SYNC."""
    state_path = tmp_path / "supervisor.json"
    broker_path = tmp_path / "broker_fills.jsonl"
    now = datetime.now(UTC)
    # Write one ENTRY fill into broker log
    fill = {
        "ts": now.isoformat(),
        "signal_id": "MNQ-001",
        "symbol": "MNQ",
        "bot_id": "ETA-L2-BookImbalance-MNQ",
        "side": "LONG",
        "qty_filled": 1,
        "exit_reason": "ENTRY",
    }
    broker_path.write_text(json.dumps(fill) + "\n", encoding="utf-8")
    persister.persist_open_positions(
        [
            {"bot_id": "ETA-L2-BookImbalance-MNQ", "symbol": "MNQ", "side": "LONG", "qty": 1},
        ],
        _path=state_path,
    )
    report = recon.reconcile(_supervisor_path=state_path, _broker_path=broker_path)
    assert report.n_discrepancies == 0
    assert report.n_in_sync == 1


# ────────────────────────────────────────────────────────────────────
# l2_seed_news_calendar
# ────────────────────────────────────────────────────────────────────


def test_seed_fomc_returns_8_meetings() -> None:
    """FOMC scheduled meetings should be exactly 8 per year."""
    fomc = seed.fomc_windows_2026()
    assert len(fomc) == 8
    assert all(w.reason == "FOMC" for w in fomc)


def test_seed_nfp_returns_12_first_fridays() -> None:
    nfp = seed.nfp_windows_2026()
    assert len(nfp) == 12
    # First NFP of 2026 is Friday Jan 2 (1st Friday of January)
    assert "2026-01-02" in nfp[0].start


def test_seed_nfp_dates_are_all_fridays() -> None:
    """First-Friday rule means every NFP date must be a Friday."""
    for w in seed.nfp_windows_2026():
        dt = datetime.fromisoformat(w.start.replace("Z", "+00:00"))
        # day-of-week 4 = Friday
        # NB: start = release - 15min, both same day
        assert dt.weekday() == 4, f"{w.start} is not a Friday"


def test_seed_cpi_returns_12_releases() -> None:
    cpi = seed.cpi_windows_2026()
    assert len(cpi) == 12


def test_seed_ecb_returns_8_meetings() -> None:
    ecb = seed.ecb_windows_2026()
    assert len(ecb) == 8


def test_seed_witching_returns_4_quarters() -> None:
    """Quad-witching is Mar/Jun/Sep/Dec — 4 per year."""
    witch = seed.witching_windows_2026()
    assert len(witch) == 4
    months = sorted({datetime.fromisoformat(w.start.replace("Z", "+00:00")).month for w in witch})
    assert months == [3, 6, 9, 12]


def test_seed_witching_dates_are_3rd_friday() -> None:
    for w in seed.witching_windows_2026():
        dt = datetime.fromisoformat(w.start.replace("Z", "+00:00"))
        assert dt.weekday() == 4  # Friday
        # 3rd Friday means day-of-month between 15 and 21 inclusive
        assert 15 <= dt.day <= 21


def test_seed_all_2026_returns_44_windows() -> None:
    """Total seed should be 8 + 12 + 12 + 8 + 4 = 44 windows."""
    seeds = seed.all_2026_seeds()
    assert len(seeds) == 44


def test_seed_all_windows_have_required_fields() -> None:
    """Every seeded window must be parseable as a BlackoutWindow."""
    for w in seed.all_2026_seeds():
        assert w.start and w.end and w.reason and w.symbols
        # ISO 8601 timestamps must parse
        datetime.fromisoformat(w.start.replace("Z", "+00:00"))
        datetime.fromisoformat(w.end.replace("Z", "+00:00"))
        # end strictly after start
        assert w.start < w.end


def test_seed_us_macro_events_include_mnq() -> None:
    """Operator's primary symbol MNQ must appear in every US macro event."""
    for kind in (seed.fomc_windows_2026, seed.nfp_windows_2026, seed.cpi_windows_2026):
        for w in kind():
            assert "MNQ" in w.symbols, f"{w.reason} window {w.start} missing MNQ in symbols"


# ────────────────────────────────────────────────────────────────────
# Integration: seeded calendar → is_in_blackout
# ────────────────────────────────────────────────────────────────────


def test_seeded_calendar_loads_via_news_blackout(tmp_path: Path) -> None:
    """End-to-end: seed file → load_events → is_in_blackout returns
    True during the window."""
    target = tmp_path / "events.jsonl"
    # Seed one known FOMC window manually (mirror the seeder)
    fomc = seed.fomc_windows_2026()[0]
    target.write_text(
        json.dumps(
            {"start": fomc.start, "end": fomc.end, "reason": fomc.reason, "symbols": fomc.symbols, "note": fomc.note}
        )
        + "\n",
        encoding="utf-8",
    )
    # Pick a UTC time inside the window
    when = datetime.fromisoformat(fomc.start.replace("Z", "+00:00")) + timedelta(minutes=10)
    result = is_in_blackout("MNQ", when=when, _path=target)
    assert result.in_blackout is True
    assert "FOMC" in (result.reason or "")


def test_seeded_calendar_clear_outside_window(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    fomc = seed.fomc_windows_2026()[0]
    target.write_text(
        json.dumps(
            {"start": fomc.start, "end": fomc.end, "reason": fomc.reason, "symbols": fomc.symbols, "note": fomc.note}
        )
        + "\n",
        encoding="utf-8",
    )
    when = datetime.fromisoformat(fomc.end.replace("Z", "+00:00")) + timedelta(hours=2)
    result = is_in_blackout("MNQ", when=when, _path=target)
    assert result.in_blackout is False


def test_seeded_calendar_filters_by_symbol(tmp_path: Path) -> None:
    """An ECB window covers MNQ + 6E but NOT MGC (gold).  is_in_blackout
    should respect the symbol filter."""
    target = tmp_path / "events.jsonl"
    ecb = seed.ecb_windows_2026()[0]
    target.write_text(
        json.dumps({"start": ecb.start, "end": ecb.end, "reason": ecb.reason, "symbols": ecb.symbols, "note": ecb.note})
        + "\n",
        encoding="utf-8",
    )
    when = datetime.fromisoformat(ecb.start.replace("Z", "+00:00")) + timedelta(minutes=10)
    # MNQ is in ECB_SYMBOLS
    assert is_in_blackout("MNQ", when=when, _path=target).in_blackout
    # MGC is NOT in ECB_SYMBOLS
    assert not is_in_blackout("MGC", when=when, _path=target).in_blackout


# ────────────────────────────────────────────────────────────────────
# Supervisor wiring: FILLED path triggers record_fill
# ────────────────────────────────────────────────────────────────────


def test_supervisor_filled_path_triggers_record_fill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the broker returns OrderStatus.FILLED with avg_price + qty,
    the supervisor's new wiring calls record_fill with exit_reason=ENTRY,
    capturing the entry-leg slip for the audit pipeline.

    Why this matters: without this path, the fill audit pipeline sees
    zero real fills until the operator wires a separate IBKR execution
    callback.  This synchronous capture is the MVP — bracket exits still
    need the async callback to round out the picture.
    """
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    captured: list[dict] = []

    def _capture_fill(**kwargs) -> None:
        captured.append(kwargs)

    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "100000")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "100000")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor.l2hooks, "record_signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.l2hooks, "record_fill", _capture_fill)

    class _Venue:
        def place_order(self, _request):
            return object()

    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: _Venue())
    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        lambda *_a, **_kw: OrderResult(
            order_id="sig-filled",
            status=OrderStatus.FILLED,
            filled_qty=1.0,
            avg_price=28251.25,
            fees=0.75,
            raw={"ibkr_order_id": 9001},
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="filled-bot",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500_000.0,
    )

    rec = router.submit_entry(
        bot=bot,
        signal_id="sig-filled",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )

    assert rec is not None
    # The supervisor's new wiring must have triggered exactly one fill
    # capture with ENTRY semantics and the broker's avg_price.
    assert len(captured) == 1
    call = captured[0]
    assert call["exit_reason"] == "ENTRY"
    assert call["side"] == "LONG"  # mapped from BUY
    assert call["actual_fill_price"] == 28251.25
    assert call["qty_filled"] == 1
    assert call["commission_usd"] == 0.75
    assert call["signal_id"] == "sig-filled"
    # intended_price should match the bracket reference used to place
    # the order (the bar close after tick-rounding)
    assert call["intended_price"] > 0


def test_supervisor_open_path_does_not_trigger_record_fill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OrderStatus.OPEN (not yet filled) must NOT call record_fill —
    we only capture terminal events.  Otherwise the audit log would be
    polluted with un-filled order acks."""
    from eta_engine.scripts import jarvis_strategy_supervisor as supervisor
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )
    from eta_engine.venues.base import OrderResult, OrderStatus

    fill_calls: list[dict] = []
    monkeypatch.setenv("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1")
    monkeypatch.setenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "100000")
    monkeypatch.setenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "100000")
    monkeypatch.setattr(supervisor.l2hooks, "pre_trade_check", lambda *_args: True)
    monkeypatch.setattr(supervisor.l2hooks, "record_signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.l2hooks, "record_fill", lambda **kw: fill_calls.append(kw))

    class _Venue:
        def place_order(self, _request):
            return object()

    monkeypatch.setattr(supervisor, "_get_live_ibkr_venue", lambda: _Venue())
    monkeypatch.setattr(
        supervisor,
        "_run_on_live_ibkr_loop",
        lambda *_a, **_kw: OrderResult(
            order_id="sig-open",
            status=OrderStatus.OPEN,
            raw={"ibkr_order_id": 9002},
        ),
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.paper_live_order_route = "direct_ibkr"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="open-bot",
        symbol="MNQ1",
        strategy_kind="x",
        direction="long",
        cash=500_000.0,
    )

    router.submit_entry(
        bot=bot,
        signal_id="sig-open",
        side="BUY",
        bar={"close": 28250.0, "high": 28260.0, "low": 28240.0, "open": 28245.0},
        size_mult=1.0,
    )
    assert fill_calls == []
