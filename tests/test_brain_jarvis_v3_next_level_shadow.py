from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_v3.next_level.shadow import (
    ShadowLedger,
    ShadowStatus,
    shadow_from_denied_request,
)


def test_shadow_ledger_resolves_long_target_and_reports_regret(tmp_path) -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    ledger = ShadowLedger()
    ledger.add(
        shadow_from_denied_request(
            request_id="req1",
            subsystem="bot.mnq",
            symbol="MNQ",
            side="LONG",
            entry_px=100.0,
            stop_px=99.0,
            target_px=102.0,
            now=now,
        )
    )

    changed = ledger.tick(price_lookup={"MNQ": 103.0}, now=now + timedelta(minutes=30))
    summary = ledger.regret(window_hours=1, now=now + timedelta(minutes=31))

    assert changed == ["req1"]
    assert ledger.get("req1").status is ShadowStatus.CLOSED  # type: ignore[union-attr]
    assert ledger.get("req1").realized_r == 2.0  # type: ignore[union-attr]
    assert summary.severity == "YELLOW"
    assert summary.cumulative_r == 2.0


def test_shadow_ledger_expires_unresolved_trade_and_round_trips(tmp_path) -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    path = tmp_path / "shadow.json"
    ledger = ShadowLedger()
    ledger.add(
        shadow_from_denied_request(
            request_id="req2",
            subsystem="bot.eth",
            symbol="ETH",
            side="SHORT",
            entry_px=100.0,
            stop_px=105.0,
            target_px=90.0,
            now=now,
        )
    )

    changed = ledger.tick(price_lookup={"ETH": 101.0}, now=now + timedelta(hours=5))
    ledger.save(path)
    loaded = ShadowLedger.load(path)

    assert changed == ["req2"]
    assert loaded.get("req2").status is ShadowStatus.EXPIRED  # type: ignore[union-attr]
    assert loaded.get("req2").realized_r == -0.2  # type: ignore[union-attr]


def test_shadow_factory_computes_r_distance() -> None:
    trade = shadow_from_denied_request(
        request_id="req3",
        subsystem="bot.btc",
        symbol="BTC",
        side="LONG",
        entry_px=50_000,
        stop_px=49_500,
        target_px=51_000,
    )

    assert trade.r_distance == 500
    assert trade.jarvis_verdict == "DENIED"
