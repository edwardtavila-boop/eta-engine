"""EVOLUTIONARY TRADING ALGO  //  tests.test_broker_routing_framework.

Tests for the v2 routing framework on top of
:mod:`eta_engine.scripts.broker_router`:

* v1 yaml loads unchanged (backwards-compat).
* v2 yaml parses ``defaults`` + ``failover`` blocks correctly.
* Resolution-order priority chain works as documented:
    per-bot pin > env override > asset-class default > v1 default > "ibkr".
* Failover chain is composed from primary + chain minus duplicates.
* Asset-class detection covers crypto / futures / equity edge cases.
* The :mod:`eta_engine.scripts.broker_router_validate` CLI passes for
  a clean config and catches an unknown-venue typo.

Design rules (mirrors test_broker_router.py)
--------------------------------------------
* pytest only — no pytest-asyncio.
* All filesystem fixtures via ``tmp_path``.
* Env var manipulation via ``monkeypatch``.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

# Skip cleanly if the implementation hasn't landed (mirrors sibling tests).
broker_router = pytest.importorskip(
    "eta_engine.scripts.broker_router",
    reason="broker_router module not yet implemented",
)


# ---------------------------------------------------------------------------
# YAML fixtures
# ---------------------------------------------------------------------------


_V1_YAML = """\
version: 1
default:
  venue: ibkr
  symbol_overrides:
    BTC:  { ibkr: BTCUSD, tasty: BTCUSDT }
    MNQ:  { ibkr: MNQ }
bots:
  btc_optimized: { venue: alpaca }
  mnq_v7:        { venue: ibkr }
"""

_PROP_TEST_YAML = """\
version: 2

defaults:
  futures: ibkr
  crypto: alpaca

failover:
  futures: [ibkr, tastytrade]

default:
  venue: ibkr
  symbol_overrides:
    MNQ:  { ibkr: MNQ, tradovate: MNQ }

prop_accounts:
  blusky_50k:
    venue: tradovate
    env: demo
    account_id_env: BLUSKY_TRADOVATE_ACCOUNT_ID
    creds_env_prefix: BLUSKY_
    bot_policy: explicit_allow
    policy_source: https://blog.blusky.pro/blusky-blog/attention-prop-firm-traders

bots:
  volume_profile_mnq:
    venue: tradovate
    account_alias: blusky_50k
"""

_V2_YAML = """\
version: 2

defaults:
  futures: ibkr
  crypto: alpaca

failover:
  crypto: [alpaca, tastytrade]
  futures: [ibkr, tastytrade]

default:
  venue: ibkr
  symbol_overrides:
    BTC:  { ibkr: BTCUSD, tasty: BTCUSDT, alpaca: "BTC/USD" }
    ETH:  { ibkr: ETHUSD, tasty: ETHUSDT, alpaca: "ETH/USD" }
    MNQ:  { ibkr: MNQ }

bots:
  btc_optimized: { venue: alpaca }
  mnq_v7:        { venue: ibkr }
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "routing.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestYamlSchema:
    def test_v1_yaml_backcompat_loads_clean(self, tmp_path: Path) -> None:
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V1_YAML))
        # v1 fields all parse.
        assert cfg.default_venue == "ibkr"
        assert "btc_optimized" in cfg.per_bot
        assert cfg.symbol_overrides["BTC"]["ibkr"] == "BTCUSD"
        # v2-only fields default to empty mappings (no crash).
        assert cfg.asset_class_defaults == {}
        assert cfg.failover_chains == {}
        # Detected version reflects the file.
        assert cfg.version == 1

    def test_v2_yaml_loads_with_defaults_and_failover(self, tmp_path: Path) -> None:
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V2_YAML))
        assert cfg.version == 2
        # Per-asset-class defaults parsed and lower-cased.
        assert cfg.asset_class_defaults["futures"] == "ibkr"
        assert cfg.asset_class_defaults["crypto"] == "alpaca"
        # Failover chains parsed in order, lower-cased.
        assert cfg.failover_chains["crypto"] == ("alpaca", "tastytrade")
        assert cfg.failover_chains["futures"] == ("ibkr", "tastytrade")
        # v1 fields still present (dual-schema).
        assert cfg.default_venue == "ibkr"
        assert cfg.per_bot["btc_optimized"]["venue"] == "alpaca"

    def test_v2_yaml_loads_prop_account_aliases_for_tradovate_testing(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _PROP_TEST_YAML))
        assert cfg.prop_accounts["blusky_50k"]["venue"] == "tradovate"
        assert cfg.prop_accounts["blusky_50k"]["account_id_env"] == "BLUSKY_TRADOVATE_ACCOUNT_ID"
        account = cfg.prop_account_for("volume_profile_mnq")
        assert account is not None
        assert account["alias"] == "blusky_50k"
        assert account["creds_env_prefix"] == "BLUSKY_"


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


