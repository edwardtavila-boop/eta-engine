"""Tests for the JARVIS supervisor -> dashboard bot_accounts bridge.

The bridge lives at ``scripts/jarvis_supervisor_bridge.py`` (a tracked
module). Its purpose: read the wave-12 JARVIS supervisor's heartbeat
at ``state/jarvis_intel/supervisor/heartbeat.json`` and lift each bot
into the ``bot_accounts`` shape that ``/api/bot-fleet`` consumes via
``build_bot_fleet_view`` -> ``_normalize_mnq_bot``.

These tests pin the bridge contract so the dashboard cannot regress to
the silent-zero state where ``confirmed_bots: 0`` despite the supervisor
running 16 bots.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_supervisor_bridge_returns_empty_when_heartbeat_missing(tmp_path: Path) -> None:
    """No heartbeat -> empty list, never an exception."""
    from eta_engine.scripts.jarvis_supervisor_bridge import (
        jarvis_supervisor_bot_accounts,
    )
    accounts = jarvis_supervisor_bot_accounts(
        heartbeat_path=tmp_path / "missing.json",
    )
    assert accounts == []


def test_supervisor_bridge_returns_empty_on_garbage_json(tmp_path: Path) -> None:
    """Malformed JSON -> empty list, never an exception."""
    from eta_engine.scripts.jarvis_supervisor_bridge import (
        jarvis_supervisor_bot_accounts,
    )
    hb = tmp_path / "heartbeat.json"
    hb.write_text("{not json}", encoding="utf-8")
    accounts = jarvis_supervisor_bot_accounts(heartbeat_path=hb)
    assert accounts == []


def test_supervisor_bridge_lifts_bots_into_account_shape(tmp_path: Path) -> None:
    """Heartbeat with bots -> properly-shaped ``bot_accounts`` rows."""
    from eta_engine.scripts.jarvis_supervisor_bridge import (
        jarvis_supervisor_bot_accounts,
    )
    hb_data = {
        "ts": "2026-04-28T12:00:00+00:00",
        "tick_count": 5,
        "n_bots": 2,
        "mode": "paper_sim",
        "bots": [
            {
                "bot_id": "mnq_futures",
                "symbol": "MNQ1",
                "strategy_kind": "orb",
                "direction": "long",
                "n_entries": 3,
                "n_exits": 3,
                "realized_pnl": 1.5,
                "open_position": None,
                "last_jarvis_verdict": "APPROVED",
                "last_bar_ts": "2026-04-28T12:00:00+00:00",
            },
            {
                "bot_id": "btc_hybrid",
                "symbol": "BTC",
                "strategy_kind": "hybrid",
                "direction": "long",
                "n_entries": 2,
                "n_exits": 1,
                "realized_pnl": -0.5,
                "open_position": {"side": "BUY", "entry_price": 67000.0},
                "last_jarvis_verdict": "CONDITIONAL",
                "last_bar_ts": "2026-04-28T12:00:00+00:00",
            },
        ],
    }
    hb = tmp_path / "heartbeat.json"
    hb.write_text(json.dumps(hb_data), encoding="utf-8")

    accounts = jarvis_supervisor_bot_accounts(heartbeat_path=hb)
    assert len(accounts) == 2

    mnq = accounts[0]
    assert mnq["id"] == "mnq_futures"
    assert mnq["name"] == "mnq_futures"
    assert mnq["status"] == "running"
    assert mnq["mode"] == "paper_sim"
    assert mnq["broker"] == "paper-sim"
    assert mnq["confirmed"] is True
    assert mnq["today"]["trades"] == 3
    assert mnq["today"]["wins"] == 3            # realized_pnl > 0
    assert mnq["today"]["losses"] == 0
    assert mnq["today"]["pnl"] == 1.5
    assert mnq["source"] == "jarvis_strategy_supervisor"
    assert mnq["last_jarvis_verdict"] == "APPROVED"

    btc = accounts[1]
    assert btc["id"] == "btc_hybrid"
    assert btc["status"] == "running"            # has open_position
    assert btc["today"]["wins"] == 0
    assert btc["today"]["losses"] == 1           # realized_pnl < 0
    assert btc["today"]["pnl"] == -0.5
    assert btc["open_position"]["side"] == "BUY"


def test_supervisor_bridge_idle_status_when_no_entries_no_position(tmp_path: Path) -> None:
    """A bot that hasn't traded yet -> status 'idle', not 'running'."""
    from eta_engine.scripts.jarvis_supervisor_bridge import (
        jarvis_supervisor_bot_accounts,
    )
    hb_data = {
        "ts": "2026-04-28T12:00:00+00:00",
        "mode": "paper_sim",
        "bots": [
            {
                "bot_id": "fresh_bot",
                "symbol": "XYZ",
                "strategy_kind": "test",
                "direction": "long",
                "n_entries": 0,
                "n_exits": 0,
                "realized_pnl": 0.0,
                "open_position": None,
                "last_jarvis_verdict": "",
                "last_bar_ts": "2026-04-28T12:00:00+00:00",
            },
        ],
    }
    hb = tmp_path / "heartbeat.json"
    hb.write_text(json.dumps(hb_data), encoding="utf-8")

    accounts = jarvis_supervisor_bot_accounts(heartbeat_path=hb)
    assert len(accounts) == 1
    assert accounts[0]["status"] == "idle"


