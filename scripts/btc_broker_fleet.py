"""Start and monitor the four-lane BTC broker-paper fleet.

The fleet intentionally does not submit broker orders. Tastytrade and IBKR are
probed with their real paper/sandbox adapters, then each BTC lane runs as a
separate broker-paper worker with its own paper bankroll and heartbeat.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.scripts import btc_paper_lane as _btc_paper_lane  # noqa: E402
from eta_engine.venues.base import ConnectionStatus  # noqa: E402
from eta_engine.venues.connection import BrokerConnectionManager  # noqa: E402
from eta_engine.venues.ibkr import IbkrClientPortalConfig  # noqa: E402
from eta_engine.venues.tastytrade import TastytradeConfig  # noqa: E402

PaperLaneRunner = _btc_paper_lane.PaperLaneRunner
run_one_tick = _btc_paper_lane.run_one_tick
shutdown_lane = _btc_paper_lane.shutdown

DEFAULT_OUT_DIR = ROOT / "docs" / "btc_live" / "broker_fleet"
DEFAULT_STARTING_CASH = 5_000.0
DEFAULT_HEARTBEAT_INTERVAL_S = 5.0
FLEET_MANIFEST = "btc_broker_fleet_latest.json"
PAPER_TRADE_LEDGER = "btc_paper_trades.jsonl"
PAPER_BROKER_ORDER_ROUTING = "paper_broker_enabled"
OPEN_POSITION_STATUSES = {"OPEN", "PARTIAL"}


@dataclass(frozen=True)
class FleetWorkerSpec:
    worker_id: str
    broker: str
    lane: str
    symbol: str = "BTCUSD"
    paper_starting_cash: float = DEFAULT_STARTING_CASH


def fleet_workers(starting_cash: float = DEFAULT_STARTING_CASH) -> list[FleetWorkerSpec]:
    """Return the canonical four BTC broker-paper workers."""

    return [
        FleetWorkerSpec("btc-directional-tastytrade", "tastytrade", "directional", paper_starting_cash=starting_cash),
        FleetWorkerSpec("btc-directional-ibkr", "ibkr", "directional", paper_starting_cash=starting_cash),
        FleetWorkerSpec("btc-grid-tastytrade", "tastytrade", "grid", paper_starting_cash=starting_cash),
        FleetWorkerSpec("btc-grid-ibkr", "ibkr", "grid", paper_starting_cash=starting_cash),
    ]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def worker_status_path(out_dir: Path, worker_id: str) -> Path:
    return out_dir / f"{worker_id}.json"


def worker_ledger_path(out_dir: Path) -> Path:
    return out_dir / PAPER_TRADE_LEDGER


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_trade_for_worker(out_dir: Path, worker_id: str) -> dict[str, Any]:
    """Return the latest paper-trade ledger row for a worker, if one exists."""

    latest: dict[str, Any] = {}
    ledger_path = worker_ledger_path(out_dir)
    if not ledger_path.exists():
        return latest
    try:
        rows = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return latest
    for row in rows:
        try:
            payload = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("worker_id") == worker_id:
            latest = payload
    return latest


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def probe_required_brokers(required: list[str] | None = None) -> dict[str, Any]:
    manager = BrokerConnectionManager.from_env()
    summary = await manager.connect(required or ["tastytrade", "ibkr"])
    reports = {
        report.venue: {
            "status": report.status.value,
            "creds_present": report.creds_present,
            "positions_count": report.positions_count,
        }
        for report in summary.reports
    }
    ready = sorted(report.venue for report in summary.reports if report.status is ConnectionStatus.READY)
    missing = sorted(set(required or ["tastytrade", "ibkr"]) - set(ready))
    return {
        "health": summary.health(),
        "overall_ok": summary.overall_ok(),
        "ready": ready,
        "missing_ready": missing,
        "reports": reports,
    }


def build_position_state(spec: FleetWorkerSpec, latest_trade: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize paper-trade state so dashboards can answer "am I in a trade?"."""

    trade = latest_trade or {}
    status = str(trade.get("status") or "FLAT").upper()
    in_trade = status in OPEN_POSITION_STATUSES
    return {
        "in_trade": in_trade,
        "status": status if trade else "FLAT",
        "symbol": trade.get("symbol") or spec.symbol,
        "side": trade.get("side") if in_trade else None,
        "qty": trade.get("qty", 0) if in_trade else 0,
        "entry_price": trade.get("entry_price") if in_trade else None,
        "unrealized_pnl": trade.get("unrealized_pnl", 0.0) if in_trade else 0.0,
        "last_order_id": trade.get("order_id"),
        "broker": trade.get("broker") or spec.broker,
        "lane": trade.get("lane") or spec.lane,
        "updated_at_utc": trade.get("updated_at_utc") or trade.get("ts_utc"),
    }


