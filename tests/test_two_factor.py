"""Tests for eta_engine.core.two_factor -- TOTP + cold-wallet gate."""

from __future__ import annotations

import time

import pytest

from eta_engine.core.two_factor import (
    _COLD_WALLET_OPS,
    CopyPolicy,
    HardwareKey,
    SecurityRegistry,
    TotpSecret,
    TwoFactorFailed,
    TwoFactorRequired,
    compute_totp,
    gate_cold_wallet_op,
    generate_base32_secret,
    is_cold_wallet_op,
    verify_totp,
)

# --------------------------------------------------------------------------- #
# Determinism vectors -- RFC 6238 Appendix B uses 20-byte ASCII "12345678901234567890"
# base32-encoded = GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ.
# We reproduce the test vectors for 30-second step, SHA-1, 6 digits.
# --------------------------------------------------------------------------- #

_RFC6238_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_RFC6238_VECTORS: list[tuple[int, str]] = [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
]


@pytest.mark.parametrize(("t", "expected"), _RFC6238_VECTORS)
def test_compute_totp_matches_rfc6238_vectors(t: int, expected: str) -> None:
    assert compute_totp(_RFC6238_SECRET, now=float(t)) == expected


def test_verify_totp_accepts_current_code() -> None:
    t = 1_700_000_000.0
    code = compute_totp(_RFC6238_SECRET, now=t)
    assert verify_totp(_RFC6238_SECRET, code, now=t) is True


def test_verify_totp_accepts_previous_step_within_window() -> None:
    t = 1_700_000_000.0
    earlier = t - 30
    code = compute_totp(_RFC6238_SECRET, now=earlier)
    # Still inside +/- 1 window at the later time
    assert verify_totp(_RFC6238_SECRET, code, now=t, window=1) is True


def test_verify_totp_rejects_code_outside_window() -> None:
    t = 1_700_000_000.0
    stale = t - 120  # 4 steps old
    code = compute_totp(_RFC6238_SECRET, now=stale)
    assert verify_totp(_RFC6238_SECRET, code, now=t, window=1) is False


def test_verify_totp_rejects_wrong_length_or_non_numeric() -> None:
    t = 1_700_000_000.0
    assert verify_totp(_RFC6238_SECRET, "12345", now=t) is False  # too short
    assert verify_totp(_RFC6238_SECRET, "1234567", now=t) is False  # too long
    assert verify_totp(_RFC6238_SECRET, "abcdef", now=t) is False  # non-digit
    assert verify_totp(_RFC6238_SECRET, "", now=t) is False  # empty


def test_generate_base32_secret_is_verifiable() -> None:
    s = generate_base32_secret()
    # What we generate should round-trip through compute/verify
    code = compute_totp(s, now=1.0)
    assert verify_totp(s, code, now=1.0) is True


def test_generate_base32_secret_is_random_enough() -> None:
    a = generate_base32_secret()
    b = generate_base32_secret()
    assert a != b


# --------------------------------------------------------------------------- #
# TotpSecret redaction
# --------------------------------------------------------------------------- #


def test_totp_secret_str_is_redacted() -> None:
    s = TotpSecret(secret_ref="FAKE_KEY")
    assert "REDACTED" in str(s)
    # A repr or str must never leak the ref being treated as a plaintext secret
    assert "REDACTED" in str(s)


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #


