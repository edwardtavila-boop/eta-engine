"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.two_factor_drill.

Drill: fake an expiring / forged 2FA claim; verify the cold-wallet gate blocks.

What this drill asserts
-----------------------
:func:`core.two_factor.gate_cold_wallet_op` must:

* Raise :class:`TwoFactorRequiredError` when a cold-wallet op arrives
  with no claim.
* Raise :class:`TwoFactorFailedError` when a claim is presented but
  is wrong (e.g. an expired / forged TOTP code).
* Return ``True`` when a correct live code is presented.

We construct a real base32 secret, compute a correct TOTP for ``now``,
and separately compute a stale TOTP for ``now - 3600`` as the "forged"
code. A silent regression in the constant-time compare or the step
window would either accept the stale code (silent auth break) or reject
the fresh one (false lockout).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from eta_engine.core.two_factor import (
    CopyPolicy,
    SecurityRegistry,
    TotpSecret,
    TwoFactorFailedError,
    TwoFactorRequiredError,
    compute_totp,
    generate_base32_secret,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_two_factor"]


class _FakeSecretsResolver:
    """Tiny fake that mimics ``core.secrets.SECRETS.get``."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    def get(self, key: str, *, required: bool = False) -> str | None:  # noqa: ARG002
        return self._m.get(key)


_COLD_OP: str = "promote_strategy_to_live"


def drill_two_factor(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Drive the 2FA gate through three scenarios; verify each one."""
    secret = generate_base32_secret()
    resolver = _FakeSecretsResolver({"APEX_TOTP": secret})
    registry = SecurityRegistry(
        totp=TotpSecret(secret_ref="APEX_TOTP"),
        policy=CopyPolicy.TOTP_ONLY,
        cold_wallet_ops_gated=True,
    )

    now = float(time.time())
    fresh_code = compute_totp(secret, now=now)
    # 1 hour ago -- well outside the default +/- 1 step tolerance
    stale_code = compute_totp(secret, now=now - 3_600.0)

    observed: dict[str, Any] = {"fresh_code_prefix": fresh_code[:2] + "****"}

    # Scenario 1: no claim at all must raise TwoFactorRequiredError.
    from eta_engine.core.two_factor import gate_cold_wallet_op  # local import keeps top-level tidy

    try:
        gate_cold_wallet_op(_COLD_OP, registry=registry, secrets_resolver=resolver, now=now)
    except TwoFactorRequiredError as exc:
        observed["no_claim_error"] = str(exc)
    else:
        return drill_result(
            "two_factor",
            passed=False,
            details="cold-wallet op without a claim did not raise TwoFactorRequiredError",
        )

    # Scenario 2: stale code must raise TwoFactorFailedError (not merely return False).
    try:
        gate_cold_wallet_op(
            _COLD_OP,
            registry=registry,
            totp_code=stale_code,
            secrets_resolver=resolver,
            now=now,
        )
    except TwoFactorFailedError as exc:
        observed["stale_claim_error"] = str(exc)
    else:
        return drill_result(
            "two_factor",
            passed=False,
            details="stale TOTP code did not raise TwoFactorFailedError",
        )

    # Scenario 3: fresh code must clear the gate.
    ok = gate_cold_wallet_op(
        _COLD_OP,
        registry=registry,
        totp_code=fresh_code,
        secrets_resolver=resolver,
        now=now,
    )
    if not ok:
        return drill_result(
            "two_factor",
            passed=False,
            details="fresh TOTP code did not clear the gate",
        )

    observed["fresh_claim_ok"] = True
    return drill_result(
        "two_factor",
        passed=True,
        details="missing claim and stale claim both rejected; fresh claim cleared the gate",
        observed=observed,
    )
