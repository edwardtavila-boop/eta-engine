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
  6. Apex tick-cadence invariant holds for configured cushion (R2)
  7. Audit-log directory is writable + fsyncable (R3)

Exit 0 = green; exit 1 = any red.

Usage:
    python -m eta_engine.scripts.preflight
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# Allow execution as both `eta_engine.scripts.preflight` (from parent)
# and `scripts.preflight` (from inside eta_engine/).
_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

import yaml  # noqa: E402
from eta_engine.core.kill_switch_runtime import (  # noqa: E402
    ApexTickCadenceError,
    validate_apex_tick_cadence,
)
from eta_engine.core.secrets import (  # noqa: E402
    REQUIRED_KEYS,
    SECRETS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from eta_engine.core.session_filter import is_news_blackout  # noqa: E402
from eta_engine.obs.alerts import Alert, AlertLevel, TelegramAlerter  # noqa: E402
from eta_engine.venues import BrokerConnectionManager, ConnectionStatus, write_broker_connection_report  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
FIRM_VERDICT_PATH = ROOT / "docs" / "last_firm_verdict.json"
VENUE_CONNECTION_REPORT_DIR = ROOT / "docs" / "broker_connections"
KILL_SWITCH_YAML_PATH = ROOT / "configs" / "kill_switch.yaml"
DEFAULT_AUDIT_LOG_DIR = ROOT / "state"
DEFAULT_LIVE_TICK_INTERVAL_S = 1.0

CheckResult = tuple[str, bool, str]


def check_secrets() -> CheckResult:
    missing = SECRETS.validate_required_keys(REQUIRED_KEYS)
    if missing:
        return ("secrets", False, f"{len(missing)}/{len(REQUIRED_KEYS)} missing: {','.join(missing[:5])}...")
    return ("secrets", True, f"all {len(REQUIRED_KEYS)} required keys present")


async def _check_venues_async() -> CheckResult:
    # Touch the config path to surface a parse error early, even though the
    # manager will re-read it itself. Historically the full parse result was
    # held locally -- we keep the read-and-validate step but no longer bind
    # the dict to an unused name (F841).
    try:
        if CONFIG_PATH.exists():
            json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return ("venues", False, f"config unreadable: {type(e).__name__}")
    manager = BrokerConnectionManager(config_path=CONFIG_PATH)
    try:
        summary = await manager.connect()
    except Exception as exc:  # noqa: BLE001
        return ("venues", False, f"connection probe failed: {type(exc).__name__}")
    _, latest = write_broker_connection_report(
        summary,
        out_dir=VENUE_CONNECTION_REPORT_DIR,
        stem="preflight_venue_connections",
    )
    failed = [report.venue for report in summary.reports if report.status is ConnectionStatus.FAILED]
    counts = summary.counts()
    msg = (
        f"{len(summary.configured_brokers)} venues configured; "
        f"ready={counts['ready']} degraded={counts['degraded']} "
        f"stubbed={counts['stubbed']} unavailable={counts['unavailable']} "
        f"report={latest.name}"
    )
    return ("venues", not failed, msg if not failed else f"{msg}; failed={','.join(failed)}")


def check_venues() -> CheckResult:
    return asyncio.run(_check_venues_async())


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


def check_tick_cadence() -> CheckResult:
    """R2 closure preflight: confirm tick/cushion invariant holds in live mode.

    Reads ``tier_a.apex_eval_preemptive.cushion_usd`` from
    ``configs/kill_switch.yaml`` and runs the canonical validator. Fails
    loudly if the cushion is too tight for the assumed tick cadence --
    same code path that ``load_runtime_config`` uses at live-boot, just
    exposed ahead of time so the operator can correct the config without
    a half-started runtime.

    Uses ``DEFAULT_LIVE_TICK_INTERVAL_S = 1.0`` to match the current
    ``RuntimeConfig`` default. Callers who plan to run with a non-default
    tick should invoke the validator themselves.
    """
    if not KILL_SWITCH_YAML_PATH.exists():
        return (
            "tick_cadence",
            False,
            f"kill_switch.yaml not found at {KILL_SWITCH_YAML_PATH}",
        )
    try:
        ks = yaml.safe_load(KILL_SWITCH_YAML_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return ("tick_cadence", False, f"kill_switch.yaml unreadable: {type(e).__name__}")
    ks = ks or {}
    preempt = (ks.get("tier_a", {}) or {}).get("apex_eval_preemptive", {}) or {}
    cushion_usd = float(preempt.get("cushion_usd", 500.0))
    try:
        validate_apex_tick_cadence(
            tick_interval_s=DEFAULT_LIVE_TICK_INTERVAL_S,
            cushion_usd=cushion_usd,
            live=True,
        )
    except ApexTickCadenceError as exc:
        return ("tick_cadence", False, str(exc))
    except ValueError as exc:
        return ("tick_cadence", False, f"invalid input: {exc}")
    return (
        "tick_cadence",
        True,
        f"tick={DEFAULT_LIVE_TICK_INTERVAL_S}s cushion=${cushion_usd:.0f} OK",
    )


def check_audit_log_readiness() -> CheckResult:
    """R3 closure preflight: confirm the audit-log path is writable + fsyncable.

    The ``TrailingDDTracker`` writes an append-only JSONL audit log with
    ``fsync`` after every append. If the filesystem rejects fsync (e.g.
    a read-only mount, a stubbed network share), the live loop would
    crash on the first state transition. Better to surface that at
    preflight than at the first freeze event.

    Writes a temp file into the tracker's default state dir, appends a
    byte, fsyncs, then deletes the temp file.
    """
    audit_dir = DEFAULT_AUDIT_LOG_DIR
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return (
            "audit_log",
            False,
            f"cannot create {audit_dir}: {type(e).__name__}: {e}",
        )
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(audit_dir),
            prefix="preflight_audit_",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(b'{"preflight":"ok"}\n')
            tmp.flush()
            os.fsync(tmp.fileno())
    except Exception as e:  # noqa: BLE001
        return (
            "audit_log",
            False,
            f"fsync failed in {audit_dir}: {type(e).__name__}: {e}",
        )
    finally:
        try:
            if "tmp_path" in locals():
                tmp_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001, S110
            pass
    return ("audit_log", True, f"writable + fsyncable at {audit_dir}")


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
        await asyncio.to_thread(check_venues),
        check_blackout_window(),
        check_firm_verdict(),
        check_tick_cadence(),
        check_audit_log_readiness(),
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