def test_merge_returns_payload_unchanged_when_no_heartbeat(tmp_path: Path) -> None:
    """No supervisor running -> payload passes through untouched."""
    from eta_engine.scripts.jarvis_supervisor_bridge import (
        merge_supervisor_into_payload,
    )
    payload = {"bot_accounts": [{"id": "legacy_bot", "name": "legacy"}]}
    result = merge_supervisor_into_payload(
        payload, heartbeat_path=tmp_path / "missing.json",
    )
    assert result is payload


def test_merge_layers_supervisor_on_top_of_legacy(tmp_path: Path) -> None:
    """Supervisor rows take precedence; non-conflicting legacy rows kept."""
    from eta_engine.scripts.jarvis_supervisor_bridge import (
        merge_supervisor_into_payload,
    )
    hb_data = {
        "ts": "2026-04-28T12:00:00+00:00",
        "mode": "paper_sim",
        "bots": [
            {
                "bot_id": "mnq_futures",
                "symbol": "MNQ1",
                "strategy_kind": "orb",
                "direction": "long",
                "n_entries": 5,
                "n_exits": 5,
                "realized_pnl": 2.0,
                "open_position": None,
                "last_jarvis_verdict": "APPROVED",
                "last_bar_ts": "2026-04-28T12:00:00+00:00",
            },
        ],
    }
    hb = tmp_path / "heartbeat.json"
    hb.write_text(json.dumps(hb_data), encoding="utf-8")

    payload = {
        "bot_accounts": [
            {"id": "mnq_futures", "name": "stale_mnq", "today": {"trades": 0}},
            {"id": "legacy_btc", "name": "btc_legacy"},
        ],
    }
    result = merge_supervisor_into_payload(payload, heartbeat_path=hb)
    assert result is not payload                 # fresh dict
    accounts = result["bot_accounts"]
    ids = [a["id"] for a in accounts]
    assert "mnq_futures" in ids
    assert "legacy_btc" in ids                   # non-conflicting kept
    # The supervisor row replaced the legacy one (precedence rule)
    mnq = next(a for a in accounts if a["id"] == "mnq_futures")
    assert mnq["name"] == "mnq_futures"          # not "stale_mnq"
    assert mnq["today"]["trades"] == 5
    assert mnq["source"] == "jarvis_strategy_supervisor"


def test_supervisor_bridge_normalizes_through_dashboard_view(tmp_path: Path) -> None:
    """End-to-end: supervisor accounts pass through ``_normalize_mnq_bot``
    cleanly and surface in ``build_bot_fleet_view`` output.

    This test depends on ``command_center.server.bot_fleet_dashboard``
    being importable. Skip cleanly when it isn't (e.g. local dev where
    command_center isn't fully checked out yet).
    """
    import pytest
    try:
        from eta_engine.command_center.server.bot_fleet_dashboard import (
            build_bot_fleet_view,
        )
    except ImportError:
        pytest.skip("command_center.server.bot_fleet_dashboard not available")

    from eta_engine.scripts.jarvis_supervisor_bridge import (
        jarvis_supervisor_bot_accounts,
    )

    hb_data = {
        "ts": "2026-04-28T12:00:00+00:00",
        "mode": "paper_sim",
        "bots": [
            {
                "bot_id": "mnq_futures",
                "symbol": "MNQ1",
                "strategy_kind": "orb",
                "direction": "long",
                "n_entries": 5,
                "n_exits": 5,
                "realized_pnl": 2.0,
                "open_position": None,
                "last_jarvis_verdict": "APPROVED",
                "last_bar_ts": "2026-04-28T12:00:00+00:00",
            },
        ],
    }
    hb = tmp_path / "heartbeat.json"
    hb.write_text(json.dumps(hb_data), encoding="utf-8")

    sup_accounts = jarvis_supervisor_bot_accounts(heartbeat_path=hb)
    payload = {"bot_accounts": sup_accounts}
    view = build_bot_fleet_view(
        payload,
        base_url="http://test",
        repo_root=hb.parent,
        payload_source="test",
    )
    mnq_rows = view.get("mnq_rows") or []
    assert any(r.get("id") == "mnq_futures" for r in mnq_rows), (
        f"supervisor bot did not surface in mnq_rows: {mnq_rows}"
    )
    surfaced = next(r for r in mnq_rows if r.get("id") == "mnq_futures")
    assert surfaced["today_trades"] == 5
    assert surfaced["today_pnl"] == 2.0
    assert surfaced["today_win_rate"] == 100.0   # 5/5
    assert surfaced["confirmed"] is True
    assert surfaced["running"] is True
