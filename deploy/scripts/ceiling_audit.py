"""ETA basement-to-ceiling audit."""

from __future__ import annotations

import json
import ssl as _ssl
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from .process_diagnostics import (
        collect_windows_processes,
        collect_windows_python_processes,
        duplicate_python_daemons,
        summarize_process_commands,
    )
except ImportError:
    from process_diagnostics import (
        collect_windows_processes,
        collect_windows_python_processes,
        duplicate_python_daemons,
        summarize_process_commands,
    )

ROOT = Path(r"C:\EvolutionaryTradingAlgo")
STATE_ROOT = ROOT / "var" / "eta_engine" / "state"
ENGINE_ROOT = ROOT / "eta_engine"
SSL_CONTEXT = _ssl._create_unverified_context()
PASS_COUNT = 0
WARN_COUNT = 0
FAIL_COUNT = 0


def say(label: str, ok: bool | None = None) -> None:
    global FAIL_COUNT, PASS_COUNT, WARN_COUNT

    if ok is True:
        print(f"  [PASS] {label}")
        PASS_COUNT += 1
    elif ok is False:
        print(f"  [FAIL] {label}")
        FAIL_COUNT += 1
    else:
        print(f"  [WARN] {label}")
        WARN_COUNT += 1


def section(title: str) -> None:
    print("\n" + "=" * 60 + f"\n  {title}\n" + "=" * 60)


