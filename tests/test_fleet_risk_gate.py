"""Tests for safety.fleet_risk_gate — the fleet-wide daily-loss
aggregator. Covers the risk-sage 2026-04-27 spec: hard 3.5% fleet
budget enforced upstream of every order submission.
"""

from __future__ import annotations

import pytest

from eta_engine.safety.fleet_risk_gate import (
    DEFAULT_LIMIT_PCT,
    FleetRiskBreach,
    FleetRiskGate,
)


def _gate(equity: float = 100_000.0, **kwargs) -> FleetRiskGate:  # type: ignore[no-untyped-def]
    """Construct a gate with deterministic defaults; ignores env."""
    return FleetRiskGate(
        fleet_starting_equity_usd=equity,
        disabled=False,
        **kwargs,
    )


def test_construction_rejects_zero_or_negative_equity() -> None:
    with pytest.raises(ValueError, match="positive"):
        FleetRiskGate(fleet_starting_equity_usd=0.0, disabled=False)
    with pytest.raises(ValueError, match="positive"):
        FleetRiskGate(fleet_starting_equity_usd=-1.0, disabled=False)


def test_default_limit_is_3_5_pct_of_equity() -> None:
    g = _gate(equity=100_000.0)
    assert g.limit_usd() == pytest.approx(3500.0)
    assert pytest.approx(0.035) == DEFAULT_LIMIT_PCT


def test_explicit_override_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_FLEET_DAILY_LOSS_LIMIT_USD", "10000")
    g = _gate(equity=100_000.0, limit_usd_override=2_500.0)
    assert g.limit_usd() == pytest.approx(2_500.0)


def test_env_usd_takes_precedence_over_pct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_FLEET_DAILY_LOSS_LIMIT_USD", "5000")
    monkeypatch.setenv("APEX_FLEET_DAILY_LOSS_LIMIT_PCT", "0.10")  # 10k of equity
    g = _gate(equity=100_000.0)
    assert g.limit_usd() == pytest.approx(5_000.0)


def test_env_pct_used_when_usd_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEX_FLEET_DAILY_LOSS_LIMIT_USD", raising=False)
    monkeypatch.setenv("APEX_FLEET_DAILY_LOSS_LIMIT_PCT", "0.05")
    g = _gate(equity=100_000.0)
    assert g.limit_usd() == pytest.approx(5_000.0)


def test_garbage_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_FLEET_DAILY_LOSS_LIMIT_USD", "not_a_number")
    monkeypatch.setenv("APEX_FLEET_DAILY_LOSS_LIMIT_PCT", "also_garbage")
    g = _gate(equity=100_000.0)
    assert g.limit_usd() == pytest.approx(3_500.0)


def test_record_pnl_accumulates_per_bot_and_aggregate() -> None:
    g = _gate()
    g.record_pnl("btc_hybrid", -100.0)
    g.record_pnl("eth_perp", -200.0)
    g.record_pnl("btc_hybrid", -50.0)
    s = g.status()
    assert s["net_pnl_usd"] == pytest.approx(-350.0)
    assert s["per_bot_pnl_usd"]["btc_hybrid"] == pytest.approx(-150.0)
    assert s["per_bot_pnl_usd"]["eth_perp"] == pytest.approx(-200.0)


def test_is_tripped_false_under_budget() -> None:
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -3_000.0)  # under -3500
    assert g.is_tripped() is False


def test_is_tripped_true_just_past_limit() -> None:
    """A clear breach (1 USD past the limit). The exact-equality
    edge case is a floating-point artefact (0.035 * 100000 doesn't
    represent exactly), so the spec is "below -limit, not equal-to"
    and we test 1 USD below to make the behaviour deterministic."""
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -3_501.0)
    assert g.is_tripped() is True


def test_is_tripped_true_when_over_budget() -> None:
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -2_000.0)
    g.record_pnl("eth_perp", -2_000.0)
    assert g.is_tripped() is True


def test_require_ok_silent_under_budget() -> None:
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -1_000.0)
    g.require_ok()  # should not raise


def test_require_ok_raises_when_tripped() -> None:
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -2_000.0)
    g.record_pnl("eth_perp", -2_000.0)
    with pytest.raises(FleetRiskBreach) as exc_info:
        g.require_ok(bot_id="sol_perp")
    assert exc_info.value.net_pnl_usd == pytest.approx(-4_000.0)
    assert exc_info.value.limit_usd == pytest.approx(3_500.0)
    assert exc_info.value.bot_id == "sol_perp"