class TestVenueResolutionOrder:
    def test_venue_for_per_bot_override_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Even with a hostile env override AND a different asset-class
        # default, the explicit per-bot pin must win.
        monkeypatch.setenv("ETA_VENUE_OVERRIDE_CRYPTO", "tastytrade")
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V2_YAML))
        assert cfg.venue_for("btc_optimized", symbol="BTC") == "alpaca"

    def test_venue_for_env_override_beats_yaml_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bot is unlisted, so per-bot doesn't apply. Env override should
        # beat the yaml asset-class default ("alpaca").
        monkeypatch.setenv("ETA_VENUE_OVERRIDE_CRYPTO", "tastytrade")
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V2_YAML))
        assert cfg.venue_for("never_seen", symbol="BTC") == "tastytrade"
        # Asset class is honored — env override only fires for crypto here.
        assert cfg.venue_for("never_seen", symbol="MNQ") == "ibkr"

    def test_venue_for_asset_class_default_when_no_per_bot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No env override, no per-bot pin -> asset-class default applies.
        monkeypatch.delenv("ETA_VENUE_OVERRIDE_CRYPTO", raising=False)
        monkeypatch.delenv("ETA_VENUE_OVERRIDE_FUTURES", raising=False)
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V2_YAML))
        assert cfg.venue_for("never_seen", symbol="BTC") == "alpaca"
        assert cfg.venue_for("never_seen", symbol="MNQ") == "ibkr"

    def test_venue_for_v1_back_compat_no_symbol_emits_deprecation(
        self,
        tmp_path: Path,
    ) -> None:
        # The no-symbol form must still resolve (back-compat for old
        # callers) but it MUST emit a DeprecationWarning so we can grep
        # for the call sites later.
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V2_YAML))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert cfg.venue_for("never_seen") == "ibkr"
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)

    def test_venue_for_falls_back_to_ibkr_when_nothing_configured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An empty config (only the version key) must still resolve to
        # the documented last-resort default.
        empty = "version: 2\n"
        monkeypatch.delenv("ETA_VENUE_OVERRIDE_CRYPTO", raising=False)
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, empty))
        assert cfg.venue_for("any_bot", symbol="BTC") == "ibkr"


# ---------------------------------------------------------------------------
# Failover chain
# ---------------------------------------------------------------------------


class TestFailoverChain:
    def test_failover_chain_resolves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ETA_VENUE_OVERRIDE_CRYPTO", raising=False)
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V2_YAML))
        # btc_optimized pinned to alpaca; chain is [alpaca, tastytrade];
        # primary already first => no duplicate.
        chain = cfg.failover_chain("btc_optimized", symbol="BTC")
        assert chain == ("alpaca", "tastytrade")

    def test_failover_chain_promotes_per_bot_primary_to_front(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If a bot's per-bot pin disagrees with the chain primary, the
        # bot's pin must come FIRST and the original primary follows
        # (de-duplicated).
        body = _V2_YAML.replace(
            "btc_optimized: { venue: alpaca }",
            "btc_optimized: { venue: tastytrade }",
        )
        monkeypatch.delenv("ETA_VENUE_OVERRIDE_CRYPTO", raising=False)
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, body))
        chain = cfg.failover_chain("btc_optimized", symbol="BTC")
        assert chain[0] == "tastytrade"
        # alpaca still appears as a downstream fallback.
        assert "alpaca" in chain
        # No duplicate of tastytrade.
        assert chain.count("tastytrade") == 1

    def test_failover_chain_collapses_to_primary_when_no_chain(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ETA_VENUE_OVERRIDE_CRYPTO", raising=False)
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V1_YAML))
        # v1 has no failover block at all.
        chain = cfg.failover_chain("btc_optimized", symbol="BTC")
        assert chain == ("alpaca",)


# ---------------------------------------------------------------------------
# Asset-class detection
# ---------------------------------------------------------------------------


