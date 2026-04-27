"""Jito JitoSOL adapter — Solana MEV-boosted liquid staking.

Solana's SDK (solana-py / solders) is heavier than web3.py and isn't a hard
dep here. The adapter therefore:

* Uses :class:`eta_engine.staking.apy_tracker.ApyTracker` for live APY
  (DefiLlama aggregates MEV into the displayed pool APY)
* Reports balances from its in-memory state — real SPL balance lookups
  require ``solana-py`` + an RPC URL + the token account address, which we
  expose as injectable kwargs so an orchestrator can wire them live.
* Builds structured stake/unstake payloads with the Jito stake pool program
  id so any live signer gets the canonical call shape.

When ``rpc_url`` and ``token_account`` are supplied AND ``solana-py`` is
installed, :meth:`get_balance` does a real ``getTokenAccountBalance`` RPC
call (via :func:`_fetch_spl_balance`). If any link is missing, it falls
back to the in-memory ledger.
"""

from __future__ import annotations

import logging
from typing import Any

from eta_engine.staking.apy_tracker import ApyTracker, get_shared_tracker
from eta_engine.staking.base import StakingAdapter
from eta_engine.staking.web3_client import build_contract_call

logger = logging.getLogger(__name__)

# Jito mainnet stake pool program + authority.
JITO_STAKE_POOL = "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb"
JITO_RESERVE_STAKE = "HVg6mhR4hN8AtdYZeGp5Mjxr4Cp4zVjFDcPnJ4nHAcgT"
JITOSOL_MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"


def _load_solana_client() -> Any | None:  # noqa: ANN401 - solana.rpc client if available
    """Lazy-import solana-py's Client. None if solana-py not installed."""
    try:
        from solana.rpc.api import Client  # type: ignore[import-untyped]
    except ImportError:
        return None
    return Client


def _fetch_spl_balance(rpc_url: str, token_account: str) -> float | None:
    """Read an SPL token account balance (human units). None on any error."""
    client_cls = _load_solana_client()
    if client_cls is None:
        return None
    try:
        client = client_cls(rpc_url)
        resp = client.get_token_account_balance(token_account)
        value = getattr(resp, "value", None) or resp.get("result", {}).get("value", {})
        ui_amount = getattr(value, "ui_amount", None) if hasattr(value, "ui_amount") else value.get("uiAmount")
    except Exception as e:  # noqa: BLE001 - RPC / network / decode errors
        logger.warning("Jito get_token_account_balance failed: %s", e)
        return None
    return float(ui_amount) if ui_amount is not None else None


class JitoAdapter(StakingAdapter):
    """Jito: SOL -> JitoSOL with MEV reward boost."""

    symbol: str = "SOL"
    token: str = "JitoSOL"
    target_apy: float = 6.5

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        wallet_address: str | None = None,
        token_account: str | None = None,
        stake_pool: str = JITO_STAKE_POOL,
        apy_tracker: ApyTracker | None = None,
    ) -> None:
        self._balance: float = 0.0
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._token_account = token_account
        self._stake_pool = stake_pool
        self._apy_tracker = apy_tracker or get_shared_tracker()

    async def stake(self, amount: float, token: str | None = None) -> str:  # noqa: ARG002 - token kept for parity
        """Deposit SOL into the Jito stake pool to mint JitoSOL."""
        if amount <= 0:
            raise ValueError(f"Stake amount must be positive, got {amount}")
        payload = build_contract_call(
            self._stake_pool,
            "DepositSol",
            self._wallet_address or "0x0",
            int(amount * 1e9),  # lamports
        )
        logger.info("Jito stake plan | amount=%.6f SOL payload=%s", amount, payload["function"])
        self._balance += amount
        return f"jito-stake-stub-{amount}"

    async def unstake(self, amount: float) -> str:
        """Withdraw JitoSOL — instant via liquid pool or 2-epoch delayed."""
        if amount <= 0 or amount > self._balance:
            raise ValueError(f"Invalid unstake amount: {amount} (balance: {self._balance})")
        payload = build_contract_call(
            self._stake_pool,
            "WithdrawSol",
            self._wallet_address or "0x0",
            int(amount * 1e9),
        )
        logger.info("Jito unstake plan | amount=%.6f JitoSOL payload=%s", amount, payload["function"])
        self._balance -= amount
        return f"jito-unstake-stub-{amount}"

    async def get_balance(self) -> float:
        """Return JitoSOL balance — real RPC if configured, else in-memory."""
        if self._rpc_url and self._token_account:
            import asyncio as _asyncio

            real = await _asyncio.to_thread(_fetch_spl_balance, self._rpc_url, self._token_account)
            if real is not None:
                return real
        return self._balance

    async def get_apy(self) -> float:
        """Live Jito APY (DefiLlama) or target_apy fallback."""
        live = await self._apy_tracker.get_apy("jito")
        return live if live is not None else self.target_apy
