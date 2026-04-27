"""Shared lazy-loaded web3 client + on-chain helpers for staking adapters.

The staking adapters (:mod:`eta_engine.staking.lido`, ``flare``, ``ethena``)
all want to call ``erc20.balanceOf(wallet)`` against different chain RPCs. This
module centralises:

* lazy ``from web3 import Web3`` import — keeps ``web3`` an optional dep
* a minimal ERC-20 ABI fragment for ``balanceOf``/``decimals``/``symbol``
* :func:`read_balance` — async read-only balance fetcher with graceful
  fallback when ``web3`` isn't installed OR no RPC is configured

Write-side transactions (stake/unstake) are NOT implemented here — each adapter
still constructs its own contract-call payload so signing stays colocated with
protocol-specific state. This module is strictly for read paths that the
allocator & dashboard need before approving any on-chain action.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ERC-20 balanceOf + decimals + symbol — enough for LSTs and stables.
ERC20_ABI_MIN: list[dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]


def _load_web3() -> Any | None:  # noqa: ANN401 - web3.Web3 class when available, else None
    """Lazy import ``web3.Web3``. Returns None if web3 is not installed."""
    try:
        from web3 import Web3  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("web3 not installed — read_balance falls back to in-memory state")
        return None
    return Web3


def read_balance(
    rpc_url: str | None,
    wallet_address: str | None,
    token_address: str,
    *,
    decimals_hint: int = 18,
) -> float | None:
    """Read ERC-20 balance for ``wallet_address`` at ``token_address``.

    Returns:
        Balance in human units (divided by 10**decimals), or ``None`` if the
        call couldn't be made (SDK missing, no config, or RPC error).

    The function is *sync* because web3 is sync under the hood — callers wrap
    it in ``asyncio.to_thread`` if they want to avoid blocking the event loop.
    """
    if not rpc_url or not wallet_address:
        return None
    web3_cls = _load_web3()
    if web3_cls is None:
        return None
    try:
        w3 = web3_cls(web3_cls.HTTPProvider(rpc_url))
        token = w3.eth.contract(
            address=web3_cls.to_checksum_address(token_address),
            abi=ERC20_ABI_MIN,
        )
        raw_balance = token.functions.balanceOf(
            web3_cls.to_checksum_address(wallet_address),
        ).call()
        try:
            decimals = int(token.functions.decimals().call())
        except Exception:  # noqa: BLE001 - some tokens skip decimals()
            decimals = decimals_hint
    except Exception as e:  # noqa: BLE001 - RPC failures / network / ABI mismatch
        logger.warning("read_balance failed for %s: %s", token_address, e)
        return None
    return raw_balance / (10**decimals)


def build_contract_call(
    contract_address: str,
    function_name: str,
    *args: Any,  # noqa: ANN401 - contract call args are heterogenous
) -> dict[str, Any]:
    """Build a structured contract-call payload (for logging / dry-run).

    Production signing flow would:
        1. Take this payload
        2. w3.eth.contract(address, abi).functions[name](*args).build_transaction(...)
        3. w3.eth.account.sign_transaction(txn, private_key)
        4. w3.eth.send_raw_transaction(signed.raw_transaction)

    We stop at step (1) here — the payload is a safe structured record the
    allocator can audit before any private key is ever unlocked.
    """
    return {
        "contract": contract_address,
        "function": function_name,
        "args": list(args),
        "kind": "contract_call",
    }
