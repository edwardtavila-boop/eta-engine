"""
JARVIS v3 // vps
================
JARVIS as VPS admin.

Operator directive: make JARVIS the admin of the VPS (where the trading
bots run). This module gives JARVIS the VOCABULARY (not the execution)
to manage Linux services, processes, disk, memory, and cron:

  * declarative service state (desired vs actual)
  * health probes per service
  * capacity checks (cpu / mem / disk)
  * action shells -- templates for systemctl / journalctl / df / free

Execution is deliberately indirect: JARVIS produces a
``VPSActionRequest`` and hands it to the existing ``JarvisAdmin``
(action type = ``SYSTEM_ADMIN``). Admin gates the request through the
usual policy engine. Actual shell execution happens in the VPS driver
(``scripts/jarvis_vps_driver.py`` -- future work) after approval.

Pure / no os.system. Only the driver runs shells.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ServiceState(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class VPSServiceSpec(BaseModel):
    """Declarative description of a systemd-managed service."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = ""
    desired: ServiceState = ServiceState.RUNNING
    # Path glob to logs (for journalctl-style tail on alerts).
    log_path: str | None = None
    # Optional URL for HTTP health probe.
    health_url: str | None = None


class VPSSnapshot(BaseModel):
    """A moment-in-time system capacity snapshot."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    cpu_pct: float = Field(ge=0.0, le=100.0)
    mem_pct: float = Field(ge=0.0, le=100.0)
    disk_pct: float = Field(ge=0.0, le=100.0)
    load_1m: float = Field(ge=0.0)
    load_5m: float = Field(ge=0.0)
    load_15m: float = Field(ge=0.0)
    network_up_kbps: float = Field(ge=0.0, default=0.0)
    network_dn_kbps: float = Field(ge=0.0, default=0.0)


class VPSActionType(StrEnum):
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"
    RELOAD = "RELOAD"
    TAIL_LOG = "TAIL_LOG"
    DISK_PRUNE = "DISK_PRUNE"
    KILL_PID = "KILL_PID"


class VPSActionRequest(BaseModel):
    """A proposed VPS action. This object is handed to JarvisAdmin."""

    model_config = ConfigDict(frozen=True)

    action: VPSActionType
    service: str | None = None
    pid: int | None = None
    extra: dict[str, str] = Field(default_factory=dict)
    rationale: str = Field(min_length=1)
    urgency: str = Field(pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$", default="MEDIUM")


class VPSHealthReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    services: dict[str, str]  # service_name -> state
    snapshot: VPSSnapshot
    alerts: list[str] = Field(default_factory=list)
    overall: str = Field(pattern="^(GREEN|YELLOW|RED)$")
    proposed_actions: list[VPSActionRequest] = Field(default_factory=list)


# Capacity thresholds
CPU_WARN = 75.0
CPU_CRIT = 90.0
MEM_WARN = 80.0
MEM_CRIT = 95.0
DISK_WARN = 85.0
DISK_CRIT = 95.0


def assess_vps(
    snapshot: VPSSnapshot,
    services: dict[str, ServiceState],
    specs: dict[str, VPSServiceSpec],
    now: datetime | None = None,
) -> VPSHealthReport:
    """Evaluate VPS health from a snapshot + service states.

    Outputs a report + a list of *proposed* actions (not executed).
    """
    now = now or datetime.now(UTC)
    alerts: list[str] = []
    proposed: list[VPSActionRequest] = []

    # Service divergence from desired state
    service_view: dict[str, str] = {}
    for name, actual in services.items():
        service_view[name] = actual.value
        spec = specs.get(name)
        if spec is None:
            continue
        if actual != spec.desired:
            alerts.append(
                f"service {name} is {actual.value}, expected {spec.desired.value}",
            )
            # Proposed action -- RESTART if failed, START if stopped-but-wanted
            if actual == ServiceState.FAILED:
                proposed.append(
                    VPSActionRequest(
                        action=VPSActionType.RESTART,
                        service=name,
                        rationale=f"{name} in FAILED state; attempt restart",
                        urgency="HIGH",
                    )
                )
            elif actual == ServiceState.STOPPED and spec.desired == ServiceState.RUNNING:
                proposed.append(
                    VPSActionRequest(
                        action=VPSActionType.START,
                        service=name,
                        rationale=f"{name} stopped but should be running",
                        urgency="HIGH",
                    )
                )

    # Capacity alerts
    if snapshot.cpu_pct >= CPU_CRIT:
        alerts.append(f"CPU {snapshot.cpu_pct:.0f}% >= critical {CPU_CRIT:.0f}%")
    elif snapshot.cpu_pct >= CPU_WARN:
        alerts.append(f"CPU {snapshot.cpu_pct:.0f}% >= warn {CPU_WARN:.0f}%")
    if snapshot.mem_pct >= MEM_CRIT:
        alerts.append(f"MEM {snapshot.mem_pct:.0f}% >= critical {MEM_CRIT:.0f}%")
    elif snapshot.mem_pct >= MEM_WARN:
        alerts.append(f"MEM {snapshot.mem_pct:.0f}% >= warn {MEM_WARN:.0f}%")
    if snapshot.disk_pct >= DISK_CRIT:
        alerts.append(f"DISK {snapshot.disk_pct:.0f}% >= critical {DISK_CRIT:.0f}%")
        proposed.append(
            VPSActionRequest(
                action=VPSActionType.DISK_PRUNE,
                rationale=f"disk {snapshot.disk_pct:.0f}% -- prune old logs/caches",
                urgency="CRITICAL",
            )
        )
    elif snapshot.disk_pct >= DISK_WARN:
        alerts.append(f"DISK {snapshot.disk_pct:.0f}% >= warn {DISK_WARN:.0f}%")

    # Classify overall health
    if (
        snapshot.cpu_pct >= CPU_CRIT
        or snapshot.mem_pct >= MEM_CRIT
        or snapshot.disk_pct >= DISK_CRIT
        or any(s == ServiceState.FAILED for s in services.values())
    ):
        overall = "RED"
    elif alerts:
        overall = "YELLOW"
    else:
        overall = "GREEN"

    return VPSHealthReport(
        ts=now,
        services=service_view,
        snapshot=snapshot,
        alerts=alerts,
        overall=overall,
        proposed_actions=proposed,
    )


# ---------------------------------------------------------------------------
# Canonical service catalog for the Evolutionary Trading Algo / MNQ / Firm stack
# ---------------------------------------------------------------------------

DEFAULT_CATALOG: dict[str, VPSServiceSpec] = {
    "mnq-bot.service": VPSServiceSpec(
        name="mnq-bot.service",
        description="MNQ futures trading bot (eta_engine framework)",
        desired=ServiceState.RUNNING,
        log_path="/var/log/mnq-bot/*.log",
    ),
    "jarvis-live.service": VPSServiceSpec(
        name="jarvis-live.service",
        description="JARVIS ContextEngine live supervisor loop",
        desired=ServiceState.RUNNING,
        log_path="/var/log/jarvis/live.log",
    ),
    "firm-agents.service": VPSServiceSpec(
        name="firm-agents.service",
        description="6-agent adversarial firm (Quant/RedTeam/Risk/Macro/Micro/PM)",
        desired=ServiceState.RUNNING,
    ),
    "tradovate-md.service": VPSServiceSpec(
        name="tradovate-md.service",
        description=("Tradovate market-data websocket self-capture [DORMANT 2026-04-24 -- broker funding-blocked]"),
        desired=ServiceState.STOPPED,
    ),
    "trading-dashboard.service": VPSServiceSpec(
        name="trading-dashboard.service",
        description="React trading-dashboard backend (FastAPI)",
        desired=ServiceState.RUNNING,
        health_url="http://127.0.0.1:8000/health",
    ),
}


def vps_action_to_shell(req: VPSActionRequest) -> list[str]:
    """Return the argv template for an approved VPS action.

    The driver process runs this via ``subprocess.run`` AFTER JarvisAdmin
    approves. We keep the translation here so it's auditable.
    """
    if req.action == VPSActionType.START and req.service:
        return ["systemctl", "start", req.service]
    if req.action == VPSActionType.STOP and req.service:
        return ["systemctl", "stop", req.service]
    if req.action == VPSActionType.RESTART and req.service:
        return ["systemctl", "restart", req.service]
    if req.action == VPSActionType.RELOAD and req.service:
        return ["systemctl", "reload", req.service]
    if req.action == VPSActionType.TAIL_LOG and req.service:
        return ["journalctl", "-u", req.service, "-n", "200", "--no-pager"]
    if req.action == VPSActionType.DISK_PRUNE:
        # Conservative: logs older than 14 days.
        return ["journalctl", "--vacuum-time=14d"]
    if req.action == VPSActionType.KILL_PID and req.pid:
        return ["kill", str(req.pid)]
    raise ValueError(f"unsupported action: {req}")