class TestAssetClassDetection:
    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            # Crypto
            ("BTC", "crypto"),
            ("ETH", "crypto"),
            ("SOL", "crypto"),
            ("XRP", "crypto"),
            ("BTCUSDT", "crypto"),
            ("ETHUSD", "crypto"),
            ("BTC/USD", "crypto"),
            ("eth/usd", "crypto"),  # case-insensitive
            ("DOGEUSD", "crypto"),
            # Futures roots
            ("MNQ", "futures"),
            ("ES", "futures"),
            ("NQ", "futures"),
            ("CL", "futures"),
            ("GC", "futures"),
            ("MNQ1", "futures"),  # supervisor numeric form
            ("MNQM6", "futures"),  # CME month-coded form
            ("ESH26", "futures"),  # full month+yy
            ("/MNQ", "futures"),  # slash-prefix bare root
            # Equity
            ("SPY", "equity"),
            ("AAPL", "equity"),
            ("VOO", "equity"),
            # Edge cases
            ("", "equity"),
        ],
    )
    def test_asset_class_detection_for_known_symbols(
        self,
        symbol: str,
        expected: str,
    ) -> None:
        assert broker_router._asset_class_for_symbol(symbol) == expected


# ---------------------------------------------------------------------------
# Validator CLI
# ---------------------------------------------------------------------------


class _StubVenue:
    """Mini venue stand-in; the validator only reads .name + has_credentials()."""

    def __init__(self, name: str, *, with_creds: bool = True) -> None:
        self.name = name
        self._creds = with_creds

    def has_credentials(self) -> bool:
        return self._creds

    async def place_order(self, request):  # noqa: ANN001 — duck-typed
        raise NotImplementedError


class _StubSmartRouter:
    """Minimal SmartRouter stand-in for the validator's adapter lookup."""

    def __init__(self, venues: dict[str, _StubVenue]) -> None:
        self._venue_map = dict(venues)
        # Empty circuits dict — validator only reads it through the
        # broker_router heartbeat surface; not exercised in these tests.
        self._venue_circuits: dict[str, object] = {}

    def _venue_by_name(self, name: str) -> _StubVenue | None:
        return self._venue_map.get(name)


class TestValidator:
    def test_validator_passes_for_clean_config(self, tmp_path: Path) -> None:
        from eta_engine.scripts import broker_router_validate as validator

        cfg = broker_router.RoutingConfig.load(_write(tmp_path, _V2_YAML))
        venues = {
            "alpaca": _StubVenue("alpaca", with_creds=True),
            "ibkr": _StubVenue("ibkr", with_creds=True),
            "tastytrade": _StubVenue("tastytrade", with_creds=False),
        }
        bot_pairs = [
            ("btc_optimized", "BTC"),
            ("mnq_v7", "MNQ"),
        ]
        results = validator.check_routing_config(
            cfg=cfg,
            bot_pairs=bot_pairs,
            smart_router=_StubSmartRouter(venues),
        )
        assert all(r.ok for r in results), [r.line() for r in results if not r.ok]
        # Spot-check the mapped symbol is what we expect.
        by_bot = {r.bot_id: r for r in results}
        assert by_bot["btc_optimized"].mapped == "BTC/USD"
        assert by_bot["mnq_v7"].mapped == "MNQ"

    def test_validator_catches_unknown_venue(self, tmp_path: Path) -> None:
        from eta_engine.scripts import broker_router_validate as validator

        # A typo in the config: "alpacca" instead of "alpaca".
        bad = _V2_YAML.replace(
            "btc_optimized: { venue: alpaca }",
            "btc_optimized: { venue: alpacca }",
        )
        cfg = broker_router.RoutingConfig.load(_write(tmp_path, bad))
        venues = {
            "alpaca": _StubVenue("alpaca", with_creds=True),
            "ibkr": _StubVenue("ibkr", with_creds=True),
            "tastytrade": _StubVenue("tastytrade", with_creds=False),
        }
        bot_pairs = [("btc_optimized", "BTC"), ("mnq_v7", "MNQ")]
        results = validator.check_routing_config(
            cfg=cfg,
            bot_pairs=bot_pairs,
            smart_router=_StubSmartRouter(venues),
        )
        by_bot = {r.bot_id: r for r in results}
        # The typo'd one fails with a useful reason; the other passes.
        assert not by_bot["btc_optimized"].ok
        assert "alpacca" in by_bot["btc_optimized"].reason or "unknown venue" in by_bot["btc_optimized"].reason
        assert by_bot["mnq_v7"].ok
