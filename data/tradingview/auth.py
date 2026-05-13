"""
EVOLUTIONARY TRADING ALGO  //  data.tradingview.auth
====================================================
Auth-state load/save for the headless TradingView session.

The auth state is the JSON blob Playwright produces from
``browser_context.storage_state()`` -- a structure containing cookies and
localStorage entries. The file MUST be 0600-mode on POSIX since cookies
include the TradingView session token.

Operator flow:

1. On a workstation with a real browser, run::

      python -m eta_engine.scripts.tradingview_auth_refresh

   A real Chrome window pops; operator logs in (incl. 2FA), then closes
   the window. The script writes ``tradingview_auth.json`` to the path.

2. Copy that file to the VPS into the canonical workspace state path:
   ``var/eta_engine/state/tradingview_auth.json``.

3. The capture daemon ``run_tradingview_capture`` loads it via
   :func:`load_auth_state` and hands it to Playwright as ``storage_state``.

Auth state is short-lived (TradingView rotates ~weekly). The dashboard
panel surfaces a `TV_AUTH_EXPIRED` alert when the loader rejects the
file or when the daemon's first navigation lands on /signin/.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eta_engine.scripts import workspace_roots

log = logging.getLogger(__name__)

DEFAULT_AUTH_PATH = workspace_roots.ETA_TRADINGVIEW_AUTH_STATE_PATH


class AuthStateError(Exception):
    """Auth-state file missing, malformed, or insecure."""


@dataclass(frozen=True)
class AuthState:
    """Wrapper around the Playwright storage_state JSON."""

    cookies: list[dict[str, Any]]
    origins: list[dict[str, Any]]
    source_path: Path | None = None

    def to_storage_state(self) -> dict[str, Any]:
        """Return the dict shape Playwright's ``storage_state=`` expects."""
        return {"cookies": list(self.cookies), "origins": list(self.origins)}

    @property
    def has_session_cookie(self) -> bool:
        """True if the auth state carries a TradingView session cookie.

        TradingView gates the chart and watchlist behind the ``sessionid``
        cookie set on the ``.tradingview.com`` domain.
        """
        return any(c.get("name") == "sessionid" and ".tradingview.com" in c.get("domain", "") for c in self.cookies)


def load_auth_state(path: Path | str | None = None) -> AuthState:
    """Load the auth-state JSON, validating shape + POSIX file mode.

    Raises :class:`AuthStateError` when the file is missing, unreadable,
    not JSON, or (POSIX only) is group/world-readable.
    """
    p = Path(path).expanduser() if path else DEFAULT_AUTH_PATH
    if not p.exists():
        raise AuthStateError(f"auth state not found: {p}")

    if os.name == "posix":
        st = p.stat()
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            raise AuthStateError(f"auth state {p} mode {oct(mode)} too open; chmod 600")

    try:
        raw = p.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except OSError as e:
        raise AuthStateError(f"auth state read failed: {e}") from e
    except json.JSONDecodeError as e:
        raise AuthStateError(f"auth state JSON malformed: {e}") from e

    if not isinstance(payload, dict):
        raise AuthStateError("auth state root must be an object")

    cookies = payload.get("cookies", [])
    origins = payload.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise AuthStateError("auth state cookies/origins must be lists")

    return AuthState(cookies=cookies, origins=origins, source_path=p)


def save_auth_state(state: dict[str, Any], path: Path | str | None = None) -> Path:
    """Persist a Playwright storage_state dict atomically with 0600 mode.

    Returns the resolved write path. Caller is expected to pass the dict
    Playwright returned from ``context.storage_state()``.
    """
    p = Path(path).expanduser() if path else DEFAULT_AUTH_PATH
    p.parent.mkdir(parents=True, exist_ok=True)

    if not isinstance(state, dict) or "cookies" not in state:
        raise AuthStateError("storage_state must be a dict with 'cookies'")

    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return p
