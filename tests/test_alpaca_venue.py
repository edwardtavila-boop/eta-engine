"""Tests for the Alpaca paper venue adapter.

Mirrors ``test_tastytrade_ibkr_venues.py``: focuses on the deterministic
paths (config, payload building, cost-basis pre-check, helpers, readiness)
without hitting the network. Live integration is exercised via the smoke
script invoked manually during operator verification.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from eta_engine.venues import (
    AlpacaConfig,
    AlpacaVenue,
    ConnectionStatus,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    alpaca_paper_readiness,
)
from eta_engine.venues.alpaca import (
    ALPACA_CRYPTO_MIN_COST_BASIS_USD,
    _alpaca_crypto_base,
    _alpaca_quantity,
    _alpaca_symbol,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_alpaca_config_reads_secret_files(tmp_path: Path) -> None:
    key_file = tmp_path / "alpaca_key.txt"
    secret_file = tmp_path / "alpaca_secret.txt"
    key_file.write_text("PKAAAAAAAAAAAAAAAAAA\n", encoding="utf-8")
    secret_file.write_text("SECRETSECRETSECRETSECRETSECRETSECRETSECRET\n", encoding="utf-8")

    # Override ETA_RUNTIME_ROOT so the default broker_paper.env auto-load
    # in the real workspace doesn't shadow the test env.
    config = AlpacaConfig.from_env(
        {
            "ETA_RUNTIME_ROOT": str(tmp_path),
            "ALPACA_API_KEY_ID_FILE": str(key_file),
            "ALPACA_API_SECRET_KEY_FILE": str(secret_file),
        }
    )

    assert config.api_key_id == "PKAAAAAAAAAAAAAAAAAA"
    assert config.api_secret_key.startswith("SECRET")
    assert config.missing_requirements() == []


def test_alpaca_config_missing_keys_marked_unready(tmp_path: Path) -> None:
    config = AlpacaConfig.from_env({"ETA_RUNTIME_ROOT": str(tmp_path)})
    missing = config.missing_requirements()

    assert "ALPACA_API_KEY_ID" in missing
    assert "ALPACA_API_SECRET_KEY" in missing


def test_alpaca_config_rejects_live_host_when_paper_required(tmp_path: Path) -> None:
    config = AlpacaConfig.from_env(
        {
            "ETA_RUNTIME_ROOT": str(tmp_path),
            "ALPACA_API_KEY_ID": "PK1",
            "ALPACA_API_SECRET_KEY": "SECRET1",
            "ALPACA_BASE_URL": "https://api.alpaca.markets",
        }
    )

    missing = config.missing_requirements()

    assert any("paper-api.alpaca.markets" in entry for entry in missing)


def test_alpaca_config_paper_host_check_can_be_disabled(tmp_path: Path) -> None:
    config = AlpacaConfig.from_env(
        {
            "ETA_RUNTIME_ROOT": str(tmp_path),
            "ALPACA_API_KEY_ID": "PK1",
            "ALPACA_API_SECRET_KEY": "SECRET1",
            "ALPACA_BASE_URL": "https://api.alpaca.markets",
            "ALPACA_REQUIRE_PAPER_HOST": "false",
        }
    )

    assert config.missing_requirements() == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_crypto_base_recognizes_supported_pairs() -> None:
    assert _alpaca_crypto_base("BTC") == "BTC"
    assert _alpaca_crypto_base("BTCUSD") == "BTC"
    assert _alpaca_crypto_base("BTCUSDT") == "BTC"
    assert _alpaca_crypto_base("BTC/USD") == "BTC"
    assert _alpaca_crypto_base("BTC-USD") == "BTC"
    assert _alpaca_crypto_base("ETH") == "ETH"
    assert _alpaca_crypto_base("SOL") == "SOL"
    assert _alpaca_crypto_base("XRP") == "XRP"
    assert _alpaca_crypto_base("AVAX") == "AVAX"
    assert _alpaca_crypto_base("LINK") == "LINK"
    assert _alpaca_crypto_base("DOGE") == "DOGE"


def test_crypto_base_rejects_non_crypto() -> None:
    assert _alpaca_crypto_base("MNQ") is None
    assert _alpaca_crypto_base("MNQM6") is None
    assert _alpaca_crypto_base("/MNQ") is None
    assert _alpaca_crypto_base("AAPL") is None
    assert _alpaca_crypto_base("ZZZ") is None
    assert _alpaca_crypto_base("") is None


def test_alpaca_symbol_formats_crypto_as_base_usd() -> None:
    assert _alpaca_symbol("BTCUSDT", is_crypto=True) == "BTC/USD"
    assert _alpaca_symbol("ETH/USD", is_crypto=True) == "ETH/USD"
    assert _alpaca_symbol("SOL", is_crypto=True) == "SOL/USD"


def test_alpaca_symbol_passes_equity_through() -> None:
    assert _alpaca_symbol("AAPL", is_crypto=False) == "AAPL"
    assert _alpaca_symbol("spy", is_crypto=False) == "SPY"


def test_alpaca_quantity_crypto_returns_decimal_string() -> None:
    assert _alpaca_quantity(0.001, is_crypto=True) == "0.001"
    assert _alpaca_quantity(0.5, is_crypto=True) == "0.5"
    assert _alpaca_quantity(1.0, is_crypto=True) == "1"
    # Trailing zeros trimmed.
    assert _alpaca_quantity(0.00100000, is_crypto=True) == "0.001"


def test_alpaca_quantity_equity_returns_int_string_when_whole() -> None:
    assert _alpaca_quantity(1.0, is_crypto=False) == "1"
    assert _alpaca_quantity(100.0, is_crypto=False) == "100"


def test_alpaca_quantity_preserves_exit_precision_no_round_up() -> None:
    """Exit orders must not request more qty than the position holds.

    Alpaca returns position quantities with up to 9-10 dp precision.
    A naive ``f"{qty:.8f}"`` rounds *to nearest*, so 0.002375228 becomes
    0.00237523 — which is LARGER than the position and gets HTTP 403
    ``insufficient balance``. This test pins the regression: position-
    size qty must serialize back without exceeding the input.
    """
    # Real example caught in paper smoke (2026-05-05): 0.002375228 BTC
    # position closing must serialize as <= 0.002375228, never 0.00237523.
    qty_in = 0.002375228
    qty_str = _alpaca_quantity(qty_in, is_crypto=True)

    from decimal import Decimal

    assert Decimal(qty_str) <= Decimal(str(qty_in)), (
        f"qty serialization rounded UP: {qty_in} -> {qty_str} (would request more than position holds)"
    )
    # And the shortest-decimal round-trip should match exactly for inputs
    # whose float repr is well-defined (most position sizes).
    assert qty_str == "0.002375228"


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def test_build_order_payload_crypto_market_uses_gtc_and_base_usd() -> None:
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        qty=0.001,
        order_type=OrderType.MARKET,
        client_order_id="cid-btc-1",
    )

    payload = venue.build_order_payload(req)

    assert payload["symbol"] == "BTC/USD"
    assert payload["qty"] == "0.001"
    assert payload["side"] == "buy"
    assert payload["type"] == "market"
    # Crypto must use GTC, not DAY.
    assert payload["time_in_force"] == "gtc"
    assert payload["client_order_id"] == "cid-btc-1"
    # Market orders do not carry limit_price.
    assert "limit_price" not in payload


def test_build_order_payload_crypto_limit_carries_limit_price() -> None:
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="ETH",
        side=Side.SELL,
        qty=0.5,
        order_type=OrderType.LIMIT,
        price=3500.0,
        client_order_id="cid-eth-1",
    )

    payload = venue.build_order_payload(req)

    assert payload["symbol"] == "ETH/USD"
    assert payload["side"] == "sell"
    assert payload["type"] == "limit"
    assert payload["limit_price"] == "3500.0"


def test_build_order_payload_truncates_long_client_order_id() -> None:
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    long_cid = "x" * 80
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.001,
        order_type=OrderType.MARKET,
        client_order_id=long_cid,
    )

    payload = venue.build_order_payload(req)

    # Alpaca caps client_order_id at 48 chars.
    assert len(payload["client_order_id"]) == 48


# ---------------------------------------------------------------------------
# Cost-basis pre-check
# ---------------------------------------------------------------------------


def test_place_order_rejects_crypto_below_min_cost_basis() -> None:
    """Crypto order with est_cost_basis < $10 rejects without network round-trip."""
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.0001,
        order_type=OrderType.LIMIT,
        price=50.0,  # 0.0001 * 50 = $0.005, far below $10 minimum
        client_order_id="cb-test",
    )

    result = asyncio.run(venue.place_order(req))

    assert result.status is OrderStatus.REJECTED
    assert result.raw["reason"] == "alpaca_min_cost_basis"
    assert result.raw["min_cost_basis_usd"] == ALPACA_CRYPTO_MIN_COST_BASIS_USD
    assert result.raw["est_cost_basis_usd"] == 0.005


def test_place_order_passes_cost_basis_check_when_above_minimum() -> None:
    """qty * price >= $10 passes the pre-check (still degrades since no network)."""
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.0005,
        order_type=OrderType.LIMIT,
        price=80_000.0,  # 0.0005 * 80000 = $40, well above $10
        client_order_id="cb-pass",
    )

    result = asyncio.run(venue.place_order(req))

    # Without httpx + paper-api host, this falls through to mock OPEN —
    # the important assertion is we did NOT see the cost-basis reject path.
    assert result.raw.get("reason") != "alpaca_min_cost_basis"


def test_place_order_skips_cost_basis_check_for_market_orders() -> None:
    """Market orders have no limit_price so pre-check can't run; let server enforce."""
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.0001,
        order_type=OrderType.MARKET,
        client_order_id="market-test",
    )

    result = asyncio.run(venue.place_order(req))

    # No client-side reject for market — the broker side will enforce on POST.
    assert result.raw.get("reason") != "alpaca_min_cost_basis"


