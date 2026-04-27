"""Staking adapter tests — lido/jito/flare/ethena + web3_client + apy_tracker.

Covers:
* :func:`eta_engine.staking.web3_client.read_balance` fallback paths
  (no RPC, no wallet, web3 uninstalled, RPC exception).
* :func:`eta_engine.staking.web3_client.build_contract_call` payload shape.
* :class:`eta_engine.staking.apy_tracker.ApyTracker` cache hit/miss,
  pool-filter matching, network-error fallback, shared-singleton lifecycle.
* All four adapters: stake/unstake validation, in-memory balance path,
  on-chain balance path (with monkey-patched read_balance), APY live-vs-fallback,
  Lido restake premium, Ethena 7-day cooldown payload.
"""

from __future__ import annotations

from typing import Any

import pytest

from eta_engine.staking import apy_tracker as apy_module
from eta_engine.staking import web3_client
from eta_engine.staking.apy_tracker import ApyTracker
from eta_engine.staking.ethena import EthenaAdapter
from eta_engine.staking.flare import FlareAdapter
from eta_engine.staking.jito import JitoAdapter
from eta_engine.staking.lido import LidoAdapter
from eta_engine.staking.web3_client import build_contract_call, read_balance

# ---------------------------------------------------------------------------
# web3_client
# ---------------------------------------------------------------------------


def test_read_balance_no_rpc_returns_none() -> None:
    assert read_balance(None, "0xwallet", "0xtoken") is None


def test_read_balance_no_wallet_returns_none() -> None:
    assert read_balance("https://rpc", None, "0xtoken") is None


def test_read_balance_web3_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web3_client, "_load_web3", lambda: None)
    assert read_balance("https://rpc", "0xwallet", "0xtoken") is None


def test_read_balance_rpc_exception_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeWeb3:
        @staticmethod
        def HTTPProvider(_url: str) -> str:  # noqa: N802 - matches web3 API
            return "provider"

        @staticmethod
        def to_checksum_address(addr: str) -> str:
            return addr

        def __init__(self, _provider: str) -> None:
            raise RuntimeError("RPC down")

    monkeypatch.setattr(web3_client, "_load_web3", lambda: _FakeWeb3)
    assert read_balance("https://rpc", "0xwallet", "0xtoken") is None


