"""Thin CLI wrapper for broker connection probes and targeted reconnect checks.

The repo already has canonical broker probe primitives in
``eta_engine.venues.connection``. This script restores the documented
operator entrypoint so runbooks and runtime hints can invoke a single,
stable command:

    python -m eta_engine.scripts.connect_brokers --probe
    python -m eta_engine.scripts.connect_brokers --reconnect ibkr

The implementation stays intentionally thin:

* build a ``BrokerConnectionManager`` from config + env/secrets,
* run a read-only probe sweep for all selected brokers,
* write the compact JSON report bundle under
  ``var/eta_engine/state/broker_connections/``,
* exit non-zero when any venue probe fails.

``--reconnect`` does not restart desktop software or kill sessions directly;
it re-runs the canonical connection/auth probe for the named venue so the
latest report and CLI result reflect current reachability.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow execution as both ``eta_engine.scripts.connect_brokers`` (from parent)
# and ``scripts/connect_brokers.py`` (from inside eta_engine/).
_ROOT = Path(__file__).resolve().parents[1]
_PARENT = _ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from eta_engine.venues.connection import (  # noqa: E402
    BrokerConnectionManager,
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUT_DIR,
    write_broker_connection_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--probe",
        action="store_true",
        help="explicitly run the default read-only broker probe sweep",
    )
    action.add_argument(
        "--reconnect",
        metavar="BROKER",
        help="re-run the canonical connection/auth probe for a single broker",
    )
    parser.add_argument(
        "--brokers",
        nargs="+",
        help="explicit broker list to probe instead of config-derived brokers",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"report output directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--stem",
        default="broker_connections",
        help="report stem for timestamped/latest JSON outputs",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the latest report payload as JSON to stdout",
    )
    bybit_group = parser.add_mutually_exclusive_group()
    bybit_group.add_argument(
        "--bybit-testnet",
        dest="bybit_testnet",
        action="store_true",
        default=None,
        help="force Bybit probes into testnet mode",
    )
    bybit_group.add_argument(
        "--bybit-live",
        dest="bybit_testnet",
        action="store_false",
        help="force Bybit probes into live mode",
    )
    tradovate_group = parser.add_mutually_exclusive_group()
    tradovate_group.add_argument(
        "--tradovate-demo",
        dest="tradovate_demo",
        action="store_true",
        default=None,
        help="force Tradovate probe wiring into demo mode",
    )
    tradovate_group.add_argument(
        "--tradovate-live",
        dest="tradovate_demo",
        action="store_false",
        help="force Tradovate probe wiring into live mode",
    )
    return parser


def _render_text_summary(payload: dict[str, object], latest: Path) -> str:
    summary = payload.get("summary") or {}
    reports = payload.get("reports") or []
    lines = [
        (
            "[connect-brokers] "
            f"health={summary.get('health', 'UNKNOWN')} "
            f"overall_ok={summary.get('overall_ok', False)} "
            f"configured={len(payload.get('configured_brokers') or [])} "
            f"report={latest.name}"
        )
    ]
    for report in reports:
        if not isinstance(report, dict):
            continue
        venue = report.get("venue", "unknown")
        status = report.get("status", "UNKNOWN")
        error = report.get("error") or ""
        detail = f" error={error}" if error else ""
        lines.append(f"[connect-brokers]   {venue}: {status}{detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.reconnect and args.brokers:
        parser.error("--reconnect cannot be combined with --brokers")

    selected = [args.reconnect] if args.reconnect else args.brokers
    source = "broker_reconnect" if args.reconnect else "broker_connect"

    manager = BrokerConnectionManager.from_env(
        config_path=args.config,
        bybit_testnet=args.bybit_testnet,
        tradovate_demo=args.tradovate_demo,
    )
    summary = asyncio.run(manager.connect(selected))
    summary.source = source
    _, latest = write_broker_connection_report(
        summary,
        out_dir=args.out_dir,
        stem=args.stem,
    )
    payload = json.loads(latest.read_text(encoding="utf-8"))

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(_render_text_summary(payload, latest))

    return 0 if summary.overall_ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
