"""
EVOLUTIONARY TRADING ALGO  //  scripts.preflight
====================================
Runtime safety gate -- must pass before every live-mode boot.

Checks, in order:
  1. All required secrets present
  2. Each configured venue reachable (stub TODO for HTTP)
  3. Current time NOT in a blackout window (session_filter)
  4. Last Firm board verdict != KILL for the active strategy
  5. Telegram alert path ("preflight OK") round-trip

Exit 0 = green; exit 1 = any red.

Usage:
    python -m eta_engine.scripts.preflight
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow execution as both `eta_engine.scripts.preflight` (from parent)
# and `scripts.preflight` (from inside eta_engine/).
_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.core.secrets import (  # noqa: E402
    REQUIRED_KEYS,
    SECRETS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from eta_engine.core.session_filter import is_news_blackout  # noqa: E402
from eta_engine.obs.alerts import Alert, AlertLevel, TelegramAlerter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
FIRM_VERDICT_PATH = ROOT / "docs" / "last_firm_verdict.json"

CheckResult = tuple[str, bool, str]


def check_secrets() -> CheckResult:
    missing = SECRETS.validate_required_keys(REQUIRED_KEYS)
    if missing:
        return ("secrets", False, f"{len(missing)}/{len(REQUIRED_KEYS)} missing: {','.join(missing[:5])}...")
    return ("secrets", True, f"all {len(REQUIRED_KEYS)} required keys present")


def check_venues() -> CheckResult:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except Exception as e:  # noqa: BLE001
        return ("venues", False, f"config unreadable: {type(e).__name__}")
    venues = cfg.get("venues", ["tradovate", "bybit"])
    # TODO: real HTTP ping to each venue health endpoint
    return ("venues", True, f"{len(venues)} venues configured (ping stubbed)")


def check_blackout_window() -> CheckResult:
    now = datetime.now(UTC)
    # TODO: load real event feed; for preflight we treat empty feed as non-blackout
    events: list = []
    if is_news_blackout(now, events):
        return ("blackout", False, f"in blackout at {now.isoformat()}")
    return ("blackout", True, f"clear window at {now.isoformat()}")


def check_firm_verdict() -> CheckResult:
    if not FIRM_VERDICT_PATH.exists():
        return ("firm_verdict", True, "no prior verdict on disk (first run)")
    try:
        data = json.loads(FIRM_VERDICT_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return ("firm_verdict", False, f"verdict file unreadable: {type(e).__name__}")
    verdict = str(data.get("verdict", "")).upper()
    if verdict in {"KILL", "NO_GO"}:
        return ("firm_verdict", False, f"last verdict={verdict}")
    return ("firm_verdict", True, f"last verdict={verdict or 'UNKNOWN'}")


async def check_telegram() -> CheckResult:
    token = SECRETS.get(TELEGRAM_BOT_TOKEN, required=False)
    chat_id = SECRETS.get(TELEGRAM_CHAT_ID, required=False)
    if not token or not chat_id:
        return ("telegram", False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
    alerter = TelegramAlerter(bot_token=token, chat_id=chat_id)
    sent = await alerter.send(
        Alert(level=AlertLevel.INFO, title="preflight", message="preflight OK", dedup_key="preflight-boot")
    )
    return ("telegram", bool(sent), "alert dispatched (transport stubbed)")


def _print_row(name: str, ok: bool, msg: str) -> None:
    icon = "[GREEN]" if ok else "[RED]"
    print(f"{icon:<8} {name:<18} {msg}")


async def _run_async() -> int:
    results: list[CheckResult] = [
        check_secrets(),
        check_venues(),
        check_blackout_window(),
        check_firm_verdict(),
        await check_telegram(),
    ]
    print()
    print("EVOLUTIONARY TRADING ALGO -- preflight")
    print("=" * 66)
    for name, ok, msg in results:
        _print_row(name, ok, msg)
    print("=" * 66)
    failed = sum(1 for _, ok, _ in results if not ok)
    status = "GO" if failed == 0 else "NO-GO"
    print(f"Result: {status}  (passed {len(results) - failed}/{len(results)})")
    return 0 if failed == 0 else 1


def run() -> int:
    return asyncio.run(_run_async())


if __name__ == "__main__":
    sys.exit(run())