def build_last_order(latest_trade: dict[str, Any] | None = None) -> dict[str, Any]:
    trade = latest_trade or {}
    return {
        "has_order": bool(trade.get("order_id")),
        "order_id": trade.get("order_id"),
        "status": trade.get("order_status") or trade.get("status"),
        "side": trade.get("side"),
        "qty": trade.get("qty", 0),
        "broker": trade.get("broker"),
        "ts_utc": trade.get("order_ts_utc") or trade.get("ts_utc") or trade.get("updated_at_utc"),
    }


def build_execution_profile(spec: FleetWorkerSpec) -> dict[str, Any]:
    """Describe whether this lane can honestly submit and reconcile broker-paper orders."""

    try:
        if spec.broker == "tastytrade":
            config = TastytradeConfig.from_env()
            missing = config.missing_requirements()
            if missing:
                return {
                    "execution_state": "BLOCKED",
                    "order_submission_ready": False,
                    "fill_lifecycle_ready": False,
                    "execution_capability": "missing_broker_paper_config",
                    "execution_blocker": "; ".join(missing),
                    "symbol_contract_id": None,
                }
            # v0.1.58: Tastytrade adapter now hits the cert REST API for
            # real order placement + status polling. ``fill_lifecycle_ready``
            # reflects that the adapter CAN report real fills; whether
            # this lane is actively placing probes is gated separately
            # by ``BTC_PAPER_LANE_AUTO_SUBMIT``.
            return {
                "execution_state": "READY",
                "order_submission_ready": True,
                "fill_lifecycle_ready": True,
                "execution_capability": "broker_paper_lifecycle",
                "execution_blocker": "",
                "symbol_contract_id": None,
            }
        if spec.broker == "ibkr":
            config = IbkrClientPortalConfig.from_env()
            missing = config.missing_requirements()
            if missing:
                return {
                    "execution_state": "BLOCKED",
                    "order_submission_ready": False,
                    "fill_lifecycle_ready": False,
                    "execution_capability": "missing_broker_paper_config",
                    "execution_blocker": "; ".join(missing),
                    "symbol_contract_id": None,
                }
            conid = config.conid_for(spec.symbol)
            if conid is None:
                return {
                    "execution_state": "BLOCKED",
                    "order_submission_ready": False,
                    "fill_lifecycle_ready": False,
                    "execution_capability": "missing_symbol_contract",
                    "execution_blocker": f"missing IBKR conid for {spec.symbol}",
                    "symbol_contract_id": None,
                }
            # v0.1.58: BTCUSD/ETHUSD conids are baked in and PAXOS is
            # the resolved listing exchange. Full order lifecycle routes
            # through the Client Portal REST API when the gateway is
            # running.
            return {
                "execution_state": "READY",
                "order_submission_ready": True,
                "fill_lifecycle_ready": True,
                "execution_capability": "broker_paper_lifecycle",
                "execution_blocker": "",
                "symbol_contract_id": conid,
                "listing_exchange": config.exchange_for(spec.symbol),
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "execution_state": "BLOCKED",
            "order_submission_ready": False,
            "fill_lifecycle_ready": False,
            "execution_capability": "config_error",
            "execution_blocker": f"{type(exc).__name__}: {exc}",
            "symbol_contract_id": None,
        }
    return {
        "execution_state": "BLOCKED",
        "order_submission_ready": False,
        "fill_lifecycle_ready": False,
        "execution_capability": "unsupported_broker",
        "execution_blocker": f"unsupported broker {spec.broker}",
        "symbol_contract_id": None,
    }