# ---------------------------------------------------------------------------
# Connection report (without network — connect() probes /v2/account but with
# missing creds it short-circuits before any HTTP)
# ---------------------------------------------------------------------------


def test_connect_reports_stub_without_credentials() -> None:
    venue = AlpacaVenue(AlpacaConfig())

    report = asyncio.run(venue.connect())

    assert report.venue == "alpaca"
    assert report.status is ConnectionStatus.STUBBED
    assert report.creds_present is False
    assert "ALPACA_API_KEY_ID" in report.details["missing"]
    assert report.details["mode"] == "paper"


# ---------------------------------------------------------------------------
# Readiness summary
# ---------------------------------------------------------------------------


def test_readiness_summary_unready_without_keys(tmp_path: Path) -> None:
    summary = alpaca_paper_readiness({"ETA_RUNTIME_ROOT": str(tmp_path)})

    assert summary["adapter_available"] is True
    assert summary["ready"] is False
    assert summary["mode"] == "paper"
    assert "ALPACA_API_KEY_ID" in summary["missing"]


def test_readiness_summary_includes_min_cost_basis_and_supported_bases(tmp_path: Path) -> None:
    summary = alpaca_paper_readiness(
        {
            "ETA_RUNTIME_ROOT": str(tmp_path),
            "ALPACA_API_KEY_ID": "PK1",
            "ALPACA_API_SECRET_KEY": "SECRET1",
        }
    )

    assert summary["ready"] is True
    assert summary["min_cost_basis_usd"] == ALPACA_CRYPTO_MIN_COST_BASIS_USD
    # All 7 of our crypto bot bases must appear in the supported list so
    # the dashboard can rule out routing mismatches at a glance.
    bases = set(summary["supported_crypto_bases"])
    for required in ("BTC", "ETH", "SOL", "XRP", "AVAX", "LINK", "DOGE"):
        assert required in bases, f"{required} missing from alpaca supported bases"


