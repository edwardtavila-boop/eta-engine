"""IBKR Client Portal Gateway auto-reauth daemon.

Automatically logs into the IBKR paper session using stored credentials
and keeps the gateway authenticated. No browser MFA required for paper accounts.

Run as a scheduled task every 30 minutes to refresh before the 24h expiry.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("ibkr_reauth")

IBKR_GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://127.0.0.1:5000")
IBKR_PROXY_HOST = os.environ.get("IBKR_PROXY_HOST", "https://ndcdyn.interactivebrokers.com")
IBKR_ACCOUNT_FILE = os.environ.get("IBKR_ACCOUNT_ID_FILE", "")
IBKR_SYMBOL_CONID_FILE = os.environ.get("IBKR_SYMBOL_CONID_MAP_FILE", "")
CREDS_FILE = os.environ.get("IBKR_CREDS_FILE", str(Path(__file__).parent / ".ibkr_creds.json"))
STATE_FILE = Path(os.environ.get("IBKR_REAUTH_STATE", str(REPO_ROOT / "var" / "eta_engine" / "state" / "ibkr_reauth.json")))


def _read_creds() -> dict[str, str]:
    """Read IBKR credentials from env or file.
    
    Prefers env vars IBKR_USERNAME and IBKR_PASSWORD for security.
    Falls back to creds file for headless operation.
    """
    username = os.environ.get("IBKR_USERNAME", "")
    password = os.environ.get("IBKR_PASSWORD", "")
    if username and password:
        return {"username": username, "password": password}
    if Path(CREDS_FILE).exists():
        return json.loads(Path(CREDS_FILE).read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"reauth_count": 0, "last_status": "", "last_error": ""}


def reauth_gateway() -> dict[str, Any]:
    """Re-authenticate the IBKR Client Portal Gateway.
    
    Uses the IBKR SSO flow to get a session token, then submits
    it to the local gateway. Returns status dict.
    """
    import ssl
    import urllib.parse
    import urllib.request

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    state = _load_state()
    creds = _read_creds()

    if not creds.get("username") or not creds.get("password"):
        return {"status": "error", "message": "No credentials configured. Set IBKR_USERNAME/IBKR_PASSWORD env vars."}

    try:
        # Step 1: Check current auth status
        req = urllib.request.Request(
            f"{IBKR_GATEWAY_URL}/v1/api/iserver/auth/status",
            method="GET",
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
            status = json.loads(resp.read().decode())

        if status.get("authenticated"):
            _save_state({
                **state,
                "reauth_count": state.get("reauth_count", 0),
                "last_status": "already_authenticated",
                "last_check": __import__("datetime").datetime.now().isoformat(),
            })
            return {"status": "ok", "message": "already authenticated"}

        # Step 2: Post to IBKR SSO login
        login_data = urllib.parse.urlencode({
            "username": creds["username"],
            "password": creds["password"],
            "locale": "en",
            "mac": "",
            "machineName": "",
        }).encode()

        req = urllib.request.Request(
            f"{IBKR_PROXY_HOST}/sso/Login",
            data=login_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:
            body = resp.read().decode()
            redirect_url = resp.geturl()

        # Step 3: Extract SSO token from redirect URL
        parsed = urllib.parse.urlparse(redirect_url)
        token_params = urllib.parse.parse_qs(parsed.query)
        sso_token = token_params.get("sso_token", [None])[0]

        if not sso_token:
            # Token might be in the response body
            import re
            match = re.search(r'sso_token=([^&"\']+)', body)
            if match:
                sso_token = match.group(1)

        if not sso_token:
            return {"status": "error", "message": "Could not extract SSO token from login response"}

        # Step 4: Submit token to local gateway
        req = urllib.request.Request(
            f"{IBKR_GATEWAY_URL}/v1/api/iserver/auth/ssovalidate",
            data=json.dumps({"sso_token": sso_token}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=30) as resp:
            validate_result = json.loads(resp.read().decode())

        # Step 5: Verify auth
        req = urllib.request.Request(
            f"{IBKR_GATEWAY_URL}/v1/api/iserver/auth/status",
            method="GET",
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
            final_status = json.loads(resp.read().decode())

        authenticated = final_status.get("authenticated", False)
        new_count = state.get("reauth_count", 0) + (1 if authenticated else 0)
        _save_state({
            **state,
            "reauth_count": new_count,
            "last_status": "authenticated" if authenticated else "failed",
            "auth_detail": final_status,
            "last_check": __import__("datetime").datetime.now().isoformat(),
            "last_error": "" if authenticated else "SSO validate did not produce authenticated session",
        })

        if authenticated:
            logger.info("IBKR reauth SUCCESS. Account: %s", final_status.get("MAC", ""))
            return {"status": "ok", "message": "authenticated"}
        else:
            logger.warning("IBKR reauth FAILED: %s", final_status)
            return {"status": "error", "message": f"SSO validate failed: {final_status}"}

    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:500] if e.fp else str(e)
        logger.error("IBKR reauth HTTP error: %s %s", e.code, err_body)
        _save_state({**state, "last_status": "http_error", "last_error": f"{e.code}: {err_body}",
                     "last_check": __import__("datetime").datetime.now().isoformat()})
        return {"status": "error", "message": f"HTTP {e.code}", "detail": err_body}
    except Exception as e:
        logger.error("IBKR reauth error: %s", e)
        _save_state({**state, "last_status": "error", "last_error": str(e),
                     "last_check": __import__("datetime").datetime.now().isoformat()})
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = reauth_gateway()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "ok" else 1)