def build_worker_payload(
    spec: FleetWorkerSpec,
    *,
    pid: int,
    status: str,
    started_at_utc: str,
    heartbeat_count: int,
    broker_ready: bool,
    latest_trade: dict[str, Any] | None = None,
    note: str = "",
    execution_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a secret-free worker heartbeat."""

    execution = execution_profile or build_execution_profile(spec)
    return {
        "worker_id": spec.worker_id,
        "broker": spec.broker,
        "lane": spec.lane,
        "symbol": spec.symbol,
        "pid": pid,
        "status": status,
        "mode": "BTC_BROKER_PAPER",
        "paper_runtime": True,
        "paper_starting_cash": round(float(spec.paper_starting_cash), 2),
        "paper_cash": round(float(spec.paper_starting_cash), 2),
        "paper_equity": round(float(spec.paper_starting_cash), 2),
        "order_routing": PAPER_BROKER_ORDER_ROUTING,
        "live_money_orders": "blocked",
        "paper_broker_orders": "allowed",
        "broker_ready": broker_ready,
        "execution_state": execution.get("execution_state"),
        "order_submission_ready": bool(execution.get("order_submission_ready")),
        "fill_lifecycle_ready": bool(execution.get("fill_lifecycle_ready")),
        "execution_capability": execution.get("execution_capability"),
        "execution_blocker": execution.get("execution_blocker"),
        "symbol_contract_id": execution.get("symbol_contract_id"),
        "position_state": build_position_state(spec, latest_trade),
        "last_order": build_last_order(latest_trade),
        "started_at_utc": started_at_utc,
        "last_heartbeat_utc": utc_now(),
        "heartbeat_count": heartbeat_count,
        "safety": "paper broker orders allowed; live-money orders blocked",
        "note": note,
    }


def build_fleet_status(
    *,
    workers: list[dict[str, Any]],
    broker_preflight: dict[str, Any],
    starting_cash: float,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> dict[str, Any]:
    running = [worker for worker in workers if worker.get("status") == "RUNNING"]
    in_trade_workers = [worker for worker in workers if worker.get("position_state", {}).get("in_trade")]
    blocked_execution_workers = [
        worker for worker in workers if str(worker.get("execution_state") or "").upper() == "BLOCKED"
    ]
    order_submission_ready_workers = [worker for worker in workers if bool(worker.get("order_submission_ready"))]
    fill_lifecycle_ready_workers = [worker for worker in workers if bool(worker.get("fill_lifecycle_ready"))]
    return {
        "generated_at_utc": utc_now(),
        "fleet": "btc_broker_paper_fleet",
        "requested_workers": 4,
        "running_workers": len(running),
        "in_trade_workers": len(in_trade_workers),
        "paper_starting_cash_per_worker": round(float(starting_cash), 2),
        "paper_starting_cash_total": round(float(starting_cash) * 4, 2),
        "order_routing": PAPER_BROKER_ORDER_ROUTING,
        "live_money_orders": "blocked",
        "paper_broker_orders": "allowed",
        "paper_runtime": True,
        "trade_visibility": {
            "in_trade_workers": len(in_trade_workers),
            "flat_workers": max(0, len(workers) - len(in_trade_workers)),
            "ledger_path": str(worker_ledger_path(out_dir)),
        },
        "execution_visibility": {
            "order_submission_ready_workers": len(order_submission_ready_workers),
            "fill_lifecycle_ready_workers": len(fill_lifecycle_ready_workers),
            "blocked_execution_workers": len(blocked_execution_workers),
            "limited_execution_workers": max(
                0,
                len(workers) - len(blocked_execution_workers),
            ),
            "operator_note": (
                "BTC broker-paper lanes now run PaperLaneRunner ticks each "
                "heartbeat. Real order placement is gated behind "
                "BTC_PAPER_LANE_AUTO_SUBMIT=1; without the opt-in lanes "
                "reconcile previously-submitted orders only."
            ),
        },
        "broker_preflight": broker_preflight,
        "workers": workers,
        "safety": "Tastytrade/IBKR paper routing is allowed; live-money order placement is blocked.",
    }


def _execute_worker_tick(
    spec: FleetWorkerSpec,
    *,
    runner: PaperLaneRunner | None,
    out_dir: Path,
    heartbeat: int,
    started_at: str,
    runner_error: str,
) -> dict[str, Any]:
    """Execute one worker iteration and return the persisted heartbeat payload.

    Pure function -- no time.sleep, no signal handling. Isolated so the
    E2E test can drive the lifecycle without spawning a subprocess.
    """
    lane_snapshot: dict[str, Any] = {}
    if runner is not None:
        try:
            lane_snapshot = asyncio.run(run_one_tick(runner))
        except Exception as exc:  # noqa: BLE001
            lane_snapshot = {
                "execution_state": "ERROR",
                "last_event": f"tick_error:{type(exc).__name__}",
                "error": str(exc),
            }
    payload = build_worker_payload(
        spec,
        pid=os.getpid(),
        status="RUNNING",
        started_at_utc=started_at,
        heartbeat_count=heartbeat,
        broker_ready=True,
        latest_trade=latest_trade_for_worker(out_dir, spec.worker_id),
        note=runner_error or "paper-lane tick completed",
    )
    if lane_snapshot:
        payload["lane_runner"] = lane_snapshot
        if lane_snapshot.get("execution_state"):
            payload["execution_state"] = lane_snapshot["execution_state"]
        if lane_snapshot.get("active_order_id"):
            payload["fill_lifecycle_ready"] = True
    write_json(worker_status_path(out_dir, spec.worker_id), payload)
    return payload


def _build_lane_runner(
    spec: FleetWorkerSpec,
    *,
    out_dir: Path,
) -> tuple[PaperLaneRunner | None, str]:
    """Construct the lane runner or capture the construction error."""
    try:
        runner = PaperLaneRunner(
            worker_id=spec.worker_id,
            broker=spec.broker,
            lane=spec.lane,
            symbol=spec.symbol,
            state_dir=out_dir,
            ledger_path=worker_ledger_path(out_dir),
        )
    except Exception as exc:  # noqa: BLE001 -- caller falls back to heartbeat-only
        return None, f"{type(exc).__name__}: {exc}"
    return runner, ""


def run_worker(spec: FleetWorkerSpec, *, out_dir: Path, heartbeat_interval_s: float) -> int:
    """Run one BTC broker-paper worker with real lane execution.

    Replaces the pre-v0.1.58 heartbeat-only loop. Each tick now:
      1. Builds (or reuses) a :class:`PaperLaneRunner` that wraps the
         broker adapter for this lane's symbol.
      2. Calls ``runner.tick()`` which either submits a probe order
         (only if ``BTC_PAPER_LANE_AUTO_SUBMIT=1``) or reconciles the
         existing probe.
      3. Emits a heartbeat JSON payload including the lane snapshot.

    On KeyboardInterrupt the worker cancels its active probe (best
    effort) and writes a final STOPPED heartbeat.
    """
    started_at = utc_now()
    heartbeat = 0
    runner, runner_error = _build_lane_runner(spec, out_dir=out_dir)
    try:
        while True:
            heartbeat += 1
            _execute_worker_tick(
                spec,
                runner=runner,
                out_dir=out_dir,
                heartbeat=heartbeat,
                started_at=started_at,
                runner_error=runner_error,
            )
            time.sleep(max(0.2, heartbeat_interval_s))
    except KeyboardInterrupt:
        if runner is not None:
            with contextlib.suppress(Exception):
                asyncio.run(shutdown_lane(runner))
        payload = build_worker_payload(
            spec,
            pid=os.getpid(),
            status="STOPPED",
            started_at_utc=started_at,
            heartbeat_count=heartbeat,
            broker_ready=True,
            latest_trade=latest_trade_for_worker(out_dir, spec.worker_id),
            note="worker interrupted; active probe cancelled",
        )
        if runner is not None:
            payload["lane_runner"] = runner.snapshot()
        write_json(worker_status_path(out_dir, spec.worker_id), payload)
        return 0


def start_worker_process(spec: FleetWorkerSpec, *, out_dir: Path, heartbeat_interval_s: float) -> subprocess.Popen[Any]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--worker-id",
        spec.worker_id,
        "--broker",
        spec.broker,
        "--lane",
        spec.lane,
        "--symbol",
        spec.symbol,
        "--starting-cash",
        str(spec.paper_starting_cash),
        "--out-dir",
        str(out_dir),
        "--heartbeat-interval-s",
        str(heartbeat_interval_s),
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


def collect_status(out_dir: Path, specs: list[FleetWorkerSpec]) -> list[dict[str, Any]]:
    workers: list[dict[str, Any]] = []
    for spec in specs:
        payload = read_json(worker_status_path(out_dir, spec.worker_id))
        latest_trade = latest_trade_for_worker(out_dir, spec.worker_id)
        pid = int(payload.get("pid") or 0)
        if payload:
            payload["process_running"] = is_pid_running(pid)
            if payload.get("status") == "RUNNING" and not payload["process_running"]:
                payload["status"] = "STALE"
            if latest_trade or "position_state" not in payload:
                payload["position_state"] = build_position_state(spec, latest_trade)
            if latest_trade or "last_order" not in payload:
                payload["last_order"] = build_last_order(latest_trade)
            workers.append(payload)
        else:
            workers.append(
                build_worker_payload(
                    spec,
                    pid=0,
                    status="MISSING",
                    started_at_utc="",
                    heartbeat_count=0,
                    broker_ready=False,
                    latest_trade=latest_trade,
                    note="no heartbeat artifact yet",
                ),
            )
    return workers


async def start_fleet(*, out_dir: Path, starting_cash: float, heartbeat_interval_s: float) -> dict[str, Any]:
    specs = fleet_workers(starting_cash)
    broker_preflight = await probe_required_brokers(["tastytrade", "ibkr"])
    if broker_preflight["missing_ready"]:
        workers = [
            build_worker_payload(
                spec,
                pid=0,
                status="BLOCKED",
                started_at_utc="",
                heartbeat_count=0,
                broker_ready=False,
                latest_trade=latest_trade_for_worker(out_dir, spec.worker_id),
                note="broker preflight did not report READY",
            )
            for spec in specs
        ]
        status = build_fleet_status(
            workers=workers,
            broker_preflight=broker_preflight,
            starting_cash=starting_cash,
            out_dir=out_dir,
        )
        write_json(out_dir / FLEET_MANIFEST, status)
        return status

    existing = {worker.get("worker_id"): worker for worker in collect_status(out_dir, specs)}
    launched: list[dict[str, Any]] = []
    for spec in specs:
        current = existing.get(spec.worker_id, {})
        pid = int(current.get("pid") or 0)
        if current.get("status") == "RUNNING" and is_pid_running(pid):
            launched.append(current)
            continue
        process = start_worker_process(spec, out_dir=out_dir, heartbeat_interval_s=heartbeat_interval_s)
        launched.append(
            build_worker_payload(
                spec,
                pid=process.pid,
                status="STARTING",
                started_at_utc=utc_now(),
                heartbeat_count=0,
                broker_ready=True,
                latest_trade=latest_trade_for_worker(out_dir, spec.worker_id),
                note="worker process launched; heartbeat pending",
            ),
        )
    deadline = time.monotonic() + max(15.0, heartbeat_interval_s * 4)
    workers = collect_status(out_dir, specs)
    while time.monotonic() < deadline:
        if all(worker.get("status") == "RUNNING" for worker in workers):
            break
        time.sleep(min(1.0, max(0.2, heartbeat_interval_s)))
        workers = collect_status(out_dir, specs)
    status = build_fleet_status(
        workers=workers,
        broker_preflight=broker_preflight,
        starting_cash=starting_cash,
        out_dir=out_dir,
    )
    write_json(out_dir / FLEET_MANIFEST, status)
    return status


def status_fleet(*, out_dir: Path, starting_cash: float) -> dict[str, Any]:
    specs = fleet_workers(starting_cash)
    broker_preflight = read_json(out_dir / FLEET_MANIFEST).get("broker_preflight", {})
    workers = collect_status(out_dir, specs)
    status = build_fleet_status(
        workers=workers,
        broker_preflight=broker_preflight,
        starting_cash=starting_cash,
        out_dir=out_dir,
    )
    write_json(out_dir / FLEET_MANIFEST, status)
    return status


def stop_fleet(*, out_dir: Path, starting_cash: float) -> dict[str, Any]:
    specs = fleet_workers(starting_cash)
    stopped: list[dict[str, Any]] = []
    for worker in collect_status(out_dir, specs):
        pid = int(worker.get("pid") or 0)
        if pid > 0 and is_pid_running(pid):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
                else:
                    os.kill(pid, 15)
                worker["status"] = "STOPPED"
                worker["note"] = "stop requested by btc_broker_fleet"
            except OSError as exc:
                worker["status"] = "STOP_FAILED"
                worker["note"] = str(exc)
        stopped.append(worker)
    status = build_fleet_status(
        workers=stopped,
        broker_preflight=read_json(out_dir / FLEET_MANIFEST).get("broker_preflight", {}),
        starting_cash=starting_cash,
        out_dir=out_dir,
    )
    write_json(out_dir / FLEET_MANIFEST, status)
    return status


def format_summary(payload: dict[str, Any]) -> str:
    lines = [
        "BTC broker-paper fleet",
        "=" * 72,
        f"running_workers: {payload.get('running_workers')}/{payload.get('requested_workers')}",
        f"starting_cash:   ${float(payload.get('paper_starting_cash_per_worker', 0.0)):,.2f} per worker",
        f"order_routing:   {payload.get('order_routing')}",
        "-" * 72,
    ]
    for worker in payload.get("workers", []):
        position = worker.get("position_state", {})
        trade_label = "IN_TRADE" if position.get("in_trade") else "FLAT"
        lines.append(
            f"{worker.get('worker_id', '?'):<28} {worker.get('status', '?'):<8} "
            f"pid={worker.get('pid', 0)} broker={worker.get('broker', '?'):<11} "
            f"lane={worker.get('lane', '?'):<11} cash=${float(worker.get('paper_cash', 0.0)):,.2f} "
            f"position={trade_label} exec={worker.get('execution_state', '?')}"
        )
    lines.append("=" * 72)
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the four-worker BTC broker-paper fleet")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true", help="Start or reuse the four worker processes")
    action.add_argument("--status", action="store_true", help="Refresh and print fleet status")
    action.add_argument("--stop", action="store_true", help="Stop known worker PIDs from the fleet manifest")
    action.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--starting-cash", type=float, default=DEFAULT_STARTING_CASH)
    parser.add_argument("--heartbeat-interval-s", type=float, default=DEFAULT_HEARTBEAT_INTERVAL_S)
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--broker", default="")
    parser.add_argument("--lane", default="")
    parser.add_argument("--symbol", default="BTCUSD")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.worker:
        spec = FleetWorkerSpec(
            args.worker_id,
            args.broker,
            args.lane,
            args.symbol,
            args.starting_cash,
        )
        return run_worker(spec, out_dir=args.out_dir, heartbeat_interval_s=args.heartbeat_interval_s)
    if args.start:
        payload = asyncio.run(
            start_fleet(
                out_dir=args.out_dir,
                starting_cash=args.starting_cash,
                heartbeat_interval_s=args.heartbeat_interval_s,
            ),
        )
    elif args.stop:
        payload = stop_fleet(out_dir=args.out_dir, starting_cash=args.starting_cash)
    else:
        payload = status_fleet(out_dir=args.out_dir, starting_cash=args.starting_cash)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_summary(payload))
        print(f"manifest -> {args.out_dir / FLEET_MANIFEST}")
    return 0 if not payload.get("broker_preflight", {}).get("missing_ready") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
