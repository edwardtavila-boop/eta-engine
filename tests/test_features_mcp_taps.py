"""Tests for features.mcp_taps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.features.mcp_taps import (
    OnchainSnapshot,
    SentimentSnapshot,
    blockscout_snapshot,
    lunarcrush_snapshot,
    use_mcp_taps_enabled,
)

if TYPE_CHECKING:
    import pytest


class FakeMcp:
    """In-memory stand-in for a real MCP wrapper."""

    def __init__(
        self,
        *,
        transfers: list[dict[str, Any]] | None = None,
        galaxy: float = 55.0,
        alt_rank: int = 42,
        social_volume: dict[str, Any] | None = None,
        fng: int = 50,
    ) -> None:
        self._transfers = transfers or []
        self._galaxy = galaxy
        self._alt_rank = alt_rank
        self._social_volume = social_volume or {}
        self._fng = fng

    def get_address_info(self, *, address: str, chain_id: int) -> dict[str, Any]:
        return {"address": address, "chain_id": chain_id}

    def get_token_transfers(self, *, address: str, chain_id: int, age_hours: int = 24) -> list[dict[str, Any]]:
        return self._transfers

    def get_galaxy_score(self, *, symbol: str) -> float:
        return self._galaxy

    def get_alt_rank(self, *, symbol: str) -> int:
        return self._alt_rank

    def get_social_volume(self, *, symbol: str) -> dict[str, Any]:
        return self._social_volume

    def get_fear_greed_index(self) -> int:
        return self._fng


class TestFeatureFlag:
    def test_default_disabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("APEX_USE_MCP_TAPS", raising=False)
        assert use_mcp_taps_enabled() is False

    def test_env_flag_enables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APEX_USE_MCP_TAPS", "1")
        assert use_mcp_taps_enabled() is True

    def test_env_flag_case_insensitive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("APEX_USE_MCP_TAPS", "TRUE")
        assert use_mcp_taps_enabled() is True


class TestBlockscoutSnapshot:
    def test_no_transfers_gives_neutral_snapshot(self):
        mcp = FakeMcp(transfers=[])
        snap = blockscout_snapshot("ETH", address="0xdead", mcp=mcp)
        assert isinstance(snap, OnchainSnapshot)
        assert snap.whale_transfers == 0
        assert snap.exchange_netflow_usd == 0.0

    def test_counts_whales_above_threshold(self):
        transfers = [
            {
                "total": {"value": "2000000000000000000000000", "decimals": 18},
                "token": {"exchange_rate": 1.0},
                "from": {"hash": "0xsender"},
                "to": {"hash": "0xdead"},
            },
            # small transfer, below threshold
            {
                "total": {"value": "1000000000000000000", "decimals": 18},
                "token": {"exchange_rate": 1.0},
                "from": {"hash": "0xsender2"},
                "to": {"hash": "0xdead"},
            },
        ]
        mcp = FakeMcp(transfers=transfers)
        snap = blockscout_snapshot("ETH", address="0xdead", mcp=mcp)
        assert snap.whale_transfers == 1

    def test_netflow_positive_when_inbound(self):
        transfers = [
            {
                "total": {"value": "5000000000000000000000000", "decimals": 18},
                "token": {"exchange_rate": 1.0},
                "from": {"hash": "0xsender"},
                "to": {"hash": "0xdead"},
            },
        ]
        mcp = FakeMcp(transfers=transfers)
        snap = blockscout_snapshot("ETH", address="0xdead", mcp=mcp)
        assert snap.exchange_netflow_usd > 0

    def test_netflow_negative_when_outbound(self):
        transfers = [
            {
                "total": {"value": "5000000000000000000000000", "decimals": 18},
                "token": {"exchange_rate": 1.0},
                "from": {"hash": "0xdead"},
                "to": {"hash": "0xreceiver"},
            },
        ]
        mcp = FakeMcp(transfers=transfers)
        snap = blockscout_snapshot("ETH", address="0xdead", mcp=mcp)
        assert snap.exchange_netflow_usd < 0

    def test_counts_unique_counterparties_as_active_addr(self):
        transfers = [
            {
                "total": {"value": "1000000", "decimals": 0},
                "token": {"exchange_rate": 1.0},
                "from": {"hash": f"0xaddr{i}"},
                "to": {"hash": "0xdead"},
            }
            for i in range(10)
        ]
        mcp = FakeMcp(transfers=transfers)
        snap = blockscout_snapshot("ETH", address="0xdead", mcp=mcp)
        assert snap.active_addresses == 10


class TestLunarCrushSnapshot:
    def test_basic_snapshot(self):
        mcp = FakeMcp(
            galaxy=82.5,
            alt_rank=7,
            social_volume={"social_volume": 12500, "social_volume_baseline": 5000},
            fng=22,
        )
        snap = lunarcrush_snapshot("BTC", mcp=mcp)
        assert isinstance(snap, SentimentSnapshot)
        assert snap.galaxy_score == 82.5
        assert snap.alt_rank == 7
        assert snap.social_volume == 12500
        assert snap.social_volume_baseline == 5000
        assert snap.fear_greed == 22

    def test_falls_back_when_baseline_missing(self):
        mcp = FakeMcp(
            galaxy=50.0,
            alt_rank=100,
            social_volume={"social_volume": 1000},  # no baseline
            fng=50,
        )
        snap = lunarcrush_snapshot("ETH", mcp=mcp)
        assert snap.social_volume_baseline == 500  # half of social_volume

    def test_asset_symbol_normalization(self):
        mcp = FakeMcp()
        snap = lunarcrush_snapshot("ETHUSDT", mcp=mcp)
        # Internal symbol becomes ETH; the snapshot still records the original asset
        assert snap.asset == "ETHUSDT"

    def test_to_ctx_matches_sentiment_feature_schema(self):
        mcp = FakeMcp(
            galaxy=55.0,
            alt_rank=100,
            social_volume={"social_volume": 500},
            fng=60,
        )
        snap = lunarcrush_snapshot("BTC", mcp=mcp)
        ctx = snap.to_ctx()
        for key in (
            "asset",
            "galaxy_score",
            "alt_rank",
            "social_volume",
            "social_volume_baseline",
            "fear_greed",
        ):
            assert key in ctx


class TestOnchainSnapshotCtx:
    def test_to_ctx_matches_onchain_feature_schema(self):
        snap = OnchainSnapshot(
            asset="ETH",
            whale_transfers=3,
            whale_transfers_baseline=2,
            exchange_netflow_usd=500_000.0,
            active_addresses=1500,
            active_addresses_baseline=1000,
            active_addresses_delta=0.5,
        )
        ctx = snap.to_ctx()
        for key in (
            "asset",
            "whale_transfers",
            "whale_transfers_baseline",
            "exchange_netflow_usd",
            "active_addresses",
            "active_addresses_baseline",
            "active_addresses_delta",
        ):
            assert key in ctx
