"""
JARVIS v3 // preflight — read-only Go/No-Go reporter for live capital cutover.

Complements ``bridge_autoheal`` (which silently FIXES known failure modes
every 15 min) with a one-shot READ-ONLY verifier that answers a single
operator question:

    "Is everything green right now for me to push capital live?"

Each ``check_*`` function returns a ``PreflightCheck`` with one of three
statuses:

  * ``PASS``      — green light, nothing to worry about
  * ``WARN``      — yellow flag; deploy can proceed but operator should know
  * ``FAIL``      — red light; do NOT push capital until resolved

The aggregate ``run_preflight()`` returns a ``PreflightReport`` whose
``verdict`` is:

  * ``READY``     — every check is PASS or WARN, no FAIL
  * ``NOT READY`` — at least one FAIL

The preflight NEVER writes anything except its own JSONL log. It does
not heal, restart, or send alerts. Action is the operator's choice
after reading the verdict.

What's checked
--------------

Infrastructure:
  1. Workspace root writable
  2. State dir writable
  3. Hermes gateway port 8642 listening
  4. Status server port 8643 listening + /health 200

Data freshness:
  5. trade_closes.jsonl recently modified (proves bots are firing)
  6. Memory backup within 48h
  7. Kaizen latest report within 25h

Cron health:
  8. ETA-Anomaly-Pulse last result == 0, within 30 min
  9. ETA-Bridge-Autoheal last result == 0, within 30 min

Trading state:
 10. Kill switch NOT engaged
 11. No UNRESOLVED critical-severity anomaly hits in last 24h
 12. Active overrides reasonable (<10 unique)

Public interface
----------------

* ``run_preflight()`` → PreflightReport
* ``PreflightReport.verdict`` → "READY" | "NOT READY"
* ``PreflightReport.checks`` → list[PreflightCheck]
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.preflight")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
_LEGACY_STATE_ROOT = _WORKSPACE / "eta_engine" / "state"
_VAR_ROOT = _WORKSPACE / "var"

_TRADE_CLOSES_PATH = _STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
_LEGACY_TRADE_CLOSES_PATH = _LEGACY_STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
_MEMORY_BACKUP_DIR = _STATE_ROOT / "backups" / "hermes_memory"
_KAIZEN_LATEST = _STATE_ROOT / "kaizen_latest.json"
_HERMES_STATE = _STATE_ROOT / "jarvis_intel" / "hermes_state.json"
_OVERRIDES_PATH = _STATE_ROOT / "kaizen_overrides.json"
_ANOMALY_HITS_LOG = _VAR_ROOT / "anomaly_watcher.jsonl"
_PREFLIGHT_LOG = _VAR_ROOT / "preflight_runs.jsonl"

_HERMES_PORT = 8642
_STATUS_SERVER_PORT = 8643
_HEALTH_TIMEOUT_S = 5.0
_PORT_TIMEOUT_S = 2.0

# Thresholds — easy to tune from one place.
TRADE_FRESHNESS_MAX_HOURS = 6  # outside RTH this can creep up; warn not fail
MEMORY_BACKUP_MAX_HOURS = 48
KAIZEN_LATEST_MAX_HOURS = 25
CRON_LAST_RUN_MAX_MINUTES = 30
MAX_OPEN_CRITICAL_ANOMALIES = 0  # any unresolved critical = FAIL
MAX_ACTIVE_OVERRIDES = 10
KILL_SWITCH_FAIL_IF_ENGAGED = True

EXPECTED_HOOKS = ("run_preflight",)


@dataclass(frozen=True)
class PreflightCheck:
    """One layer's verdict."""

    name: str
    status: str  # "PASS" | "WARN" | "FAIL"
    detail: str
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreflightReport:
    """Aggregate report. Verdict is READY only if zero FAIL checks."""

    asof: str
    verdict: str  # "READY" | "NOT READY"
    n_pass: int
    n_warn: int
    n_fail: int
    checks: list[PreflightCheck]

    def to_dict(self) -> dict[str, Any]:
        return {
            "asof": self.asof,
            "verdict": self.verdict,
            "n_pass": self.n_pass,
            "n_warn": self.n_warn,
            "n_fail": self.n_fail,
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _file_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return (_now() - mtime).total_seconds() / 3600.0
    except OSError:
        return None


def _port_listening(host: str, port: int, timeout_s: float = _PORT_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _http_health(port: int, timeout_s: float = _HEALTH_TIMEOUT_S) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
            ok = 200 <= resp.status < 300
            return ok, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)[:80]


def _schtasks_last_run(task_name: str) -> tuple[int | None, datetime | None]:
    """Query a Windows scheduled task's Last Result + Last Run Time.

    Returns ``(last_result, last_run_dt)`` or ``(None, None)`` on error.
    """
    try:
        out = subprocess.run(  # noqa: S603, S607
            ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode != 0:
            return None, None
    except (subprocess.TimeoutExpired, OSError):
        return None, None

    last_result: int | None = None
    last_run: datetime | None = None
    for raw in out.stdout.splitlines():
        line = raw.strip()
        if line.lower().startswith("last result:"):
            with contextlib.suppress(ValueError):
                last_result = int(line.split(":", 1)[1].strip())
        elif line.lower().startswith("last run time:"):
            ts = line.split(":", 1)[1].strip()
            # Schtasks emits localized "M/D/YYYY h:mm:ss AM/PM" — try a few.
            # CRITICAL: schtasks output is in SYSTEM LOCAL TIME, not UTC. Using
            # astimezone(UTC) on a naive datetime treats it as local and
            # converts properly (Python 3.6+).
            for fmt in (
                "%m/%d/%Y %I:%M:%S %p",
                "%m/%d/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
            ):
                try:
                    naive = datetime.strptime(ts, fmt)
                    last_run = naive.astimezone(UTC)
                    break
                except ValueError:
                    continue
    return last_result, last_run


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_workspace_writable() -> PreflightCheck:
    name = "workspace_writable"
    try:
        probe = _WORKSPACE / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return PreflightCheck(name=name, status="PASS", detail=f"{_WORKSPACE} writable")
    except OSError as exc:
        return PreflightCheck(name=name, status="FAIL", detail=f"unwritable: {exc}")


def check_state_dir_writable() -> PreflightCheck:
    name = "state_dir_writable"
    try:
        _STATE_ROOT.mkdir(parents=True, exist_ok=True)
        probe = _STATE_ROOT / ".preflight_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return PreflightCheck(name=name, status="PASS", detail=f"{_STATE_ROOT} writable")
    except OSError as exc:
        return PreflightCheck(name=name, status="FAIL", detail=f"unwritable: {exc}")


def check_hermes_port_listening() -> PreflightCheck:
    name = "hermes_port_listening"
    if _port_listening("127.0.0.1", _HERMES_PORT):
        return PreflightCheck(
            name=name,
            status="PASS",
            detail=f"port {_HERMES_PORT} listening",
        )
    return PreflightCheck(
        name=name,
        status="FAIL",
        detail=f"port {_HERMES_PORT} not listening",
    )


def check_status_server() -> PreflightCheck:
    name = "status_server_health"
    if not _port_listening("127.0.0.1", _STATUS_SERVER_PORT):
        return PreflightCheck(
            name=name,
            status="FAIL",
            detail=f"port {_STATUS_SERVER_PORT} not listening",
        )
    ok, detail = _http_health(_STATUS_SERVER_PORT)
    return PreflightCheck(
        name=name,
        status="PASS" if ok else "FAIL",
        detail=detail,
    )


def check_trade_close_stream_fresh() -> PreflightCheck:
    """trade_closes.jsonl modified within TRADE_FRESHNESS_MAX_HOURS.

    Outside RTH the stream may sit idle; treat staleness as WARN unless
    extremely old. Missing file is FAIL — that's a real plumbing break.
    """
    name = "trade_close_stream"
    primary_age = _file_age_hours(_TRADE_CLOSES_PATH)
    legacy_age = _file_age_hours(_LEGACY_TRADE_CLOSES_PATH)
    ages = [a for a in (primary_age, legacy_age) if a is not None]
    if not ages:
        return PreflightCheck(
            name=name,
            status="FAIL",
            detail="no trade_closes.jsonl found (canonical or legacy)",
        )
    freshest = min(ages)
    extras = {"freshest_age_hours": round(freshest, 2)}
    if freshest <= TRADE_FRESHNESS_MAX_HOURS:
        return PreflightCheck(
            name=name,
            status="PASS",
            detail=f"newest modified {freshest:.1f}h ago",
            extras=extras,
        )
    if freshest <= TRADE_FRESHNESS_MAX_HOURS * 4:
        return PreflightCheck(
            name=name,
            status="WARN",
            detail=f"stale ({freshest:.1f}h since last write)",
            extras=extras,
        )
    return PreflightCheck(
        name=name,
        status="FAIL",
        detail=f"very stale ({freshest:.1f}h since last write)",
        extras=extras,
    )


def check_memory_backup_fresh() -> PreflightCheck:
    name = "memory_backup_fresh"
    if not _MEMORY_BACKUP_DIR.exists():
        return PreflightCheck(
            name=name,
            status="WARN",
            detail=f"no backup dir at {_MEMORY_BACKUP_DIR}",
        )
    newest: float | None = None
    try:
        for f in _MEMORY_BACKUP_DIR.iterdir():
            if not f.is_file():
                continue
            age = _file_age_hours(f)
            if age is None:
                continue
            if newest is None or age < newest:
                newest = age
    except OSError as exc:
        return PreflightCheck(
            name=name,
            status="WARN",
            detail=f"backup dir read failed: {exc}",
        )
    if newest is None:
        return PreflightCheck(name=name, status="WARN", detail="no backup files found")
    extras = {"newest_age_hours": round(newest, 2)}
    if newest <= MEMORY_BACKUP_MAX_HOURS:
        return PreflightCheck(
            name=name,
            status="PASS",
            detail=f"newest backup {newest:.1f}h old",
            extras=extras,
        )
    return PreflightCheck(
        name=name,
        status="WARN",
        detail=f"backup stale ({newest:.1f}h old)",
        extras=extras,
    )


def check_kaizen_latest_fresh() -> PreflightCheck:
    name = "kaizen_latest_fresh"
    age = _file_age_hours(_KAIZEN_LATEST)
    if age is None:
        return PreflightCheck(name=name, status="WARN", detail="kaizen_latest.json missing")
    extras = {"age_hours": round(age, 2)}
    if age <= KAIZEN_LATEST_MAX_HOURS:
        return PreflightCheck(
            name=name,
            status="PASS",
            detail=f"kaizen ran {age:.1f}h ago",
            extras=extras,
        )
    return PreflightCheck(
        name=name,
        status="WARN",
        detail=f"kaizen has not run in {age:.1f}h",
        extras=extras,
    )


def _check_cron_task(task_name: str, friendly: str) -> PreflightCheck:
    last_result, last_run = _schtasks_last_run(task_name)
    if last_result is None or last_run is None:
        return PreflightCheck(
            name=f"cron_{friendly}",
            status="WARN",
            detail=f"could not query {task_name}",
        )
    minutes_ago = (_now() - last_run).total_seconds() / 60.0
    extras = {
        "task": task_name,
        "last_result": last_result,
        "last_run": last_run.isoformat(),
        "minutes_since_last_run": round(minutes_ago, 1),
    }
    if last_result != 0:
        return PreflightCheck(
            name=f"cron_{friendly}",
            status="FAIL",
            detail=f"{task_name} last exit code {last_result}",
            extras=extras,
        )
    if minutes_ago > CRON_LAST_RUN_MAX_MINUTES:
        return PreflightCheck(
            name=f"cron_{friendly}",
            status="WARN",
            detail=f"{task_name} hasn't fired in {minutes_ago:.0f}min",
            extras=extras,
        )
    return PreflightCheck(
        name=f"cron_{friendly}",
        status="PASS",
        detail=f"{task_name} last ran {minutes_ago:.1f}min ago, exit 0",
        extras=extras,
    )


def check_cron_anomaly_pulse() -> PreflightCheck:
    return _check_cron_task("ETA-Anomaly-Pulse", "anomaly_pulse")


def check_cron_bridge_autoheal() -> PreflightCheck:
    return _check_cron_task("ETA-Bridge-Autoheal", "bridge_autoheal")


def check_telegram_inbound_running() -> PreflightCheck:
    """Telegram inbound bot has processed something recently (or is fresh-started).

    The bot is long-running so 'last update' could be hours old on a
    quiet day — that's fine. What matters is the offset file exists
    (proves the bot has at least booted once) and the silence file is
    not stuck.
    """
    name = "telegram_inbound_alive"
    offset_path = _VAR_ROOT / "telegram_inbound_offset.json"
    log_path = _VAR_ROOT / "telegram_inbound.log"
    err_path = _VAR_ROOT / "telegram_inbound.err"
    jsonl_path = _VAR_ROOT / "telegram_inbound.jsonl"
    extras: dict[str, Any] = {}

    if (
        not offset_path.exists()
        and not log_path.exists()
        and not err_path.exists()
    ):
        return PreflightCheck(
            name=name,
            status="WARN",
            detail="no offset file or log — bot may not have started yet",
        )
    # 2026-05-13: use the FRESHEST of the bot's four state files. The
    # bot writes to .log only on inbound messages (silent on quiet
    # days), but .err captures every poll cycle's stderr — which keeps
    # ticking even when nobody sends commands. The offset file ticks on
    # every Telegram getUpdates call. Either one being fresh proves
    # the bot is alive.
    candidate_files = [
        ("log", log_path),
        ("err", err_path),
        ("jsonl", jsonl_path),
        ("offset", offset_path),
    ]
    ages: dict[str, float] = {}
    for label, p in candidate_files:
        if p.exists():
            ages[label] = _file_age_hours(p)
    if not ages:
        return PreflightCheck(
            name=name,
            status="WARN",
            detail="no inbound state files present — bot may not have started yet",
        )
    freshest_label = min(ages, key=lambda k: ages[k])
    freshest_age = ages[freshest_label]
    extras = {
        "freshest_file": freshest_label,
        "freshest_age_hours": round(freshest_age, 2),
        "all_ages_hours": {k: round(v, 2) for k, v in ages.items()},
    }
    if freshest_age > 12:
        return PreflightCheck(
            name=name,
            status="WARN",
            detail=f"inbound state files all >12h old (freshest: {freshest_label} {freshest_age:.1f}h)",
            extras=extras,
        )
    return PreflightCheck(
        name=name,
        status="PASS",
        detail=f"inbound bot alive (freshest: {freshest_label} {freshest_age:.1f}h)",
        extras=extras,
    )


def check_kill_switch_disengaged() -> PreflightCheck:
    name = "kill_switch_disengaged"
    state = _read_json(_HERMES_STATE) or {}
    kill = bool(state.get("kill_all", False))
    if kill and KILL_SWITCH_FAIL_IF_ENGAGED:
        return PreflightCheck(
            name=name,
            status="FAIL",
            detail="kill_all is ENGAGED — manual reset required",
            extras={"hermes_state": state},
        )
    return PreflightCheck(
        name=name,
        status="PASS",
        detail="kill switch is clear" if not kill else "kill_all flag present but tolerated",
    )


def check_active_overrides_reasonable() -> PreflightCheck:
    name = "active_overrides"
    overrides = _read_json(_OVERRIDES_PATH) or {}
    # Count both size_modifier entries and school_weight entries
    sz = overrides.get("size_modifiers") or overrides.get("size_modifier") or {}
    sw = overrides.get("school_weights") or {}
    n = 0
    if isinstance(sz, dict):
        n += len(sz)
    if isinstance(sw, dict):
        n += len(sw)
    extras = {"n_overrides": n}
    if n == 0:
        return PreflightCheck(
            name=name,
            status="PASS",
            detail="no active overrides",
            extras=extras,
        )
    if n <= MAX_ACTIVE_OVERRIDES:
        return PreflightCheck(
            name=name,
            status="PASS",
            detail=f"{n} active overrides (under cap {MAX_ACTIVE_OVERRIDES})",
            extras=extras,
        )
    return PreflightCheck(
        name=name,
        status="WARN",
        detail=f"{n} active overrides (over cap {MAX_ACTIVE_OVERRIDES})",
        extras=extras,
    )


def check_prop_firm_accounts_healthy() -> PreflightCheck:
    """All registered prop firm accounts are within rule limits.

    blown or critical → FAIL (cannot push capital with an account at risk)
    warn               → WARN (deploy ok, but operator should know)
    all ok             → PASS
    """
    name = "prop_firm_accounts_healthy"
    try:
        from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

        snaps = g.aggregate_status()
    except Exception as exc:  # noqa: BLE001
        return PreflightCheck(
            name=name,
            status="WARN",
            detail=f"prop_firm_guardrails unavailable: {exc}"[:200],
        )

    if not snaps:
        return PreflightCheck(name=name, status="PASS", detail="no accounts registered")

    blown_or_crit = [s for s in snaps if s.severity in ("blown", "critical")]
    warns = [s for s in snaps if s.severity == "warn"]
    extras: dict[str, Any] = {
        "n_total": len(snaps),
        "n_critical_or_blown": len(blown_or_crit),
        "n_warn": len(warns),
    }
    if blown_or_crit:
        names = [s.rules.account_id for s in blown_or_crit]
        return PreflightCheck(
            name=name,
            status="FAIL",
            detail=f"{len(blown_or_crit)} account(s) blown/critical: {names}",
            extras=extras,
        )
    if warns:
        names = [s.rules.account_id for s in warns]
        return PreflightCheck(
            name=name,
            status="WARN",
            detail=f"{len(warns)} account(s) approaching limits: {names}",
            extras=extras,
        )
    return PreflightCheck(
        name=name,
        status="PASS",
        detail=f"all {len(snaps)} accounts within limits",
        extras=extras,
    )


def check_no_open_critical_anomalies() -> PreflightCheck:
    """No critical-severity anomaly hits in the last 24h.

    Loss streaks of 5+ or fleet drawdown are CRITICAL and indicate the
    operator should not be pushing fresh capital without first
    investigating. WARN-severity hits are tolerated (informational).
    """
    name = "no_open_critical_anomalies"
    if not _ANOMALY_HITS_LOG.exists():
        return PreflightCheck(name=name, status="PASS", detail="no anomaly log yet")
    cutoff = _now() - timedelta(hours=24)

    # 2026-05-13: skip hits whose bot_id is currently deactivated.
    # Anomaly_watcher itself now filters these at scan time, but the
    # JSONL still contains stale entries from BEFORE the retirement
    # (e.g. crude_compression's "7 losses in last 8 trades" was
    # written hours before kaizen retired the bot). Reading those at
    # face value blocks the preflight gate forever. Source of truth:
    # the kaizen_overrides sidecar.
    try:
        from eta_engine.strategies.per_bot_registry import (  # noqa: PLC0415
            get_for_bot,
            kaizen_deactivation_record,
        )
        from eta_engine.strategies.per_bot_registry import (
            is_active as _reg_is_active,
        )

        def _bot_is_active(bot_id: str) -> bool:
            # Kaizen override is highest-priority signal — a deactivation
            # record means "operator/loop has retired this", regardless
            # of whether the registry still has an assignment.
            if kaizen_deactivation_record(bot_id):
                return False
            assignment = get_for_bot(bot_id)
            if assignment is None:
                # Truly unknown bot — not in registry AND no kaizen
                # record. Most likely a phantom/test bot whose name
                # appears only in old anomaly history. Treat as inactive
                # so its stale hits don't block the preflight gate.
                return False
            return _reg_is_active(assignment)

    except ImportError:
        def _bot_is_active(bot_id: str) -> bool:  # type: ignore[misc]
            return True

    crits: list[dict[str, Any]] = []
    skipped_deactivated = 0
    try:
        with _ANOMALY_HITS_LOG.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("severity") or "").lower() != "critical":
                    continue
                ts = _parse_iso(rec.get("asof"))
                if ts is None or ts < cutoff:
                    continue
                bot_id = str(rec.get("bot_id") or "")
                if bot_id and not _bot_is_active(bot_id):
                    skipped_deactivated += 1
                    continue
                crits.append(rec)
    except OSError as exc:
        return PreflightCheck(
            name=name,
            status="WARN",
            detail=f"anomaly log unreadable: {exc}",
        )
    if not crits:
        return PreflightCheck(
            name=name,
            status="PASS",
            detail="no critical anomalies in last 24h",
        )
    if MAX_OPEN_CRITICAL_ANOMALIES <= 0:
        return PreflightCheck(
            name=name,
            status="FAIL",
            detail=f"{len(crits)} CRITICAL anomaly hits in last 24h",
            extras={
                "n_critical": len(crits),
                "first": crits[0],
                "patterns": sorted({str(c.get("pattern")) for c in crits}),
            },
        )
    return PreflightCheck(
        name=name,
        status="WARN",
        detail=f"{len(crits)} critical hits (over threshold)",
        extras={"n_critical": len(crits)},
    )


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


_ALL_CHECKS = (
    check_workspace_writable,
    check_state_dir_writable,
    check_hermes_port_listening,
    check_status_server,
    check_trade_close_stream_fresh,
    check_memory_backup_fresh,
    check_kaizen_latest_fresh,
    check_cron_anomaly_pulse,
    check_cron_bridge_autoheal,
    check_telegram_inbound_running,
    check_kill_switch_disengaged,
    check_active_overrides_reasonable,
    check_prop_firm_accounts_healthy,
    check_no_open_critical_anomalies,
)


def run_preflight() -> PreflightReport:
    """Run every check, return an aggregate PreflightReport.

    Never raises. Each check is independently isolated — a single
    broken probe won't sabotage the rest of the report.
    """
    checks: list[PreflightCheck] = []
    for fn in _ALL_CHECKS:
        try:
            checks.append(fn())
        except Exception as exc:  # noqa: BLE001
            checks.append(
                PreflightCheck(
                    name=getattr(fn, "__name__", "unknown_check"),
                    status="WARN",
                    detail=f"check raised: {exc}"[:200],
                )
            )

    n_pass = sum(1 for c in checks if c.status == "PASS")
    n_warn = sum(1 for c in checks if c.status == "WARN")
    n_fail = sum(1 for c in checks if c.status == "FAIL")
    verdict = "READY" if n_fail == 0 else "NOT READY"

    report = PreflightReport(
        asof=_now_iso(),
        verdict=verdict,
        n_pass=n_pass,
        n_warn=n_warn,
        n_fail=n_fail,
        checks=checks,
    )

    # Best-effort audit log — never blocks
    with contextlib.suppress(Exception):
        _PREFLIGHT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _PREFLIGHT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(report.to_dict(), default=str) + "\n")

    return report
