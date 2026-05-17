"""Watch IBKR crypto permissions and auto-cutover the supervisor.

Runs on a schedule (Windows Task Scheduler / cron). Each invocation:

  1. Queries IBKR for ``Cryptocurrency@USD``.
  2. If non-zero AND ETA_IBKR_CRYPTO is not yet active, perform the
     cutover:
       a. Uncomment / append ``ETA_IBKR_CRYPTO=1`` in .env
       b. Restart the supervisor process (taskkill + relaunch)
       c. Emit a v3 event that flows to Telegram via Hermes:
          layer="ops", event="live_crypto_activated", severity="INFO"
  3. If perms are still zero, write a status file so the operator
     can see how many checks have happened without action.

Auto-cutover is GATED behind ETA_AUTO_CRYPTO_CUTOVER=1 to keep the
behavior opt-in. Without that flag, the watcher only logs and pings
Telegram — it never modifies state.

Use:
    python -m eta_engine.scripts.auto_cutover_watcher          # dry-run
    python -m eta_engine.scripts.auto_cutover_watcher --apply  # act if ETA_AUTO_CRYPTO_CUTOVER=1

Environment:
    ETA_AUTO_CRYPTO_CUTOVER=1   # opt-in to actually flip the env + restart
    ETA_CUTOVER_STATUS_FILE     # path for the status JSON (default: var/.../cutover_status.json)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import workspace_roots

logger = logging.getLogger(__name__)


# Load eta_engine/.env so the watcher honors ETA_AUTO_CRYPTO_CUTOVER /
# ETA_IBKR_MARKETDATA_TYPE / ... when invoked from Task Scheduler
# (which doesn't inherit .env). The supervisor itself loads .env from
# its own startup; the watcher needs the same.
def _bootstrap_env() -> None:
    env_path = workspace_roots.ETA_ENGINE_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_bootstrap_env()


def _query_crypto_permission() -> tuple[bool, dict[str, object]]:
    """Connect to TWS via dedicated clientId and read Cryptocurrency@USD.
    Returns (perms_active, summary_dict). perms_active=True iff > 0."""
    from ib_insync import IB

    ib = IB()
    summary: dict[str, object] = {}
    try:
        ib.connect("127.0.0.1", 4002, clientId=66, timeout=10)
        for s in ib.accountSummary():
            if "Crypto" in s.tag or s.tag == "AccountType":
                summary[f"{s.tag}@{s.currency}"] = s.value
        crypto_usd = float(summary.get("Cryptocurrency@USD", 0) or 0)
        return crypto_usd > 0, summary
    except Exception as exc:  # noqa: BLE001
        logger.warning("crypto perm query failed: %s", exc)
        return False, {"error": str(exc)}
    finally:
        with contextlib.suppress(Exception):
            ib.disconnect()


def _crypto_currently_enabled_in_env() -> bool:
    """True iff ETA_IBKR_CRYPTO is set (uncommented) in the .env."""
    env_path = workspace_roots.ETA_ENGINE_ROOT / ".env"
    if not env_path.exists():
        return False
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("ETA_IBKR_CRYPTO=") and s.split("=", 1)[1].lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _enable_crypto_in_env() -> bool:
    """Flip ``# ETA_IBKR_CRYPTO=1`` → ``ETA_IBKR_CRYPTO=1`` (or append).
    Returns True iff the file was modified."""
    env_path = workspace_roots.ETA_ENGINE_ROOT / ".env"
    if not env_path.exists():
        env_path.write_text("ETA_IBKR_CRYPTO=1\n", encoding="utf-8")
        return True

    lines = env_path.read_text(encoding="utf-8").splitlines()
    flipped = False
    out: list[str] = []
    for line in lines:
        if line.strip() in {"# ETA_IBKR_CRYPTO=1", "#ETA_IBKR_CRYPTO=1"}:
            out.append("ETA_IBKR_CRYPTO=1")
            flipped = True
        else:
            out.append(line)
    if not flipped and not any(
        ln.strip().startswith("ETA_IBKR_CRYPTO=") and not ln.strip().startswith("#") for ln in lines
    ):
        out.append("ETA_IBKR_CRYPTO=1")
        flipped = True
    if flipped:
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return flipped


def _restart_supervisor() -> bool:
    """Kill the running supervisor process(es) and relaunch from venv."""
    try:
        # kill
        subprocess.run(  # noqa: S603 — controlled, no user input
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'jarvis_strategy_supervisor' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
            ],
            check=False,
            timeout=20,
        )
        # restart via Start-Process (detached)
        log_dir = workspace_roots.ETA_RUNTIME_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"supervisor_{ts}.log"
        subprocess.run(  # noqa: S603
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Start-Process -FilePath "
                "'C:\\EvolutionaryTradingAlgo\\eta_engine\\.venv\\Scripts\\python.exe' "
                "-ArgumentList 'scripts\\jarvis_strategy_supervisor.py' "
                "-WorkingDirectory 'C:\\EvolutionaryTradingAlgo\\eta_engine' "
                f"-RedirectStandardOutput '{log_file}' "
                f"-RedirectStandardError '{log_file}.err' "
                "-WindowStyle Hidden",
            ],
            check=False,
            timeout=20,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("supervisor restart failed: %s", exc)
        return False


def _emit_cutover_event(perms_summary: dict[str, object]) -> None:
    """Drop a v3 event so Hermes pings the operator's Telegram."""
    try:
        from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event

        emit_event(
            layer="ops",
            event="live_crypto_activated",
            bot_id="",
            cls="crypto",
            details={
                "perms": perms_summary,
                "env_flipped": True,
                "supervisor_restarted": True,
            },
            severity="INFO",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("v3 event emit failed: %s", exc)


def _write_status(status: dict[str, object]) -> None:
    path = Path(
        os.getenv(
            "ETA_CUTOVER_STATUS_FILE",
            str(workspace_roots.ETA_CUTOVER_STATUS_PATH),
        )
    )
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually flip env + restart if perms are active. Otherwise log only.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    perms_active, summary = _query_crypto_permission()
    env_active = _crypto_currently_enabled_in_env()
    auto_opted_in = os.getenv("ETA_AUTO_CRYPTO_CUTOVER", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    status = {
        "checked_at": datetime.now(UTC).isoformat(),
        "perms_active": perms_active,
        "env_active": env_active,
        "auto_opted_in": auto_opted_in,
        "summary": summary,
        "action": "noop",
    }

    if perms_active and not env_active:
        if args.apply and auto_opted_in:
            logger.info("ACTIVATING live crypto: env flip + supervisor restart")
            flipped = _enable_crypto_in_env()
            restarted = _restart_supervisor() if flipped else False
            _emit_cutover_event(summary)
            status["action"] = "activated"
            status["env_flipped"] = flipped
            status["supervisor_restarted"] = restarted
        else:
            logger.info(
                "PERMS ACTIVE but auto-cutover NOT applied (--apply=%s, ETA_AUTO_CRYPTO_CUTOVER=%s)",
                args.apply,
                auto_opted_in,
            )
            status["action"] = "ready_pending_opt_in"
            # Always emit the v3 event so the operator knows perms flipped
            try:
                from eta_engine.brain.jarvis_v3.policies._v3_events import emit_event

                emit_event(
                    layer="ops",
                    event="crypto_perms_ready",
                    bot_id="",
                    cls="crypto",
                    details={"summary": summary, "auto_opted_in": auto_opted_in},
                    severity="INFO",
                )
            except Exception:  # noqa: BLE001
                pass
    elif perms_active and env_active:
        status["action"] = "already_live"
    elif not perms_active:
        logger.info(
            "perms_active=False (Cryptocurrency@USD=%s) — IBKR side still off",
            summary.get("Cryptocurrency@USD", "?"),
        )
        status["action"] = "waiting_for_ibkr"

    _write_status(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
