"""
EVOLUTIONARY TRADING ALGO  //  core.two_factor
==================================
Stdlib TOTP (RFC 6238) + hardware-key registry for cold-wallet gates.

Why in-house
------------
We already refuse to add a runtime dep just for 2FA (see ROADMAP P8).
RFC 6238 is HMAC-SHA1 over (current_time // step) with 6-digit truncation
- ~40 lines of deterministic Python. Zero network, zero dependencies.

Surfaces
--------
* ``TotpSecret`` -- pydantic wrapper around a base32 shared secret. Never
  logs the secret; ``str()`` returns a redacted marker.
* ``verify_totp(secret, code, now=None, window=1)`` -- accept a 6-digit
  code if it matches the expected code in (now, now-step, now+step)
  windows. Returns ``True`` / ``False`` only.
* ``HardwareKey`` -- pydantic model for a registered FIDO2/YubiKey. We
  store only the public metadata (kid, aaguid, user_verification flag).
* ``SecurityRegistry`` -- two_fa secret ref + hardware-key list + policy
  flags. Persisted to ``core/secrets`` via SECRETS.set -- never written
  to disk as plaintext.
* ``gate_cold_wallet_op(op, registry, code)`` -- pre-flight check. Raises
  ``TwoFactorRequired`` if no 2FA claim is presented on a gated op, and
  ``TwoFactorFailed`` if the claim is wrong.

Scope
-----
This module does NOT:
* Hold live secrets at rest. SECRETS (env / keyring / .env) is canonical.
* Enforce anything on hot-wallet trading -- only cold-wallet ops.
* Replace the OS-level 2FA on the Tradovate / Bybit account logins --
  those are configured by the operator directly on the venue.

It only gates the APEX-side operations that move USD or crypto between
accounts (cold-funnel transfers, staking withdrawals, strategy promotion
to LIVE tier).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _stdlib_secrets
import struct
import time
from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# TOTP (RFC 6238)
# ---------------------------------------------------------------------------

_TOTP_STEP_SECONDS: int = 30
_TOTP_DIGITS: int = 6


def _b32decode_padded(s: str) -> bytes:
    """Decode a base32 string, tolerating missing `=` padding."""
    s = s.strip().replace(" ", "").upper()
    pad = (-len(s)) % 8
    return base64.b32decode(s + ("=" * pad))


def _hotp(secret_bytes: bytes, counter: int, digits: int = _TOTP_DIGITS) -> str:
    """RFC 4226 HOTP. counter is a 64-bit unsigned int."""
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(secret_bytes, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    code = truncated % (10**digits)
    return str(code).zfill(digits)


def compute_totp(
    secret_base32: str, now: float | None = None, step: int = _TOTP_STEP_SECONDS, digits: int = _TOTP_DIGITS
) -> str:
    """Compute the current TOTP code for a base32-encoded shared secret."""
    if now is None:
        now = time.time()
    counter = int(now // step)
    return _hotp(_b32decode_padded(secret_base32), counter, digits=digits)


def verify_totp(
    secret_base32: str,
    code: str,
    now: float | None = None,
    window: int = 1,
    step: int = _TOTP_STEP_SECONDS,
    digits: int = _TOTP_DIGITS,
) -> bool:
    """Verify a TOTP code against a +/- window of step intervals.

    ``window=1`` (default) tolerates 30s clock drift (previous + current +
    next step). Widen with care -- each extra window halves the security.
    """
    code = str(code).strip()
    if len(code) != digits or not code.isdigit():
        return False
    if now is None:
        now = time.time()
    current = int(now // step)
    target = _b32decode_padded(secret_base32)
    for delta in range(-window, window + 1):
        candidate_counter = current + delta
        if candidate_counter < 0:
            # Negative counters have no TOTP meaning (would predate epoch).
            continue
        candidate = _hotp(target, candidate_counter, digits=digits)
        # constant-time compare guards against remote timing attacks
        if hmac.compare_digest(candidate, code):
            return True
    return False


def generate_base32_secret(n_bytes: int = 20) -> str:
    """Return a fresh base32 TOTP secret. 20 bytes = 160 bits, RFC 4226 minimum."""
    raw = _stdlib_secrets.token_bytes(n_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TotpSecret(BaseModel):
    """Opaque wrapper. ``str()`` redacts."""

    secret_ref: str = Field(
        description=(
            "Reference key into the SECRETS backend (e.g. TRADOVATE_TOTP). "
            "Never hold the actual secret in the model; callers must fetch "
            "via SECRETS.get(secret_ref) at verify time."
        ),
    )
    issuer: str = Field(default="ApexPredator")
    account: str = Field(default="operator")

    def __str__(self) -> str:
        return f"<TotpSecret ref={self.secret_ref} REDACTED>"


class HardwareKey(BaseModel):
    """Registered FIDO2/YubiKey metadata only.

    This model intentionally holds NO private key material. The server
    side of a WebAuthn ceremony produces a credential id (``kid``); we
    store it for recall. The actual private key lives on the key.
    """

    kid: str = Field(min_length=4, description="Base64 credential id")
    aaguid: str = Field(default="", description="Authenticator AAGUID")
    user_verification: bool = Field(default=True)
    nickname: str = Field(default="", description="Operator-friendly label")
    registered_epoch: float = Field(default_factory=time.time)


class CopyPolicy(StrEnum):
    """What the gate expects on a cold-wallet op."""

    TOTP_ONLY = "TOTP_ONLY"
    HARDWARE_ONLY = "HARDWARE_ONLY"
    TOTP_OR_HARDWARE = "TOTP_OR_HARDWARE"
    BOTH = "BOTH"


class SecurityRegistry(BaseModel):
    """All the 2FA/hardware material the cold-wallet gate consults."""

    totp: TotpSecret | None = None
    hardware_keys: list[HardwareKey] = Field(default_factory=list)
    policy: CopyPolicy = CopyPolicy.TOTP_ONLY
    cold_wallet_ops_gated: bool = Field(
        default=True,
        description="Master kill-switch. If False the gate no-ops (NOT recommended).",
    )


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TwoFactorRequiredError(Exception):
    """Raised when a gated cold-wallet op is attempted without a claim."""


class TwoFactorFailedError(Exception):
    """Raised when a claim was presented but failed verification."""


# Backward-compat aliases (ruff N818 prefers the Error suffix). Existing
# call sites can keep the short names.
TwoFactorRequired = TwoFactorRequiredError
TwoFactorFailed = TwoFactorFailedError


_COLD_WALLET_OPS: frozenset[str] = frozenset(
    {
        "withdraw_cold",
        "stake_withdraw",
        "cross_wallet_transfer",
        "promote_strategy_to_live",
        "register_new_api_key",
        "disable_kill_switch",
    }
)


def is_cold_wallet_op(op: str) -> bool:
    """True if the op name is in the gated set."""
    return op in _COLD_WALLET_OPS


def _resolve_totp_secret(secret_ref: str, resolver: object | None) -> str | None:
    """Resolve the TOTP base32 secret from an injected SECRETS-like resolver.

    The resolver duck-types ``.get(key, required=False) -> str | None``.
    We avoid importing core.secrets so tests can inject a fake resolver
    without side effects.
    """
    if resolver is None:
        return None
    getter = getattr(resolver, "get", None)
    if not callable(getter):
        return None
    return getter(secret_ref, required=False)


def gate_cold_wallet_op(
    op: str,
    *,
    registry: SecurityRegistry,
    totp_code: str | None = None,
    hardware_kid: str | None = None,
    secrets_resolver: object | None = None,
    now: float | None = None,
) -> bool:
    """Raise on insufficient proof, return True when the op is cleared.

    Arguments:
      op                A cold-wallet op name. Non-gated ops return True.
      registry          SecurityRegistry loaded from config / SECRETS.
      totp_code         The 6-digit code the operator typed (if any).
      hardware_kid      The ``kid`` of the hardware key that just signed.
      secrets_resolver  An object with ``.get(key, required=False)`` that
                        returns the TOTP base32 secret. Typically
                        ``eta_engine.core.secrets.SECRETS``.
      now               Optional epoch override (tests).

    Policy matrix:
      TOTP_ONLY           -> TOTP must verify.
      HARDWARE_ONLY       -> hardware_kid must be registered.
      TOTP_OR_HARDWARE    -> either proof suffices.
      BOTH                -> both required simultaneously.
    """
    if not is_cold_wallet_op(op):
        return True
    if not registry.cold_wallet_ops_gated:
        # Operator has explicitly disabled the gate. Still no-op but
        # never emit False from here -- the kill-switch logs this
        # upstream at a different layer.
        return True

    totp_ok = False
    if totp_code is not None and registry.totp is not None:
        secret = _resolve_totp_secret(registry.totp.secret_ref, secrets_resolver)
        if secret:
            totp_ok = verify_totp(secret, totp_code, now=now)

    hw_ok = False
    if hardware_kid is not None:
        registered_kids = {k.kid for k in registry.hardware_keys}
        hw_ok = hardware_kid in registered_kids

    required = registry.policy
    if required == CopyPolicy.TOTP_ONLY:
        ok = totp_ok
        presented = totp_code is not None
    elif required == CopyPolicy.HARDWARE_ONLY:
        ok = hw_ok
        presented = hardware_kid is not None
    elif required == CopyPolicy.TOTP_OR_HARDWARE:
        ok = totp_ok or hw_ok
        presented = (totp_code is not None) or (hardware_kid is not None)
    elif required == CopyPolicy.BOTH:
        ok = totp_ok and hw_ok
        presented = (totp_code is not None) and (hardware_kid is not None)
    else:  # pragma: no cover -- Enum closed set
        raise ValueError(f"unknown policy {required!r}")

    if not presented:
        raise TwoFactorRequiredError(
            f"op {op!r} requires policy {required.value}; no claim presented",
        )
    if not ok:
        raise TwoFactorFailedError(
            f"op {op!r} rejected by policy {required.value}; claim did not verify",
        )
    return True