def test_read_balance_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake Web3 returns a wei balance; we divide by 10**18 to get 1.5 ETH."""

    class _FakeContract:
        def __init__(self, *, address: str, abi: list[dict[str, Any]]) -> None:  # noqa: ARG002
            self.address = address
            self.functions = self

        def balanceOf(self, _wallet: str) -> Any:  # noqa: N802, ANN401 - matches ERC-20 callable shape
            class _Call:
                def call(self) -> int:
                    return 1_500_000_000_000_000_000  # 1.5 * 1e18

            return _Call()

        def decimals(self) -> Any:  # noqa: ANN401 - ERC-20 callable shape
            class _Call:
                def call(self) -> int:
                    return 18

            return _Call()

    class _FakeEth:
        def contract(self, **kwargs: Any) -> _FakeContract:  # noqa: ANN401 - w3.eth.contract shape
            return _FakeContract(**kwargs)

    class _FakeWeb3:
        def __init__(self, _provider: str) -> None:
            self.eth = _FakeEth()

        @staticmethod
        def HTTPProvider(_url: str) -> str:  # noqa: N802 - matches web3 API
            return "provider"

        @staticmethod
        def to_checksum_address(addr: str) -> str:
            return addr

    monkeypatch.setattr(web3_client, "_load_web3", lambda: _FakeWeb3)
    result = read_balance("https://rpc", "0xwallet", "0xtoken")
    assert result == pytest.approx(1.5)


def test_build_contract_call_shape() -> None:
    payload = build_contract_call("0xabc", "submit", 42, "foo")
    assert payload == {
        "contract": "0xabc",
        "function": "submit",
        "args": [42, "foo"],
        "kind": "contract_call",
    }


# ---------------------------------------------------------------------------
# ApyTracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apy_tracker_unknown_key_returns_none() -> None:
    tracker = ApyTracker()
    assert await tracker.get_apy("does-not-exist") is None
    await tracker.close()


@pytest.mark.asyncio
async def test_apy_tracker_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = ApyTracker()
    call_count = {"n": 0}

    async def _fake_fetch(_self: ApyTracker, _key: str) -> float | None:
        call_count["n"] += 1
        return 4.25

    monkeypatch.setattr(ApyTracker, "_fetch_apy", _fake_fetch)
    first = await tracker.get_apy("lido")
    second = await tracker.get_apy("lido")
    assert first == 4.25
    assert second == 4.25
    assert call_count["n"] == 1  # cached second call
    await tracker.close()


@pytest.mark.asyncio
async def test_apy_tracker_network_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = ApyTracker()

    async def _fake_fetch(_self: ApyTracker, _key: str) -> float | None:
        return None

    monkeypatch.setattr(ApyTracker, "_fetch_apy", _fake_fetch)
    assert await tracker.get_apy("lido") is None
    await tracker.close()


def test_apy_tracker_match_pool_picks_best() -> None:
    payload = {
        "data": [
            {"project": "lido", "chain": "Ethereum", "symbol": "STETH", "apy": 3.6},
            {"project": "lido", "chain": "Ethereum", "symbol": "STETH-FOO", "apy": 4.1},
            {"project": "rocket", "chain": "Ethereum", "symbol": "RETH", "apy": 5.0},  # shouldn't match
        ]
    }
    best = ApyTracker._match_pool(payload, {"project": "lido", "chain": "Ethereum", "symbol": "STETH"})
    assert best == 4.1


def test_apy_tracker_match_pool_no_match() -> None:
    payload = {"data": [{"project": "other", "chain": "x", "symbol": "y", "apy": 1.0}]}
    assert ApyTracker._match_pool(payload, {"project": "lido", "chain": "Ethereum", "symbol": "STETH"}) is None


@pytest.mark.asyncio
async def test_shared_tracker_singleton_lifecycle() -> None:
    t1 = apy_module.get_shared_tracker()
    t2 = apy_module.get_shared_tracker()
    assert t1 is t2
    await apy_module.close_shared_tracker()
    t3 = apy_module.get_shared_tracker()
    assert t3 is not t1  # fresh after close
    await apy_module.close_shared_tracker()


# ---------------------------------------------------------------------------
# LidoAdapter
# ---------------------------------------------------------------------------


class _StubTracker:
    """Minimal ApyTracker stand-in returning a preset value (or None)."""

    def __init__(self, apy: float | None) -> None:
        self._apy = apy

    async def get_apy(self, _key: str) -> float | None:
        return self._apy

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_lido_stake_positive_only() -> None:
    adapter = LidoAdapter(apy_tracker=_StubTracker(None))
    with pytest.raises(ValueError, match="must be positive"):
        await adapter.stake(-1.0)
    tx = await adapter.stake(2.5)
    assert tx.startswith("lido-stake-stub")
    assert await adapter.get_balance() == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_lido_unstake_rejects_overdraw() -> None:
    adapter = LidoAdapter(apy_tracker=_StubTracker(None))
    await adapter.stake(1.0)
    with pytest.raises(ValueError, match="Invalid unstake amount"):
        await adapter.unstake(5.0)


@pytest.mark.asyncio
async def test_lido_restake_eigenlayer_premium() -> None:
    base = LidoAdapter(apy_tracker=_StubTracker(None))
    restaked = LidoAdapter(restake_eigenlayer=True, apy_tracker=_StubTracker(None))
    assert await base.get_apy() == pytest.approx(3.8)
    assert await restaked.get_apy() == pytest.approx(3.8 + 1.5)


@pytest.mark.asyncio
async def test_lido_apy_live_value(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = LidoAdapter(apy_tracker=_StubTracker(4.25))
    assert await adapter.get_apy() == pytest.approx(4.25)


@pytest.mark.asyncio
async def test_lido_on_chain_balance_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When RPC + wallet are set AND read_balance returns non-None, use it."""
    from eta_engine.staking import lido as lido_module

    def _fake_read(*_args: Any, **_kwargs: Any) -> float | None:  # noqa: ANN401 - mirrors read_balance signature
        return 7.25

    monkeypatch.setattr(lido_module, "read_balance", _fake_read)
    adapter = LidoAdapter(
        rpc_url="https://eth.llamarpc.com",
        wallet_address="0xabc",
        apy_tracker=_StubTracker(None),
    )
    assert await adapter.get_balance() == pytest.approx(7.25)


