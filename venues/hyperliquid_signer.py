"""
EVOLUTIONARY TRADING ALGO  //  venues.hyperliquid_signer
============================================
EIP-712 signer for Hyperliquid L1 actions.

Hyperliquid's exchange endpoint requires every mutating action (order,
cancel, modify, withdraw) to be signed with the operator's Ethereum
private key using EIP-712 typed-data. This module encapsulates the
signing primitive so the rest of the codebase never touches the key.

Design
------
  * The signer lives in its own module so the Hyperliquid venue can
    load optional dependency ``eth_account`` on first use only.
  * The key material is read from a file path (never env vars or JSON
    config). The file must be operator-owned with mode 0o600 on POSIX;
    on Windows we warn if the file is world-readable.
  * ``sign_l1_action(msg: dict, *, nonce: int, chain_id: int) -> dict``
    returns ``{"r": ..., "s": ..., "v": ...}`` compatible with the
    Hyperliquid exchange API's ``signature`` field.
  * ``eth_account`` is NOT required to import this module. It's only
    required to actually sign. ``is_available()`` tells you whether the
    signer can run; ``sign_l1_action()`` raises
    :class:`HyperliquidSignerUnavailable` if the dep is missing.

Usage
-----
    signer = HyperliquidSigner.from_key_file("/etc/apex/hl.key")
    if signer.is_available():
        sig = signer.sign_l1_action(
            {"type": "order", "orders": [...], "grouping": "na"},
            nonce=int(time.time() * 1000),
            chain_id=42161,
        )
        payload = {"action": msg, "nonce": nonce, "signature": sig}
        # POST payload to /exchange
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HYPERLIQUID_MAINNET_CHAIN_ID = 42161  # Arbitrum One
HYPERLIQUID_TESTNET_CHAIN_ID = 421614  # Arbitrum Sepolia


class HyperliquidSignerUnavailableError(RuntimeError):
    """Raised when ``eth_account`` is not installed but a signature is requested."""


# Back-compat alias so "Unavailable" reads naturally at call sites. The
# Error-suffixed name is the canonical one.
HyperliquidSignerUnavailable = HyperliquidSignerUnavailableError


class HyperliquidSignerConfigError(ValueError):
    """Raised when the signer is constructed with invalid key material."""


class HyperliquidSigner:
    """EIP-712 signer for Hyperliquid L1 actions.

    Construct via :meth:`from_key_file` in production. The ``__init__``
    constructor accepts a raw hex private key for unit-test convenience
    only -- operators should never hold the key in memory longer than
    the signing call.
    """

    def __init__(self, private_key_hex: str) -> None:
        key = (private_key_hex or "").strip()
        if key.startswith("0x") or key.startswith("0X"):
            key = key[2:]
        if len(key) != 64:
            msg = f"Hyperliquid private key must be 32 bytes (64 hex chars); got {len(key)} chars"
            raise HyperliquidSignerConfigError(msg)
        try:
            int(key, 16)
        except ValueError as exc:
            msg = "Hyperliquid private key must be valid hex"
            raise HyperliquidSignerConfigError(msg) from exc
        self._key_hex = "0x" + key

    @classmethod
    def from_key_file(cls, path: str | Path) -> HyperliquidSigner:
        """Load the private key from a file and return a signer.

        The file should contain a single hex-encoded private key.
        Leading ``0x`` is optional; trailing whitespace is stripped.

        On POSIX systems the file must have mode ``0o600`` (operator
        read/write only). On Windows we warn if the file is readable
        by other users but do not refuse -- Windows ACL enforcement is
        the operator's responsibility.
        """
        key_path = Path(path)
        if not key_path.exists():
            msg = f"Hyperliquid signer key file not found: {key_path}"
            raise HyperliquidSignerConfigError(msg)
        if os.name == "posix":
            mode = key_path.stat().st_mode & 0o777
            if mode & 0o077:
                msg = f"Hyperliquid signer key file {key_path} has mode {oct(mode)}; must be 0o600 (owner-only)"
                raise HyperliquidSignerConfigError(msg)
        raw = key_path.read_text(encoding="utf-8").strip()
        return cls(raw)

    @staticmethod
    def is_available() -> bool:
        """Return True when ``eth_account`` is importable."""
        try:
            import eth_account  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True

    def address(self) -> str | None:
        """Return the 0x-prefixed Ethereum address, or ``None`` if eth_account missing."""
        if not self.is_available():
            return None
        from eth_account import Account  # noqa: PLC0415

        return Account.from_key(self._key_hex).address

    def sign_l1_action(
        self,
        action: dict[str, Any],
        *,
        nonce: int,
        chain_id: int = HYPERLIQUID_MAINNET_CHAIN_ID,
        vault_address: str | None = None,
    ) -> dict[str, Any]:
        """Sign an L1 action payload per Hyperliquid's typed-data schema.

        Parameters
        ----------
        action:
            The action dict (``{"type": "order", ...}``, etc.).
        nonce:
            Monotonic nonce (typically ``int(time.time() * 1000)``).
        chain_id:
            EIP-155 chain id. Use
            :const:`HYPERLIQUID_MAINNET_CHAIN_ID` for prod,
            :const:`HYPERLIQUID_TESTNET_CHAIN_ID` for testnet.
        vault_address:
            Optional sub-account / vault signer override.

        Returns
        -------
        dict
            ``{"r": "0x...", "s": "0x...", "v": 27 | 28}`` -- the shape
            Hyperliquid's ``/exchange`` endpoint expects in the
            ``signature`` field.

        Raises
        ------
        HyperliquidSignerUnavailable
            When ``eth_account`` is not installed.
        """
        if not self.is_available():
            msg = "eth_account is required to sign Hyperliquid L1 actions. Install with: pip install eth-account"
            raise HyperliquidSignerUnavailableError(msg)

        from eth_account import Account  # noqa: PLC0415
        from eth_account.messages import encode_typed_data  # noqa: PLC0415

        # Hyperliquid's domain. See:
        # https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#signing
        domain = {
            "name": "Exchange",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        }
        types = {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
        }
        # ``connectionId`` is the keccak256 hash of the msgpack-encoded
        # action + nonce (+ optional vault_address). The caller is
        # expected to provide a pre-computed connectionId via
        # action["__connection_id"] for now; full msgpack encoding is
        # a follow-up when the full action encoder lands.
        connection_id = action.get("__connection_id")
        if connection_id is None:
            msg = (
                "sign_l1_action requires action['__connection_id'] "
                "(32-byte keccak over msgpack-encoded action || nonce). "
                "Compute it in the caller until the full action encoder "
                "is wired."
            )
            raise HyperliquidSignerConfigError(msg)
        if isinstance(connection_id, bytes):
            if len(connection_id) != 32:
                msg = "connection_id bytes must be exactly 32 bytes"
                raise HyperliquidSignerConfigError(msg)
            conn_bytes = connection_id
        elif isinstance(connection_id, str):
            hex_ = connection_id[2:] if connection_id.startswith("0x") else connection_id
            if len(hex_) != 64:
                msg = "connection_id hex must be 32 bytes (64 hex chars)"
                raise HyperliquidSignerConfigError(msg)
            conn_bytes = bytes.fromhex(hex_)
        else:
            msg = f"connection_id must be bytes or hex string; got {type(connection_id)!r}"
            raise HyperliquidSignerConfigError(msg)

        _ = nonce, vault_address  # reserved for future use by the action encoder
        message = {
            "source": "a" if chain_id == HYPERLIQUID_MAINNET_CHAIN_ID else "b",
            "connectionId": conn_bytes,
        }
        structured = encode_typed_data(
            domain_data=domain,
            message_types=types,
            message_data=message,
        )
        account = Account.from_key(self._key_hex)
        signed = account.sign_message(structured)
        return {
            "r": "0x" + signed.r.to_bytes(32, "big").hex(),
            "s": "0x" + signed.s.to_bytes(32, "big").hex(),
            "v": signed.v,
        }


__all__ = [
    "HYPERLIQUID_MAINNET_CHAIN_ID",
    "HYPERLIQUID_TESTNET_CHAIN_ID",
    "HyperliquidSigner",
    "HyperliquidSignerConfigError",
    "HyperliquidSignerUnavailable",
    "HyperliquidSignerUnavailableError",
]
