"""
EVOLUTIONARY TRADING ALGO  //  scripts.authorize_tradovate
==============================================
Tradovate OAuth2 authorization — pull creds from SECRETS, POST
``/auth/accessTokenRequest`` on the demo endpoint, persist an auth-status
artifact. This is the human-runnable front door for the auth flow baked
into ``eta_engine.venues.tradovate.TradovateVenue.authenticate()``.

Exit codes:
    0  AUTHORIZED   — real OAuth2 flow succeeded, token acquired.
    1  FAILED       — creds present but OAuth2 call failed (HTTP / payload).
    2  STUBBED      — creds missing; ran the creds-less stub path. Not fatal
                      to the repo, but the live fleet cannot start until real
                      creds are populated.

Usage:
    python -m eta_engine.scripts.authorize_tradovate             # demo
    python -m eta_engine.scripts.authorize_tradovate --live      # live URL
    python -m eta_engine.scripts.authorize_tradovate --prop-account blusky_50k
    python -m eta_engine.scripts.authorize_tradovate --json      # machine-readable

Writes:
    var/eta_engine/state/tradovate_auth_status.json -- per-run report,
    overwritten each run.

This script does NOT log secret values. Only last-4 of the access token
and the key names it attempted to read are emitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Allow `python -m eta_engine.scripts.authorize_tradovate` from either parent.
_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.core.secrets import (  # noqa: E402
    SECRETS,
    TRADOVATE_APP_ID,
    TRADOVATE_APP_SECRET,
    TRADOVATE_CID,
    TRADOVATE_PASSWORD,
    TRADOVATE_USERNAME,
)
from eta_engine.venues.tradovate import (  # noqa: E402
    TRADOVATE_DEMO,
    TRADOVATE_LIVE,
    TradovateVenue,
)

_LOG = logging.getLogger(__name__)


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "y"}


# Replicated from ``eta_engine.venues.connection._secret``. Importing that
# helper would pull in the full venues.connection module (BybitVenue,
# IbkrClientPortalVenue, OkxVenue, TastytradeVenue, TradovateVenue, router
# constants...) just to get a 12-line gate, which inflates this script's
# import surface and creates a scripts -> venues.connection dependency that
# the rest of scripts/ does not have. Keep behavior identical: fail-closed
# when ETA_LIVE_MODE=1 and the secret is empty; warn-and-fall-through
# otherwise so dev/test runs still get the empty-string fallback with a
# paper trail in the logs.
def _secret(key: str) -> str:
    """Resolve a broker secret with fail-closed live-mode enforcement."""
    value = SECRETS.get(key, required=False) or ""
    if not value:
        live_mode = _truthy(os.environ.get("ETA_LIVE_MODE"))
        if live_mode:
            raise RuntimeError(
                f"ETA_LIVE_MODE=1 but broker secret missing for {key}; "
                "refusing to silently fall through to mock"
            )
        _LOG.warning(
            "broker secret missing for %s; falling through to mock adapter "
            "(set ETA_LIVE_MODE=1 to fail closed)",
            key,
        )
    return value

ROOT = _ROOT
STATUS_PATH = ROOT.parent / "var" / "eta_engine" / "state" / "tradovate_auth_status.json"

_REQUIRED = [
    TRADOVATE_USERNAME,
    TRADOVATE_PASSWORD,
    TRADOVATE_APP_ID,
    TRADOVATE_APP_SECRET,
    TRADOVATE_CID,
]

_PROP_ACCOUNT_PREFIXES = {
    "blusky_50k": "BLUSKY_",
    "mffu_50k": "MFFU_",
}


@dataclass
class AuthReport:
    kind: str = "eta_tradovate_auth_status"
    generated_at_utc: str = ""
    credential_scope: str = "default"
    endpoint: str = ""
    demo: bool = True
    creds_present: dict[str, bool] = field(default_factory=dict)
    has_all_creds: bool = False
    auth_path: str = "stub"  # "real" | "stub"
    result: str = "PENDING"  # AUTHORIZED | FAILED | STUBBED
    reason: str = ""
    token_last4: str = ""
    token_expires_at: str = ""


def _required_keys(prefix: str = "") -> list[str]:
    return [f"{prefix}{key}" for key in _REQUIRED]


def _prefix_for_prop_account(prop_account: str | None) -> tuple[str, str]:
    if not prop_account:
        return "", "default"
    alias = prop_account.strip().lower()
    try:
        return _PROP_ACCOUNT_PREFIXES[alias], alias
    except KeyError as exc:
        choices = ", ".join(sorted(_PROP_ACCOUNT_PREFIXES))
        raise ValueError(f"unknown prop account alias {prop_account!r}; expected one of: {choices}") from exc


def _build_report(required_keys: list[str] | None = None, credential_scope: str = "default") -> AuthReport:
    report = AuthReport(
        generated_at_utc=datetime.now(UTC).isoformat(),
        credential_scope=credential_scope,
    )
    for k in required_keys or _REQUIRED:
        v = SECRETS.get(k, required=False)
        report.creds_present[k] = bool(v)
    report.has_all_creds = all(report.creds_present.values())
    return report


def _last4(s: str | None) -> str:
    if not s:
        return ""
    return s[-4:] if len(s) >= 4 else "****"


async def _run(demo: bool, prop_account: str | None = None) -> tuple[int, AuthReport]:
    # Workspace hard rule #2: Tradovate is dormant unless explicitly reactivated.
    if not _truthy(os.environ.get("ETA_TRADOVATE_ENABLED")):
        raise RuntimeError(
            "Tradovate is dormant per workspace policy; "
            "set ETA_TRADOVATE_ENABLED=1 to activate"
        )

    prefix, credential_scope = _prefix_for_prop_account(prop_account)
    required = _required_keys(prefix)
    report = _build_report(required, credential_scope)
    report.demo = demo
    report.endpoint = TRADOVATE_DEMO if demo else TRADOVATE_LIVE

    if not report.has_all_creds:
        # Stub path — no network, no creds to leak.
        venue = TradovateVenue(api_key="", api_secret="", demo=demo)
        await venue.authenticate()
        report.auth_path = "stub"
        report.result = "STUBBED"
        missing = [k for k, ok in report.creds_present.items() if not ok]
        report.reason = (
            f"missing {len(missing)}/{len(required)} creds: "
            f"{','.join(missing)} -- populate via keyring or eta_engine/.env "
            f"and rerun"
        )
        report.token_last4 = ""  # stub token, don't report fake last4
        report.token_expires_at = venue._expiration.isoformat() if venue._expiration else ""
        await venue.close()
        return 2, report

    # Real auth path — creds are present.
    username = _secret(f"{prefix}{TRADOVATE_USERNAME}")
    password = _secret(f"{prefix}{TRADOVATE_PASSWORD}")
    app_id = _secret(f"{prefix}{TRADOVATE_APP_ID}") or "EtaEngine"
    cid = _secret(f"{prefix}{TRADOVATE_CID}")
    app_secret = _secret(f"{prefix}{TRADOVATE_APP_SECRET}")
    venue = TradovateVenue(
        api_key=username,
        api_secret=password,
        demo=demo,
        app_id=app_id,
        cid=cid,
        app_secret=app_secret,
    )
    report.auth_path = "real"
    try:
        await venue.authenticate()
    except Exception as exc:  # noqa: BLE001
        report.result = "FAILED"
        report.reason = f"{type(exc).__name__}: {exc}"
        await venue.close()
        return 1, report
    report.result = "AUTHORIZED"
    report.reason = "oauth2 accessTokenRequest succeeded"
    report.token_last4 = _last4(venue._access_token)
    report.token_expires_at = venue._expiration.isoformat() if venue._expiration else ""
    await venue.close()
    return 0, report


def _write(report: AuthReport) -> Path:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return STATUS_PATH


def _print_human(report: AuthReport) -> None:
    print()
    print("EVOLUTIONARY TRADING ALGO -- Tradovate Authorization")
    print("=" * 66)
    print(f"endpoint     : {report.endpoint}")
    print(f"credential_scope: {report.credential_scope}")
    print(f"auth_path    : {report.auth_path}")
    print(f"has_all_creds: {report.has_all_creds}")
    for k, ok in report.creds_present.items():
        icon = "[OK]" if ok else "[--]"
        print(f"  {icon} {k}")
    print("-" * 66)
    print(f"result       : {report.result}")
    print(f"token_last4  : {report.token_last4}")
    print(f"expires_at   : {report.token_expires_at}")
    if report.reason:
        # Wrap at 66 chars
        print(f"reason       : {report.reason}")
    print("=" * 66)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tradovate OAuth2 authorize")
    ap.add_argument("--live", action="store_true", help="Use live URL instead of demo (default: demo)")
    ap.add_argument(
        "--prop-account",
        choices=sorted(_PROP_ACCOUNT_PREFIXES),
        help="Authorize with a prop-account credential prefix, e.g. BLUSKY_*. ",
    )
    ap.add_argument("--json", action="store_true", help="Emit only the JSON report on stdout")
    args = ap.parse_args()

    rc, report = asyncio.run(_run(demo=not args.live, prop_account=args.prop_account))
    path = _write(report)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        _print_human(report)
        print(f"status -> {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