def api(path: str) -> object | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def ibkr(path: str) -> object:
    try:
        with urllib.request.urlopen(
            f"https://127.0.0.1:5000/v1/api{path}",
            context=SSL_CONTEXT,
            timeout=10,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def ps(command: str) -> str:
    try:
        return subprocess.check_output(f'powershell -c "{command}"', shell=True, text=True)
    except Exception:
        return ""


def as_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def as_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except ValueError:
        return default


def minutes_since(path: Path) -> float:
    return (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 60


def bot_float(bot: dict[object, object], field: str) -> float:
    value = bot.get(field, 0)
    return float(value) if isinstance(value, int | float) else 0.0


section("1. INFRA")
python_count = as_int(ps("(Get-Process python* -ea 0).Count") or "0")
say(f"Python: {python_count}", python_count >= 4)

java_count = as_int(ps("(Get-Process java* -ea 0).Count") or "0")
say(f"Java(IBKR): {java_count}", java_count >= 1)

for port, name in [(5000, "IBKR"), (8000, "Dashboard")]:
    socket_state = ps(f"netstat -ano|sls ':{port} .*LISTENING'")
    say(f"Port {port}({name})", bool(socket_state.strip()))

free_gb = round(as_float(ps("(Get-PSDrive C).Free/1GB") or "0"), 1)
say(f"Disk:{free_gb}GB", free_gb > 5)

cloudflared_summary = summarize_process_commands(
    collect_windows_processes(ps, "cloudflared.exe"),
    process_name="cloudflared",
    executables=("cloudflared.exe",),
)
if cloudflared_summary.total == 0:
    say("Cloudflared:0", False)
elif cloudflared_summary.extra_instances > 0:
    say(
        (
            f"Cloudflared:{cloudflared_summary.total} "
            f"({cloudflared_summary.extra_instances} duplicate extra instance(s))"
        ),
        False,
    )
elif cloudflared_summary.total > 1:
    say(f"Cloudflared:{cloudflared_summary.total} distinct tunnel command(s)", None)
else:
    say("Cloudflared:1", True)

section("2. DATA")
for state_dir in [STATE_ROOT, ENGINE_ROOT / "state"]:
    if not state_dir.exists():
        continue

    state_files = sorted(state_dir.rglob("*.json"), key=lambda file_path: file_path.stat().st_mtime, reverse=True)
    if state_files:
        age_minutes = minutes_since(state_files[0])
        say(f"State newest:{state_files[0].name} ({age_minutes:.0f}m)", age_minutes < 15)
        break
else:
    say("No state files", False)

verdict_path = ENGINE_ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"
if verdict_path.exists():
    age_minutes = minutes_since(verdict_path)
    say(f"Verdicts log: {age_minutes:.0f}m old", age_minutes < 15)
else:
    say("Verdicts missing", False)

health_path = STATE_ROOT / "jarvis_live_health.json"
if health_path.exists():
    try:
        health_payload = json.loads(health_path.read_text())
        health = health_payload.get("health", "?") if isinstance(health_payload, dict) else "?"
        say(f"JARVIS health: {health}", health in ("GREEN", "YELLOW"))
    except Exception:
        say("JARVIS health parse error", False)
else:
    say("JARVIS health file missing", False)

section("3. SIGNALS & BOTS")
fleet = api("/api/bot-fleet")
fleet_bots: list[dict[object, object]] = []
if isinstance(fleet, dict):
    raw_bots = fleet.get("bots", [])
    if isinstance(raw_bots, list):
        fleet_bots = [bot for bot in raw_bots if isinstance(bot, dict)]

    total_bots = len(fleet_bots)
    active_bots = sum(1 for bot in fleet_bots if bot.get("status") not in ("idle", "readiness_only"))
    running_bots = sum(1 for bot in fleet_bots if bot.get("status") == "running")
    approved = sum(1 for bot in fleet_bots if bot.get("last_jarvis_verdict") == "APPROVED")
    conditional = sum(1 for bot in fleet_bots if bot.get("last_jarvis_verdict") == "CONDITIONAL")
    denied = sum(1 for bot in fleet_bots if bot.get("last_jarvis_verdict") == "DENIED")

    say(f"Bots:{total_bots} Active:{active_bots} Running:{running_bots}", running_bots > 0)
    say(f"APPROVED:{approved} COND:{conditional} DENIED:{denied}", approved + conditional > 0)
    say(f"Today PnL:${sum(bot_float(bot, 'todays_pnl') for bot in fleet_bots):.0f}", True)

    error_bots = [str(bot.get("id", "?")) for bot in fleet_bots if bot.get("status") == "error"]
    readiness_only = [str(bot.get("id", "?")) for bot in fleet_bots if bot.get("status") == "readiness_only"]
    say(f"Error bots:{len(error_bots)}", len(error_bots) == 0)
    if error_bots:
        print(f"         ERRORS: {error_bots}")
    if readiness_only:
        print(f"         INACTIVE(by design): {readiness_only}")
else:
    say("Fleet API unreachable", False)

section("4. GATES & RISK")
risk = api("/api/risk_gates")
if isinstance(risk, dict):
    any_kill = risk.get("any_latched") or risk.get("any_killed")
    say(f"Kill latch: {any_kill}", not any_kill)

kill_latch_path = ENGINE_ROOT / "state" / "safety" / "kill_switch_latch.json"
if kill_latch_path.exists():
    try:
        kill_latch = json.loads(kill_latch_path.read_text())
        flatten_all = kill_latch.get("flatten_all", False) if isinstance(kill_latch, dict) else False
        say(f"Global kill:{flatten_all}", not flatten_all)
    except Exception:
        say("Kill latch bad", False)
else:
    say("No kill latch", True)

section("5. IBKR EXECUTION")
auth = ibkr("/iserver/auth/status")
if isinstance(auth, dict) and "error" not in auth:
    say(f"Auth: auth={auth.get('authenticated')} conn={auth.get('connected')}", bool(auth.get("authenticated")))
else:
    say("IBKR auth fail", False)

account = ibkr("/portfolio/accounts")
if isinstance(account, list) and account and isinstance(account[0], dict):
    say(f"Account:{account[0].get('accountId', '?')}({account[0].get('type', '?')})", True)

positions = ibkr("/portfolio/DUQ319869/positions/0")
if isinstance(positions, list):
    say(f"Positions:{len(positions)}", True)

section("6. LLM / DEEPSEEK")
env_path = ENGINE_ROOT / ".env"
if env_path.exists():
    env_text = env_path.read_text(encoding="utf-8", errors="replace")
    say("DeepSeek key: present", "DEEPSEEK_API_KEY=sk-" in env_text)

avengers_heartbeat = STATE_ROOT / "avengers_heartbeat.json"
if avengers_heartbeat.exists():
    try:
        heartbeat = json.loads(avengers_heartbeat.read_text())
        if isinstance(heartbeat, dict):
            quota_state = heartbeat.get("quota_state", "?")
            hourly_pct = float(heartbeat.get("hourly_pct", 0) or 0)
            daily_pct = float(heartbeat.get("daily_pct", 0) or 0)
            say(f"Quota:{quota_state} (h{hourly_pct:.0%} d{daily_pct:.0%})", quota_state == "NORMAL")
    except Exception:
        say("Quota parse error", False)

section("7. SAFETY & DRAWDOWN")
if fleet_bots:
    max_drawdown = max(bot_float(bot, "max_dd") for bot in fleet_bots)
    say(f"Max DD:${max_drawdown:.0f}", max_drawdown < 500)

section("8. EFFICIENCY")
duplicates = duplicate_python_daemons(collect_windows_python_processes(ps), ["jarvis_live", "avengers_daemon"])
if duplicates:
    say(f"Duplicates:{', '.join(duplicates)}", False)
else:
    say("No duplicate processes", True)

section("9. VERDICT QUALITY")
if verdict_path.exists():
    try:
        with verdict_path.open("rb") as handle:
            recent_lines = handle.readlines()[-200:]

        verdicts: list[dict[object, object]] = []
        for raw_line in recent_lines:
            parsed = json.loads(raw_line)
            if isinstance(parsed, dict):
                verdicts.append(parsed)

        approved_verdicts = [verdict for verdict in verdicts if verdict.get("base_verdict") == "APPROVED"]
        conditional_verdicts = [verdict for verdict in verdicts if verdict.get("base_verdict") == "CONDITIONAL"]
        denied_verdicts = [verdict for verdict in verdicts if verdict.get("base_verdict") == "DENIED"]

        say(
            f"Last 200: {len(approved_verdicts)}APP/{len(conditional_verdicts)}COND/{len(denied_verdicts)}DEN",
            len(denied_verdicts) < len(approved_verdicts) * 2,
        )

        confidences = [
            float(verdict.get("confidence", 0) or 0)
            for verdict in approved_verdicts + conditional_verdicts
        ]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0
        say(f"Avg confidence:{avg_confidence:.2f}", avg_confidence > 0.4)

        subsystems: dict[str, int] = {}
        for verdict in approved_verdicts + conditional_verdicts:
            subsystem = str(verdict.get("subsystem", "?"))
            subsystems[subsystem] = subsystems.get(subsystem, 0) + 1

        if subsystems:
            print("         By asset class:")
            for subsystem, count in sorted(subsystems.items(), key=lambda item: -item[1]):
                print(f"           {subsystem:<20s} {count:>3d}")
    except Exception as exc:
        say(f"Verdict analysis err:{exc}", False)

section("10. DASHBOARD HEALTH")
health = api("/health")
if isinstance(health, dict):
    say(f"API status:{health.get('status', '?')}", health.get("status") == "ok")
else:
    say("Health endpoint fail", False)

print("\n" + "=" * 60 + f"\n  PASS={PASS_COUNT}  WARN={WARN_COUNT}  FAIL={FAIL_COUNT}\n" + "=" * 60)
if FAIL_COUNT == 0 and WARN_COUNT <= 3:
    print("  VERDICT: SYSTEM HEALTHY")
elif FAIL_COUNT <= 2:
    print("  VERDICT: MINOR ISSUES")
else:
    print("  VERDICT: ATTENTION REQUIRED")