class _FakeSecrets:
    """Duck-typed SECRETS-like resolver."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    def get(self, key: str, required: bool = False) -> str | None:  # noqa: ARG002
        return self._m.get(key)


def test_is_cold_wallet_op_recognizes_known_ops() -> None:
    for op in _COLD_WALLET_OPS:
        assert is_cold_wallet_op(op) is True
    assert is_cold_wallet_op("place_market_order") is False
    assert is_cold_wallet_op("log_heartbeat") is False


def test_gate_passes_through_non_cold_op() -> None:
    reg = SecurityRegistry()
    assert gate_cold_wallet_op("log_heartbeat", registry=reg) is True
    assert gate_cold_wallet_op("place_market_order", registry=reg) is True


def test_gate_totp_only_accepts_valid_code() -> None:
    t = 1_700_000_000.0
    secret = _RFC6238_SECRET
    code = compute_totp(secret, now=t)
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        policy=CopyPolicy.TOTP_ONLY,
    )
    resolver = _FakeSecrets({"OPERATOR_TOTP": secret})
    assert (
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code=code,
            secrets_resolver=resolver,
            now=t,
        )
        is True
    )


def test_gate_totp_only_raises_required_when_no_code() -> None:
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        policy=CopyPolicy.TOTP_ONLY,
    )
    with pytest.raises(TwoFactorRequired):
        gate_cold_wallet_op("withdraw_cold", registry=reg)


def test_gate_totp_only_raises_failed_when_bad_code() -> None:
    t = 1_700_000_000.0
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        policy=CopyPolicy.TOTP_ONLY,
    )
    resolver = _FakeSecrets({"OPERATOR_TOTP": _RFC6238_SECRET})
    with pytest.raises(TwoFactorFailed):
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code="000000",
            secrets_resolver=resolver,
            now=t,
        )


def test_gate_hardware_only_requires_registered_kid() -> None:
    reg = SecurityRegistry(
        hardware_keys=[HardwareKey(kid="k-abc", nickname="blue-yubi")],
        policy=CopyPolicy.HARDWARE_ONLY,
    )
    assert (
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            hardware_kid="k-abc",
        )
        is True
    )
    with pytest.raises(TwoFactorFailed):
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            hardware_kid="k-unknown",
        )
    with pytest.raises(TwoFactorRequired):
        gate_cold_wallet_op("withdraw_cold", registry=reg)


def test_gate_totp_or_hardware_either_path() -> None:
    t = 1_700_000_000.0
    secret = _RFC6238_SECRET
    code = compute_totp(secret, now=t)
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        hardware_keys=[HardwareKey(kid="k-abc")],
        policy=CopyPolicy.TOTP_OR_HARDWARE,
    )
    resolver = _FakeSecrets({"OPERATOR_TOTP": secret})
    # TOTP path
    assert (
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code=code,
            secrets_resolver=resolver,
            now=t,
        )
        is True
    )
    # HW path
    assert (
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            hardware_kid="k-abc",
        )
        is True
    )


def test_gate_both_requires_two_claims_and_both_valid() -> None:
    t = 1_700_000_000.0
    secret = _RFC6238_SECRET
    code = compute_totp(secret, now=t)
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        hardware_keys=[HardwareKey(kid="k-abc")],
        policy=CopyPolicy.BOTH,
    )
    resolver = _FakeSecrets({"OPERATOR_TOTP": secret})
    # Both valid -> ok
    assert (
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code=code,
            hardware_kid="k-abc",
            secrets_resolver=resolver,
            now=t,
        )
        is True
    )
    # Only TOTP -> TwoFactorRequired (claim not presented)
    with pytest.raises(TwoFactorRequired):
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code=code,
            secrets_resolver=resolver,
            now=t,
        )
    # Both presented, TOTP bad -> TwoFactorFailed
    with pytest.raises(TwoFactorFailed):
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code="000000",
            hardware_kid="k-abc",
            secrets_resolver=resolver,
            now=t,
        )


def test_gate_respects_disabled_master_switch() -> None:
    # When operator has explicitly unlocked (not recommended), the gate
    # simply no-ops. We verify this path runs without raising and returns True.
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        policy=CopyPolicy.TOTP_ONLY,
        cold_wallet_ops_gated=False,
    )
    assert gate_cold_wallet_op("withdraw_cold", registry=reg) is True


def test_gate_resolver_missing_secret_is_treated_as_invalid() -> None:
    t = 1_700_000_000.0
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="NOT_IN_BACKEND"),
        policy=CopyPolicy.TOTP_ONLY,
    )
    resolver = _FakeSecrets({})  # nothing stored
    with pytest.raises(TwoFactorFailed):
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code="123456",
            secrets_resolver=resolver,
            now=t,
        )


def test_gate_without_resolver_treats_totp_as_invalid() -> None:
    t = 1_700_000_000.0
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        policy=CopyPolicy.TOTP_ONLY,
    )
    # No resolver at all -> cannot verify -> reject
    with pytest.raises(TwoFactorFailed):
        gate_cold_wallet_op(
            "withdraw_cold",
            registry=reg,
            totp_code="123456",
            now=t,
        )


def test_security_registry_round_trips_through_pydantic() -> None:
    reg = SecurityRegistry(
        totp=TotpSecret(secret_ref="OPERATOR_TOTP"),
        hardware_keys=[
            HardwareKey(kid="k-abc", nickname="blue-yubi"),
            HardwareKey(kid="k-xyz", nickname="red-yubi", user_verification=False),
        ],
        policy=CopyPolicy.TOTP_OR_HARDWARE,
    )
    d = reg.model_dump()
    reg2 = SecurityRegistry(**d)
    assert reg2.policy == CopyPolicy.TOTP_OR_HARDWARE
    assert len(reg2.hardware_keys) == 2
    # secret_ref survived
    assert reg2.totp is not None
    assert reg2.totp.secret_ref == "OPERATOR_TOTP"


def test_hardware_key_registered_epoch_defaults_to_now() -> None:
    before = time.time()
    k = HardwareKey(kid="k-abc")
    after = time.time()
    assert before <= k.registered_epoch <= after
