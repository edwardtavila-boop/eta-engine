"""
EVOLUTIONARY TRADING ALGO  //  core.secrets
===============================
Tiered secret manager -- env -> keyring -> .env file -> None.

NEVER log a value. Only log the key name being accessed. Audit trail grows
on every access for compliance.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical required keys for the full fleet
# ---------------------------------------------------------------------------

BYBIT_API_KEY = "BYBIT_API_KEY"
BYBIT_API_SECRET = "BYBIT_API_SECRET"
TRADOVATE_USERNAME = "TRADOVATE_USERNAME"
TRADOVATE_PASSWORD = "TRADOVATE_PASSWORD"
TRADOVATE_APP_ID = "TRADOVATE_APP_ID"
TRADOVATE_APP_SECRET = "TRADOVATE_APP_SECRET"
TRADOVATE_CID = "TRADOVATE_CID"
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
DISCORD_WEBHOOK_URL = "DISCORD_WEBHOOK_URL"
DATABENTO_API_KEY = "DATABENTO_API_KEY"
LUNARCRUSH_API_KEY = "LUNARCRUSH_API_KEY"
BLOCKSCOUT_API_KEY = "BLOCKSCOUT_API_KEY"
COLD_WALLET_ADDRESS = "COLD_WALLET_ADDRESS"

REQUIRED_KEYS: list[str] = [
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    TRADOVATE_USERNAME,
    TRADOVATE_PASSWORD,
    TRADOVATE_APP_ID,
    TRADOVATE_APP_SECRET,
    TRADOVATE_CID,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    DISCORD_WEBHOOK_URL,
    DATABENTO_API_KEY,
    LUNARCRUSH_API_KEY,
    BLOCKSCOUT_API_KEY,
    COLD_WALLET_ADDRESS,
]

_KEYRING_SERVICE = "eta_engine"


class SecretsManager:
    """Tiered secret lookup: env -> keyring -> .env -> None."""

    def __init__(self, env_file: Path | str | None = None) -> None:
        self.env_file = Path(env_file) if env_file else Path(".env")
        self.audit_log: list[str] = []
        self._env_cache: dict[str, str] | None = None

    def _load_env_file(self) -> dict[str, str]:
        if self._env_cache is not None:
            return self._env_cache
        data: dict[str, str] = {}
        if self.env_file.exists():
            for raw in self.env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                data[key.strip()] = val.strip().strip('"').strip("'")
        self._env_cache = data
        return data

    def _try_keyring(self, key: str) -> str | None:
        try:
            import keyring  # type: ignore

            return keyring.get_password(_KEYRING_SERVICE, key)
        except Exception:
            return None

    def _record(self, key: str, source: str) -> None:
        ts = datetime.now(UTC).isoformat()
        self.audit_log.append(f"{ts} get key={key} source={source}")

    def get(self, key: str, required: bool = True) -> str | None:
        """Look up `key` via env -> keyring -> .env -> None.

        If required=True and not found, raises KeyError (never leaks the value).
        """
        # Tier 1: process environment.
        if key in os.environ and os.environ[key] != "":
            self._record(key, "env")
            return os.environ[key]

        # Tier 2: keyring (if installed).
        kr_val = self._try_keyring(key)
        if kr_val:
            self._record(key, "keyring")
            return kr_val

        # Tier 3: .env file.
        env_file = self._load_env_file()
        if key in env_file:
            self._record(key, "env_file")
            return env_file[key]

        self._record(key, "missing")
        if required:
            raise KeyError(f"Required secret not found: {key}")
        return None

    def set(self, key: str, value: str, scope: str = "keyring") -> None:
        """Persist a secret. Default scope uses keyring."""
        if scope != "keyring":
            raise ValueError(f"Unsupported scope: {scope}")
        try:
            import keyring  # type: ignore

            keyring.set_password(_KEYRING_SERVICE, key, value)
            self.audit_log.append(f"{datetime.now(UTC).isoformat()} set key={key} scope={scope}")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Failed to set secret {key}: {type(e).__name__}") from None

    def validate_required_keys(self, keys: list[str] | None = None) -> list[str]:
        """Return list of MISSING required keys (empty list = startup OK)."""
        targets = keys if keys is not None else REQUIRED_KEYS
        missing: list[str] = []
        for k in targets:
            if self.get(k, required=False) is None:
                missing.append(k)
        return missing


# Module-level singleton -- import SECRETS for 99% of use.
SECRETS = SecretsManager()
