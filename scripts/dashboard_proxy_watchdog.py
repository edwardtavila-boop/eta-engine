"""Dashboard proxy watchdog for the public ops bridge.

The Cloudflare route reaches the ETA dashboard through the local
``ETA-Proxy-8421`` compatibility bridge. Task Scheduler can report that bridge
as successful even when the process has exited, so this watchdog probes the
local URL and restarts the bridge task when the dashboard is not reachable.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.uptime_events import record_uptime_event  # noqa: E402

logger = logging.getLogger("dashboard_proxy_watchdog")

DEFAULT_URL = os.getenv("ETA_DASHBOARD_PROXY_URL", "http://127.0.0.1:8421/")
DEFAULT_EXPECT_TEXT = os.getenv("ETA_DASHBOARD_PROXY_EXPECT_TEXT", "Portfolio Command")
DEFAULT_TIMEOUT_S = float(os.getenv("ETA_DASHBOARD_PROXY_TIMEOUT_S", "5"))
DEFAULT_TASK_NAME = os.getenv("ETA_DASHBOARD_PROXY_TASK_NAME", "ETA-Proxy-8421")
DEFAULT_INTERVAL_S = float(os.getenv("ETA_DASHBOARD_PROXY_WATCHDOG_INTERVAL_S", "60"))
DEFAULT_RESTART_DELAY_S = float(os.getenv("ETA_DASHBOARD_PROXY_RESTART_DELAY_S", "3"))
DEFAULT_HEARTBEAT_PATH = (
    workspace_roots.ETA_RUNTIME_STATE_DIR / "dashboard_proxy_watchdog_heartbeat.json"
)


@dataclass(slots=True)
class ProxyProbe:
    """Result of one dashboard proxy probe."""

    healthy: bool
    url: str
    status_code: int | None
    reason: str
    elapsed_ms: int
    body_len: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ProxyWatchdogDecision:
    """Structured output for one watchdog tick."""

    checked_at: str
    action: str
    task_name: str
    probe: ProxyProbe
    restart_ok: bool | None = None
    restart_reason: str | None = None
    post_restart_probe: ProxyProbe | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["probe"] = self.probe.to_dict()
        if self.post_restart_probe is not None:
            payload["post_restart_probe"] = self.post_restart_probe.to_dict()
        return payload


def probe_dashboard_proxy(
    *,
    url: str = DEFAULT_URL,
    expect_text: str = DEFAULT_EXPECT_TEXT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ProxyProbe:
    """Probe the local dashboard bridge and validate the expected page marker."""
    started = time.monotonic()
    status_code: int | None = None
    body = ""
    try:
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status_code = int(response.status)
            body = response.read(1_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        with contextlib.suppress(Exception):
            body = exc.read(16_384).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - watchdog must be fail-soft.
        return ProxyProbe(
            healthy=False,
            url=url,
            status_code=status_code,
            reason=f"probe_error:{type(exc).__name__}:{exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
            body_len=0,
        )

    if status_code != 200:
        reason = f"unexpected_status:{status_code}"
        healthy = False
    elif expect_text and expect_text not in body:
        reason = "missing_expected_text"
        healthy = False
    elif "Error code 502" in body or "Bad gateway" in body:
        reason = "bad_gateway_body"
        healthy = False
    else:
        reason = "ok"
        healthy = True

    return ProxyProbe(
        healthy=healthy,
        url=url,
        status_code=status_code,
        reason=reason,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        body_len=len(body),
    )


def restart_proxy_task(task_name: str = DEFAULT_TASK_NAME) -> tuple[bool, str]:
    """Start the Scheduled Task that owns the 8421 proxy bridge."""
    try:
        result = subprocess.run(  # noqa: S603, S607 - fixed Windows command.
            ["schtasks", "/Run", "/TN", task_name],
            capture_output=True,
            timeout=30,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return False, "schtasks_not_found"
    except Exception as exc:  # noqa: BLE001
        return False, f"schtasks_failed:{type(exc).__name__}:{exc}"

    if result.returncode == 0:
        return True, "schtasks_run_ok"
    message = (result.stderr or result.stdout or "").strip().replace("\n", " ")
    return False, f"schtasks_rc={result.returncode}:{message[:240]}"


def run_once(
    *,
    url: str = DEFAULT_URL,
    expect_text: str = DEFAULT_EXPECT_TEXT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    task_name: str = DEFAULT_TASK_NAME,
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
    restart_delay_s: float = DEFAULT_RESTART_DELAY_S,
    probe_fn: Callable[..., ProxyProbe] = probe_dashboard_proxy,
    restart_fn: Callable[[str], tuple[bool, str]] = restart_proxy_task,
) -> ProxyWatchdogDecision:
    """Run one proxy watchdog tick and write the canonical heartbeat."""
    probe = probe_fn(url=url, expect_text=expect_text, timeout_s=timeout_s)
    decision = ProxyWatchdogDecision(
        checked_at=datetime.now(UTC).isoformat(),
        action="noop" if probe.healthy else "restart_requested",
        task_name=task_name,
        probe=probe,
    )

    if not probe.healthy:
        ok, reason = restart_fn(task_name)
        decision.restart_ok = ok
        decision.restart_reason = reason
        decision.action = "restarted" if ok else "restart_failed"
        if ok and restart_delay_s > 0:
            time.sleep(restart_delay_s)
            decision.post_restart_probe = probe_fn(
                url=url,
                expect_text=expect_text,
                timeout_s=timeout_s,
            )

    _record(decision)
    _write_heartbeat(heartbeat_path, decision)
    return decision


def _record(decision: ProxyWatchdogDecision) -> None:
    with contextlib.suppress(Exception):
        record_uptime_event(
            component="dashboard_proxy_watchdog",
            event=decision.action,
            reason=decision.probe.reason,
            extra=decision.to_dict(),
        )


def _write_heartbeat(path: Path, decision: ProxyWatchdogDecision) -> None:
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "component": "dashboard_proxy_watchdog",
        "decision": decision.to_dict(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dashboard proxy watchdog heartbeat write failed: %s", exc)


def _exit_code(decision: ProxyWatchdogDecision) -> int:
    if decision.probe.healthy:
        return 0
    if decision.restart_ok is not True:
        return 2
    if decision.post_restart_probe is not None and not decision.post_restart_probe.healthy:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--expect-text", default=DEFAULT_EXPECT_TEXT)
    parser.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--interval-s", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--restart-delay-s", type=float, default=DEFAULT_RESTART_DELAY_S)
    parser.add_argument("--heartbeat-path", type=Path, default=DEFAULT_HEARTBEAT_PATH)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    def tick() -> ProxyWatchdogDecision:
        return run_once(
            url=args.url,
            expect_text=args.expect_text,
            timeout_s=args.timeout_s,
            task_name=args.task_name,
            heartbeat_path=args.heartbeat_path,
            restart_delay_s=args.restart_delay_s,
        )

    if args.once:
        decision = tick()
        if args.json:
            print(json.dumps(decision.to_dict(), indent=2))
        else:
            logger.info(
                "dashboard proxy watchdog: action=%s reason=%s restart_ok=%s",
                decision.action,
                decision.probe.reason,
                decision.restart_ok,
            )
        return _exit_code(decision)

    while True:
        try:
            decision = tick()
            logger.info(
                "dashboard proxy watchdog: action=%s reason=%s restart_ok=%s",
                decision.action,
                decision.probe.reason,
                decision.restart_ok,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("dashboard proxy watchdog tick failed: %s", exc)
        time.sleep(max(5.0, float(args.interval_s)))


if __name__ == "__main__":
    raise SystemExit(main())