# ---------------------------------------------------------------------------
# JitoAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jito_stake_unstake_cycle() -> None:
    adapter = JitoAdapter(apy_tracker=_StubTracker(None))
    await adapter.stake(10.0)
    assert await adapter.get_balance() == pytest.approx(10.0)
    await adapter.unstake(4.0)
    assert await adapter.get_balance() == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_jito_apy_live_falls_back() -> None:
    adapter = JitoAdapter(apy_tracker=_StubTracker(None))
    assert await adapter.get_apy() == pytest.approx(6.5)


@pytest.mark.asyncio
async def test_jito_spl_balance_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from eta_engine.staking import jito as jito_module

    def _fake_spl(*_args: Any, **_kwargs: Any) -> float | None:  # noqa: ANN401 - mirrors _fetch_spl_balance signature
        return 12.5

    monkeypatch.setattr(jito_module, "_fetch_spl_balance", _fake_spl)
    adapter = JitoAdapter(
        rpc_url="https://rpc.mainnet.sol",
        token_account="Tok1",
        apy_tracker=_StubTracker(None),
    )
    assert await adapter.get_balance() == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# FlareAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flare_stake_with_delegation(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    adapter = FlareAdapter(ftso_data_provider="0xdelegate", apy_tracker=_StubTracker(None))
    with caplog.at_level(logging.INFO, logger="eta_engine.staking.flare"):
        await adapter.stake(100.0)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "delegate=yes" in joined
    assert "steps=3" in joined


@pytest.mark.asyncio
async def test_flare_stake_without_delegation(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    adapter = FlareAdapter(apy_tracker=_StubTracker(None))
    with caplog.at_level(logging.INFO, logger="eta_engine.staking.flare"):
        await adapter.stake(100.0)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "delegate=no" in joined
    assert "steps=2" in joined


@pytest.mark.asyncio
async def test_flare_apy_live_value() -> None:
    adapter = FlareAdapter(apy_tracker=_StubTracker(5.2))
    assert await adapter.get_apy() == pytest.approx(5.2)


# ---------------------------------------------------------------------------
# EthenaAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ethena_stake_cycle() -> None:
    adapter = EthenaAdapter(apy_tracker=_StubTracker(None))
    await adapter.stake(10000.0)
    assert await adapter.get_balance() == pytest.approx(10000.0)
    tx = await adapter.unstake(5000.0)
    assert tx.startswith("ethena-unstake-stub")
    assert await adapter.get_balance() == pytest.approx(5000.0)


@pytest.mark.asyncio
async def test_ethena_unstake_logs_cooldown(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    adapter = EthenaAdapter(apy_tracker=_StubTracker(None))
    await adapter.stake(1000.0)
    with caplog.at_level(logging.INFO, logger="eta_engine.staking.ethena"):
        await adapter.unstake(500.0)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "cooldown=7d" in joined


@pytest.mark.asyncio
async def test_ethena_apy_live_value() -> None:
    adapter = EthenaAdapter(apy_tracker=_StubTracker(11.4))
    assert await adapter.get_apy() == pytest.approx(11.4)


@pytest.mark.asyncio
async def test_ethena_apy_fallback() -> None:
    adapter = EthenaAdapter(apy_tracker=_StubTracker(None))
    assert await adapter.get_apy() == pytest.approx(7.0)
