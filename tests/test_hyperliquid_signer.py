"""Tests for the Hyperliquid EIP-712 signer primitive.

Covers:
  * Config validation (key length, hex format, file existence, file mode)
  * ``is_available()`` detection
  * Signing path when ``eth_account`` is absent -> raises Unavailable
  * Error paths in sign_l1_action (missing connection_id, bad hex)

When ``eth_account`` IS installed, additional tests verify the signature
shape (r, s, v), determinism, and address derivation.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from eta_engine.venues.hyperliquid_signer import (
    HYPERLIQUID_MAINNET_CHAIN_ID,
    HyperliquidSigner,
    HyperliquidSignerConfigError,
    HyperliquidSignerUnavailable,
)

if TYPE_CHECKING:
    from pathlib import Path

# A deterministic test key -- NEVER use this in production, it's public.
_TEST_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
_TEST_KEY_NO_PREFIX = _TEST_KEY[2:]

_DUMMY_CONNECTION_ID = "0x" + ("ab" * 32)


# ---------------------------------------------------------------------------
# Construction + validation
# ---------------------------------------------------------------------------


def test_accepts_hex_with_prefix() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    assert signer is not None


def test_accepts_hex_without_prefix() -> None:
    signer = HyperliquidSigner(_TEST_KEY_NO_PREFIX)
    assert signer is not None


def test_rejects_short_key() -> None:
    with pytest.raises(HyperliquidSignerConfigError, match="32 bytes"):
        HyperliquidSigner("deadbeef")


def test_rejects_non_hex_key() -> None:
    bad = "z" * 64
    with pytest.raises(HyperliquidSignerConfigError, match="valid hex"):
        HyperliquidSigner(bad)


def test_rejects_empty_key() -> None:
    with pytest.raises(HyperliquidSignerConfigError):
        HyperliquidSigner("")


# ---------------------------------------------------------------------------
# from_key_file
# ---------------------------------------------------------------------------


def test_from_key_file_reads_hex(tmp_path: Path) -> None:
    key_path = tmp_path / "hl.key"
    key_path.write_text(_TEST_KEY, encoding="utf-8")
    if os.name == "posix":
        key_path.chmod(0o600)
    signer = HyperliquidSigner.from_key_file(key_path)
    assert signer is not None


def test_from_key_file_strips_trailing_whitespace(tmp_path: Path) -> None:
    key_path = tmp_path / "hl.key"
    key_path.write_text(_TEST_KEY + "\n\n", encoding="utf-8")
    if os.name == "posix":
        key_path.chmod(0o600)
    signer = HyperliquidSigner.from_key_file(key_path)
    assert signer is not None


def test_from_key_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(HyperliquidSignerConfigError, match="not found"):
        HyperliquidSigner.from_key_file(tmp_path / "nonexistent.key")


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only mode check")
def test_from_key_file_rejects_group_readable(tmp_path: Path) -> None:
    key_path = tmp_path / "hl.key"
    key_path.write_text(_TEST_KEY, encoding="utf-8")
    key_path.chmod(0o640)  # group-readable -- should be rejected
    with pytest.raises(HyperliquidSignerConfigError, match="mode"):
        HyperliquidSigner.from_key_file(key_path)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


def test_is_available_matches_eth_account_import() -> None:
    try:
        import eth_account  # noqa: F401

        expected = True
    except ImportError:
        expected = False
    assert HyperliquidSigner.is_available() is expected


# ---------------------------------------------------------------------------
# Signing paths without eth_account
# ---------------------------------------------------------------------------


def _eth_account_installed() -> bool:
    try:
        import eth_account  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    _eth_account_installed(),
    reason="runs only when eth_account is NOT installed",
)
def test_sign_l1_action_raises_unavailable_when_missing() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    with pytest.raises(HyperliquidSignerUnavailable):
        signer.sign_l1_action(
            {"type": "order", "__connection_id": _DUMMY_CONNECTION_ID},
            nonce=1,
        )


@pytest.mark.skipif(
    _eth_account_installed(),
    reason="runs only when eth_account is NOT installed",
)
def test_address_returns_none_when_missing() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    assert signer.address() is None


# ---------------------------------------------------------------------------
# Signing paths with eth_account
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _eth_account_installed(),
    reason="requires eth_account",
)
def test_sign_l1_action_returns_signature_shape() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    sig = signer.sign_l1_action(
        {"type": "order", "__connection_id": _DUMMY_CONNECTION_ID},
        nonce=1_700_000_000_000,
        chain_id=HYPERLIQUID_MAINNET_CHAIN_ID,
    )
    assert set(sig.keys()) == {"r", "s", "v"}
    assert sig["r"].startswith("0x") and len(sig["r"]) == 66  # 32 bytes hex
    assert sig["s"].startswith("0x") and len(sig["s"]) == 66
    assert sig["v"] in (27, 28)


@pytest.mark.skipif(
    not _eth_account_installed(),
    reason="requires eth_account",
)
def test_sign_l1_action_is_deterministic() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    action = {"type": "order", "__connection_id": _DUMMY_CONNECTION_ID}
    a = signer.sign_l1_action(action, nonce=42)
    b = signer.sign_l1_action(action, nonce=42)
    assert a == b


@pytest.mark.skipif(
    not _eth_account_installed(),
    reason="requires eth_account",
)
def test_address_returns_checksummed_hex() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    addr = signer.address()
    assert isinstance(addr, str)
    assert addr is not None and addr.startswith("0x")
    assert len(addr) == 42


# ---------------------------------------------------------------------------
# Error paths regardless of eth_account availability
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _eth_account_installed(),
    reason="only testable when eth_account is available",
)
def test_sign_l1_action_requires_connection_id() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    with pytest.raises(HyperliquidSignerConfigError, match="connection_id"):
        signer.sign_l1_action({"type": "order"}, nonce=1)


@pytest.mark.skipif(
    not _eth_account_installed(),
    reason="only testable when eth_account is available",
)
def test_sign_l1_action_rejects_short_connection_id() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    with pytest.raises(HyperliquidSignerConfigError, match="32 bytes"):
        signer.sign_l1_action(
            {"type": "order", "__connection_id": "0xdeadbeef"},
            nonce=1,
        )


@pytest.mark.skipif(
    not _eth_account_installed(),
    reason="only testable when eth_account is available",
)
def test_sign_l1_action_rejects_wrong_byte_length() -> None:
    signer = HyperliquidSigner(_TEST_KEY)
    with pytest.raises(HyperliquidSignerConfigError, match="32 bytes"):
        signer.sign_l1_action(
            {"type": "order", "__connection_id": b"too-short"},
            nonce=1,
        )