# ---------------------------------------------------------------------------
# Bracket attachment (server-side OCO via order_class=bracket)
# ---------------------------------------------------------------------------


def test_build_payload_no_bracket_for_crypto_even_with_stop_target() -> None:
    """Crypto orders MUST NOT carry order_class=bracket.

    Alpaca rejects crypto with HTTP 422
    ``{"code":42210000,"message":"crypto orders not allowed for advanced
    order_class: otoco"}`` if any advanced order_class is sent. Caught
    live 2026-05-06. The supervisor's tick-level _maybe_exit path
    manages stop/target for crypto positions instead — same pattern
    used for IBKR PAXOS crypto, which has the same constraint.

    Stop/target ARE still required on the OrderRequest (the
    naked-entry-blocked check enforces them) so the supervisor can
    drive its local exit logic.
    """
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.001,
        order_type=OrderType.LIMIT,
        price=80_500.0,
        stop_price=79_000.0,
        target_price=82_000.0,
        client_order_id="bracket-buy-crypto",
    )

    payload = venue.build_order_payload(req)

    # Crypto path must NOT include any bracket fields.
    assert "order_class" not in payload
    assert "take_profit" not in payload
    assert "stop_loss" not in payload
    # Parent leg unchanged.
    assert payload["symbol"] == "BTC/USD"
    assert payload["side"] == "buy"
    assert payload["limit_price"] == "80500.0"