def test_disabled_gate_never_trips_or_raises() -> None:
    g = FleetRiskGate(fleet_starting_equity_usd=100_000.0, disabled=True)
    g.record_pnl("btc_hybrid", -100_000.0)
    assert g.is_tripped() is False
    g.require_ok()  # must not raise


def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_FLEET_RISK_DISABLED", "1")
    g = FleetRiskGate(fleet_starting_equity_usd=100_000.0)
    assert g.disabled is True
    g.record_pnl("btc_hybrid", -100_000.0)
    g.require_ok()  # would raise if not disabled


def test_status_payload_shape() -> None:
    g = _gate(equity=50_000.0)
    g.record_pnl("btc_hybrid", -100.0)
    g.record_pnl("eth_perp", 50.0)
    s = g.status()
    assert set(s.keys()) >= {
        "today_utc", "net_pnl_usd", "limit_usd", "tripped",
        "per_bot_pnl_usd", "disabled", "fleet_starting_equity_usd",
    }
    assert s["fleet_starting_equity_usd"] == pytest.approx(50_000.0)
    assert s["tripped"] is False  # -50 net < -1750 limit


def test_reset_clears_running_aggregate() -> None:
    g = _gate()
    g.record_pnl("btc_hybrid", -1_000.0)
    g.reset()
    s = g.status()
    assert s["net_pnl_usd"] == pytest.approx(0.0)
    assert s["per_bot_pnl_usd"] == {}


def test_profits_offset_losses() -> None:
    """Win-on-one-bot, lose-on-another nets to a smaller drawdown."""
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -3_000.0)
    g.record_pnl("eth_perp", +1_000.0)
    assert g.is_tripped() is False  # net -2000, under -3500


def test_breach_exception_carries_structured_attrs() -> None:
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -5_000.0)
    try:
        g.require_ok(bot_id="btc_hybrid")
    except FleetRiskBreach as e:
        assert isinstance(e.net_pnl_usd, float)
        assert isinstance(e.limit_usd, float)
        assert e.bot_id == "btc_hybrid"
        assert "fleet daily-loss limit breached" in str(e)
    else:
        pytest.fail("FleetRiskBreach should have raised")


def test_register_and_get_singleton_round_trip() -> None:
    from eta_engine.safety.fleet_risk_gate import (
        get_fleet_risk_gate,
        register_fleet_risk_gate,
    )
    g = _gate()
    register_fleet_risk_gate(g)
    try:
        assert get_fleet_risk_gate() is g
    finally:
        register_fleet_risk_gate(None)
    assert get_fleet_risk_gate() is None


def test_assert_fleet_within_budget_noop_when_unregistered() -> None:
    from eta_engine.safety.fleet_risk_gate import (
        assert_fleet_within_budget,
        register_fleet_risk_gate,
    )
    register_fleet_risk_gate(None)
    # Should not raise; this is the paper / unit-test default path
    assert_fleet_within_budget(bot_id="any")


def test_assert_fleet_within_budget_raises_when_tripped() -> None:
    from eta_engine.safety.fleet_risk_gate import (
        FleetRiskBreach,
        assert_fleet_within_budget,
        register_fleet_risk_gate,
    )
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -5_000.0)  # past the -3500 limit
    register_fleet_risk_gate(g)
    try:
        with pytest.raises(FleetRiskBreach) as exc_info:
            assert_fleet_within_budget(bot_id="sol_perp")
        assert exc_info.value.bot_id == "sol_perp"
    finally:
        register_fleet_risk_gate(None)


def test_assert_fleet_within_budget_silent_under_budget() -> None:
    from eta_engine.safety.fleet_risk_gate import (
        assert_fleet_within_budget,
        register_fleet_risk_gate,
    )
    g = _gate(equity=100_000.0)
    g.record_pnl("btc_hybrid", -1_000.0)
    register_fleet_risk_gate(g)
    try:
        assert_fleet_within_budget(bot_id="any")  # should not raise
    finally:
        register_fleet_risk_gate(None)


def test_thread_safety_smoke() -> None:
    """Concurrent record_pnl from multiple threads should not corrupt
    the aggregate. We can't deterministically detect torn writes, but
    the lock should make the totals match the sum of all deltas."""
    import threading

    g = _gate(equity=1_000_000.0)
    n_threads = 8
    pnl_per_thread = -10.0
    iterations = 100

    def hammer(bot_id: str) -> None:
        for _ in range(iterations):
            g.record_pnl(bot_id, pnl_per_thread)

    threads = [
        threading.Thread(target=hammer, args=(f"bot_{i}",))
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = pnl_per_thread * iterations * n_threads
    assert g.status()["net_pnl_usd"] == pytest.approx(expected)
