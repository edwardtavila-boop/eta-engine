import json
import time
from datetime import datetime
from urllib.request import Request, urlopen

from eta_engine.scripts import workspace_roots


def _format_live_broker_degraded_display(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if bool(payload.get("ready")) and not payload.get("error"):
        return ""
    degraded_reason = str(
        payload.get("broker_snapshot_source")
        or payload.get("broker_snapshot_state")
        or payload.get("source")
        or ""
    ).strip()
    if not degraded_reason:
        return ""
    display = f"live broker_state now degraded: {degraded_reason}"
    source = str(payload.get("source") or "").strip()
    if source and source != degraded_reason:
        display = f"{display}; via {source}"
    return display

# Check supervisor execution
logs_dir = workspace_roots.ETA_RUNTIME_LOG_DIR
if logs_dir.exists():
    logs = sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    print(f"Logs: {len(logs)}")
    for log in logs[:5]:
        print(f"  {log.name} ({log.stat().st_mtime})")
else:
    print("No log dir")

# Check broker-router inbox for pending orders
pending_dir = workspace_roots.ETA_BROKER_ROUTER_PENDING_DIR
print(f"\nRouter pending dir exists: {pending_dir.exists()}")
pending = list(pending_dir.glob("*.pending_order.json")) if pending_dir.exists() else []
print(f"Pending orders: {len(pending)}")

# Check ibkr bridge log
bridge_log = workspace_roots.ETA_IBKR_BRIDGE_LOG_PATH
print(f"\nBridge log exists: {bridge_log.exists()}")

# Check jarvis health
health = workspace_roots.ETA_JARVIS_LIVE_HEALTH_PATH
if health.exists():
    d = json.loads(health.read_text())
    print(f"\nJARVIS health: {d.get('health', '?')}")
    print(f"  Reasons: {d.get('reasons', [])}")

# Check canonical ETA readiness snapshot
eta_readiness = workspace_roots.ROOT_VAR_DIR / "ops" / "eta_readiness_snapshot_latest.json"
print(f"\nETA readiness snapshot exists: {eta_readiness.exists()}")
if eta_readiness.exists():
    try:
        readiness = json.loads(eta_readiness.read_text(encoding="utf-8"))
        print(f"ETA readiness summary: {readiness.get('summary', '?')}")
        readiness_status = str(readiness.get("status") or "").strip()
        readiness_primary_blocker = str(readiness.get("primary_blocker") or "").strip()
        readiness_detail = str(readiness.get("detail") or "").strip()
        readiness_primary_action = str(readiness.get("primary_action") or "").strip()
        if readiness_status:
            print(f"  Effective status: {readiness_status}")
        if readiness_primary_blocker:
            print(f"  Primary blocker: {readiness_primary_blocker}")
        if readiness_detail:
            print(f"  Detail: {readiness_detail}")
        if readiness_primary_action:
            print(f"  Primary action: {readiness_primary_action}")
        receipt_age_s = None
        checked_at_utc = str(readiness.get("checked_at_utc") or readiness.get("checked_at") or "").strip()
        if checked_at_utc:
            try:
                checked_at_dt = datetime.fromisoformat(checked_at_utc.replace("Z", "+00:00"))
                age_s = max(0, int(time.time() - checked_at_dt.timestamp()))
            except ValueError:
                age_s = None
            receipt_age_s = age_s
            if age_s is not None:
                freshness = "fresh" if age_s <= 300 else "stale"
                print(f"  Receipt freshness: {freshness} ({age_s}s old)")
                if age_s > 300:
                    print(r"  Refresh command: .\scripts\eta-readiness-snapshot.ps1")
        fallback_reason = str(readiness.get("public_fallback_reason") or "").strip()
        brackets_summary = str(readiness.get("brackets_summary") or "").strip()
        brackets_next_action = str(readiness.get("brackets_next_action") or "").strip()
        if fallback_reason:
            print(f"  Public fallback: {fallback_reason}")
        if brackets_summary:
            print(f"  Brackets: {brackets_summary}")
        if brackets_next_action:
            print(f"  Brackets next: {brackets_next_action}")
        fallback_stale_display = str(
            readiness.get("public_fallback_stale_flat_open_order_display") or ""
        ).strip()
        public_live_broker_degraded_display = str(
            readiness.get("public_live_broker_degraded_display") or ""
        ).strip()
        current_public_live_broker_degraded_display = ""
        if fallback_reason:
            try:
                request = Request(
                    "https://ops.evolutionarytradingalgo.com/api/live/broker_state",
                    headers={"Accept": "application/json", "User-Agent": "ETA-Operator/1.0"},
                )
                with urlopen(request, timeout=5.0) as response:
                    current_public_live_broker_degraded_display = _format_live_broker_degraded_display(
                        json.loads(response.read().decode("utf-8"))
                    )
            except Exception:  # noqa: BLE001
                current_public_live_broker_degraded_display = ""
        if fallback_stale_display:
            print(f"  Stale broker orders: {fallback_stale_display}")
        fallback_stale_relation_display = str(
            readiness.get("public_fallback_stale_flat_open_order_relation_display") or ""
        ).strip()
        receipt_live_broker_open_order_count = int(readiness.get("public_live_broker_open_order_count") or 0)
        fallback_broker_order_drift_display = str(
            readiness.get("public_fallback_broker_open_order_drift_display") or ""
        ).strip()
        dashboard_api_runtime_drift_display = str(
            readiness.get("dashboard_api_runtime_drift_display") or ""
        ).strip()
        dashboard_api_runtime_retune_drift_display = str(
            readiness.get("dashboard_api_runtime_retune_drift_display") or ""
        ).strip()
        dashboard_api_runtime_probe_display = str(
            readiness.get("dashboard_api_runtime_probe_display") or ""
        ).strip()
        dashboard_api_runtime_refresh_command = str(
            readiness.get("dashboard_api_runtime_refresh_command") or ""
        ).strip()
        dashboard_api_runtime_refresh_requires_elevation = bool(
            readiness.get("dashboard_api_runtime_refresh_requires_elevation")
        )
        public_live_retune_generated_at_utc = str(
            readiness.get("public_live_retune_generated_at_utc") or ""
        ).strip()
        public_live_retune_sync_drift_display = str(
            readiness.get("public_live_retune_sync_drift_display") or ""
        ).strip()
        current_public_retune_generated_at_utc = ""
        current_public_retune_outcome_line = ""
        current_public_retune_sync_drift_display = ""
        if fallback_reason or (receipt_age_s is not None and receipt_age_s > 300):
            try:
                request = Request(
                    "https://ops.evolutionarytradingalgo.com/api/jarvis/diamond_retune_status",
                    headers={"Accept": "application/json", "User-Agent": "ETA-Operator/1.0"},
                )
                with urlopen(request, timeout=5.0) as response:
                    current_public_retune = json.loads(response.read().decode("utf-8"))
                current_public_retune_generated_at_utc = str(
                    current_public_retune.get("generated_at_utc")
                    or current_public_retune.get("generated_at")
                    or ""
                ).strip()
                current_public_retune_outcome_line = str(
                    current_public_retune.get("focus_active_experiment_outcome_line") or ""
                ).strip()
                if (
                    current_public_retune_generated_at_utc
                    and current_public_retune_generated_at_utc != public_live_retune_generated_at_utc
                ):
                    current_public_retune_sync_drift_display = (
                        "public retune truth now refreshed at "
                        f"{current_public_retune_generated_at_utc} after readiness cached "
                        f"{public_live_retune_generated_at_utc or 'no public retune timestamp'}"
                    )
                elif (
                    current_public_retune_outcome_line
                    and current_public_retune_outcome_line
                    != str(readiness.get("public_live_retune_focus_active_experiment_outcome_line") or "").strip()
                ):
                    cached_public_retune_outcome_line = str(
                        readiness.get("public_live_retune_focus_active_experiment_outcome_line") or ""
                    ).strip()
                    current_public_retune_sync_drift_display = (
                        "public retune outcome now says "
                        f"{current_public_retune_outcome_line} vs readiness cached "
                        f"{cached_public_retune_outcome_line or 'no public retune outcome'}"
                    )
            except Exception:  # noqa: BLE001
                current_public_retune_generated_at_utc = ""
                current_public_retune_outcome_line = ""
                current_public_retune_sync_drift_display = ""
        cached_local_retune_generated_at_utc = str(
            readiness.get("local_retune_generated_at_utc") or ""
        ).strip()
        receipt_current_local_retune_generated_at_utc = str(
            readiness.get("current_local_retune_generated_at_utc") or ""
        ).strip()
        retune_drift_display = str(
            readiness.get("retune_focus_active_experiment_drift_display") or ""
        ).strip()
        local_retune_sync_drift_display = str(
            readiness.get("local_retune_sync_drift_display") or ""
        ).strip()
        current_local_retune_generated_at_utc = receipt_current_local_retune_generated_at_utc
        local_retune_status_path = workspace_roots.ETA_DIAMOND_RETUNE_STATUS_PATH
        if (
            (not local_retune_sync_drift_display or not current_local_retune_generated_at_utc)
            and local_retune_status_path.exists()
        ):
            try:
                local_retune_status = json.loads(local_retune_status_path.read_text(encoding="utf-8"))
                if not current_local_retune_generated_at_utc:
                    current_local_retune_generated_at_utc = str(
                        local_retune_status.get("generated_at_utc")
                        or local_retune_status.get("generated_at")
                        or ""
                    ).strip()
            except Exception:  # noqa: BLE001
                if not receipt_current_local_retune_generated_at_utc:
                    current_local_retune_generated_at_utc = ""
        if (
            not local_retune_sync_drift_display
            and current_local_retune_generated_at_utc
            and cached_local_retune_generated_at_utc
        ):
            try:
                current_local_retune_dt = datetime.fromisoformat(
                    current_local_retune_generated_at_utc.replace("Z", "+00:00")
                )
                cached_local_retune_dt = datetime.fromisoformat(
                    cached_local_retune_generated_at_utc.replace("Z", "+00:00")
                )
            except ValueError:
                current_local_retune_dt = None
                cached_local_retune_dt = None
            if (
                current_local_retune_dt is not None
                and cached_local_retune_dt is not None
                and current_local_retune_dt > cached_local_retune_dt
            ):
                local_retune_sync_drift_display = (
                    "local retune snapshot refreshed at "
                    f"{current_local_retune_generated_at_utc} after readiness cached "
                    f"{cached_local_retune_generated_at_utc}"
                )
            elif current_local_retune_generated_at_utc != cached_local_retune_generated_at_utc:
                local_retune_sync_drift_display = (
                    "local retune snapshot timestamp "
                    f"{current_local_retune_generated_at_utc} differs from readiness cached "
                    f"{cached_local_retune_generated_at_utc}"
                )
        elif (
            not local_retune_sync_drift_display
            and current_local_retune_generated_at_utc
            and not cached_local_retune_generated_at_utc
        ):
            local_retune_sync_drift_display = (
                "local retune snapshot refreshed at "
                f"{current_local_retune_generated_at_utc} but readiness cached no local retune timestamp"
            )
        if public_live_retune_generated_at_utc:
            print(f"  Public retune generated: {public_live_retune_generated_at_utc}")
        if public_live_retune_sync_drift_display:
            print(f"  Public retune sync drift: {public_live_retune_sync_drift_display}")
        if (
            current_public_retune_generated_at_utc
            and current_public_retune_generated_at_utc != public_live_retune_generated_at_utc
        ):
            print(f"  Current public retune generated: {current_public_retune_generated_at_utc}")
        if current_public_retune_outcome_line and (
            current_public_retune_generated_at_utc != public_live_retune_generated_at_utc
            or current_public_retune_sync_drift_display
        ):
            print(f"  Current public retune outcome: {current_public_retune_outcome_line}")
        if current_public_retune_sync_drift_display:
            print(f"  Current public retune sync drift: {current_public_retune_sync_drift_display}")
        if cached_local_retune_generated_at_utc:
            print(f"  Cached local retune generated: {cached_local_retune_generated_at_utc}")
        if (
            current_local_retune_generated_at_utc
            and current_local_retune_generated_at_utc != cached_local_retune_generated_at_utc
        ):
            print(f"  Current local retune generated: {current_local_retune_generated_at_utc}")
        if fallback_stale_relation_display:
            print(f"  Stale-order pressure: {fallback_stale_relation_display}")
        if public_live_broker_degraded_display:
            print(f"  Public broker_state degraded: {public_live_broker_degraded_display}")
        if (
            current_public_live_broker_degraded_display
            and current_public_live_broker_degraded_display != public_live_broker_degraded_display
        ):
            print(
                "  Current live broker_state degraded: "
                f"{current_public_live_broker_degraded_display}"
            )
        if fallback_broker_order_drift_display:
            print(f"  Broker-order drift: {fallback_broker_order_drift_display}")
        try:
            request = Request(
                "http://127.0.0.1:8421/api/master/status",
                headers={"Accept": "application/json"},
            )
            with urlopen(request, timeout=5.0) as response:
                master_status = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            master_status = {}
        current_live_broker_open_order_count = int(
            master_status.get("current_live_broker_open_order_count") or 0
        )
        current_live_broker_open_order_drift_display = str(
            master_status.get("current_live_broker_open_order_drift_display") or ""
        ).strip()
        if dashboard_api_runtime_drift_display:
            print(f"  Dashboard API runtime drift: {dashboard_api_runtime_drift_display}")
        if dashboard_api_runtime_retune_drift_display:
            print(f"  Dashboard API runtime retune drift: {dashboard_api_runtime_retune_drift_display}")
        if dashboard_api_runtime_probe_display:
            print(f"  Dashboard API runtime probe: {dashboard_api_runtime_probe_display}")
        if dashboard_api_runtime_refresh_command:
            print(f"  Dashboard API runtime refresh: {dashboard_api_runtime_refresh_command}")
            if dashboard_api_runtime_refresh_requires_elevation:
                print("  Dashboard API runtime refresh requires elevation: true")
        elif current_live_broker_open_order_drift_display:
            print(f"  Dashboard API runtime drift: {current_live_broker_open_order_drift_display}")
        elif receipt_live_broker_open_order_count > 0 and current_live_broker_open_order_count <= 0:
            print(
                "  Dashboard API runtime drift: "
                "8421 master/status is still blank for current_live_broker_open_order_count "
                f"while readiness receipt has {receipt_live_broker_open_order_count}"
            )
        elif (
            receipt_live_broker_open_order_count > 0
            and current_live_broker_open_order_count > 0
            and current_live_broker_open_order_count != receipt_live_broker_open_order_count
        ):
            print(
                "  Dashboard API runtime drift: "
                f"8421 master/status reports {current_live_broker_open_order_count} current live broker open orders "
                f"while readiness receipt has {receipt_live_broker_open_order_count}"
            )
        if retune_drift_display:
            print(f"  Retune mirror drift: {retune_drift_display}")
        if local_retune_sync_drift_display:
            print(f"  Local retune sync drift: {local_retune_sync_drift_display}")
    except Exception as exc:  # noqa: BLE001
        print(f"ETA readiness snapshot unreadable: {exc}")