def test_build_payload_attaches_bracket_for_equity_with_stop_and_target() -> None:
    """Equity (non-crypto) entries with stop+target DO get a server-side bracket.

    Alpaca only forbids advanced order_class on crypto orders; equity
    orders accept order_class=bracket cleanly. This pins the behavior
    so the bracket attachment doesn't regress for the equity path
    when the crypto exception is enforced.
    """
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="SPY",
        side=Side.BUY,
        qty=10,
        order_type=OrderType.LIMIT,
        price=500.0,
        stop_price=495.0,
        target_price=510.0,
        client_order_id="bracket-buy-equity",
    )

    payload = venue.build_order_payload(req)

    assert payload["order_class"] == "bracket"
    assert payload["take_profit"] == {"limit_price": "510.00"}
    assert payload["stop_loss"] == {"stop_price": "495.00"}
    assert payload["symbol"] == "SPY"
    assert payload["side"] == "buy"
    assert payload["limit_price"] == "500.0"


def test_build_payload_no_bracket_for_reduce_only_exit() -> None:
    """Exits (reduce_only=True) must NOT attach a bracket.

    A bracket on an exit would either reject server-side (Alpaca rejects
    order_class=bracket on reduce_only) or, worse, place fresh OCO
    siblings that could re-open the position. Exit orders close the
    position with a single bare leg.
    """
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="BTC",
        side=Side.SELL,
        qty=0.001,
        order_type=OrderType.MARKET,
        # Even if stop/target somehow leak onto an exit, do not attach.
        stop_price=79_000.0,
        target_price=82_000.0,
        reduce_only=True,
        client_order_id="exit-1",
    )

    payload = venue.build_order_payload(req)

    assert "order_class" not in payload
    assert "take_profit" not in payload
    assert "stop_loss" not in payload


def test_place_order_rejects_naked_entry() -> None:
    """Non-reduce-only entries without bracket fields must be rejected.

    Mirrors the IBKR live-venue safety contract from
    eta_engine/venues/base.py:OrderRequest. The supervisor MUST always
    set stop_price + target_price on entries; if they are missing, the
    venue layer fails closed rather than placing a naked position.
    """
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.001,
        order_type=OrderType.LIMIT,
        price=80_500.0,
        # No stop_price / target_price → naked entry.
        client_order_id="naked-entry",
    )

    result = asyncio.run(venue.place_order(req))

    assert result.status is OrderStatus.REJECTED
    assert result.raw["reason"] == "naked_entry_blocked"
    assert result.raw["stop_price"] is None
    assert result.raw["target_price"] is None


def test_bracket_geometry_buy_validates_stop_below_entry_target_above() -> None:
    """Invalid BUY bracket (stop above entry, target below) is rejected.

    A BUY bracket must satisfy stop < entry < target; otherwise the STP
    converts to a MKT immediately on submission (stop already breached)
    and the TP fills the moment price ticks up to entry — a guaranteed
    losing round-trip. Catch the inversion before POST so the operator
    gets a deterministic reject instead of a torn-up position.
    """
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    # BUY with stop ABOVE entry and target BELOW entry — completely inverted.
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.001,
        order_type=OrderType.LIMIT,
        price=80_500.0,
        stop_price=82_000.0,  # WRONG: should be below entry
        target_price=79_000.0,  # WRONG: should be above entry
        client_order_id="bad-buy-bracket",
    )

    result = asyncio.run(venue.place_order(req))

    assert result.status is OrderStatus.REJECTED
    assert result.raw["reason"] == "bracket_geometry_invalid"
    assert result.raw["side"] == "BUY"
    assert "stop < entry < target" in result.raw["detail"]
