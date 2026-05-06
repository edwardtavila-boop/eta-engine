"""Broker-router watchdog (24/7 framework, 2026-05-06).

Thin wrapper around :func:`eta_watchdog.watchdog_tick` that pre-targets
the broker_router heartbeat + process. Lets the operator register a
separate Windows scheduled task (``ETA-BrokerRouterWatchdog``) without
needing to remember ``--component broker_router``.

Behaviour matches :mod:`eta_engine.scripts.eta_watchdog`:

* Reads ``var/eta_engine/state/router/broker_router_heartbeat.json``.
* Looks for processes whose cmdline contains ``broker_router.py``.
* On stale heartbeat: terminates lingering broker_router processes and
  re-launches via the wrapper or Windows task ``ETA-BrokerRouter``.
* Honors the ``var/eta_engine/state/broker_router_disabled.txt`` opt-out.
* Stamps ``var/eta_engine/state/broker_router_watchdog_heartbeat.json``.

Configuration env
-----------------
* ``ETA_BROKER_ROUTER_WATCHDOG_TASK_NAME``      -- task name for relaunch
  (default ``ETA-BrokerRouter``).
* ``ETA_BROKER_ROUTER_WATCHDOG_WRAPPER_CMD``    -- direct wrapper invocation.
* ``ETA_BROKER_ROUTER_WATCHDOG_PROCESS_NAME``   -- cmdline match substring
  (default ``broker_router.py``).
* ``ETA_BROKER_ROUTER_WATCHDOG_STALE_S``        -- staleness threshold
  (default 300s).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402
from eta_engine.scripts.eta_watchdog import watchdog_tick  # noqa: E402

logger = logging.getLogger("eta_broker_router_watchdog")


def _broker_router_heartbeat_path() -> Path:
    """Resolve the broker_router heartbeat path.

    Operator overrides via ``ETA_BROKER_ROUTER_STATE_ROOT`` env, mirroring
    the supervisor's heartbeat resolution.
    """
    state_root = os.getenv("ETA_BROKER_ROUTER_STATE_ROOT", "").strip()
    if state_root:
        return Path(state_root) / "broker_router_heartbeat.json"
    return (
        workspace_roots.ETA_RUNTIME_STATE_DIR
        / "router"
        / "broker_router_heartbeat.json"
    )


def _disabled_flag_path() -> Path:
    return workspace_roots.ETA_RUNTIME_STATE_DIR / "broker_router_disabled.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit (default: 60s loop).",
    )
    parser.add_argument(
        "--interval-s",
        type=float,
        default=60.0,
        help="Loop interval seconds when not --once.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    stale_s = float(os.getenv("ETA_BROKER_ROUTER_WATCHDOG_STALE_S", "300"))

    common_kwargs = {
        "component": "broker_router",
        "heartbeat_path": _broker_router_heartbeat_path(),
        "keepalive_path": None,
        "process_substring": os.getenv(
            "ETA_BROKER_ROUTER_WATCHDOG_PROCESS_NAME",
            "broker_router.py",
        ),
        "stale_s": stale_s,
        "disabled_flag_path": _disabled_flag_path(),
        "watchdog_heartbeat_path": (
            workspace_roots.ETA_RUNTIME_STATE_DIR
            / "broker_router_watchdog_heartbeat.json"
        ),
        "task_name": os.getenv(
            "ETA_BROKER_ROUTER_WATCHDOG_TASK_NAME", "ETA-BrokerRouter",
        ),
        "wrapper_cmd": os.getenv("ETA_BROKER_ROUTER_WATCHDOG_WRAPPER_CMD") or None,
    }

    if args.once:
        decision = watchdog_tick(**common_kwargs)
        logger.info(
            "broker_router watchdog tick: action=%s heartbeat_age_s=%s",
            decision.action, decision.heartbeat_age_s,
        )
        return 0

    while True:
        try:
            decision = watchdog_tick(**common_kwargs)
            logger.info(
                "broker_router watchdog tick: action=%s heartbeat_age_s=%s",
                decision.action, decision.heartbeat_age_s,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("broker_router watchdog tick raised: %s", exc)
        time.sleep(max(1.0, float(args.interval_s)))


if __name__ == "__main__":
    sys.exit(main())
