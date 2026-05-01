from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HEALTH_FILE = ROOT / "docs" / "jarvis_live_health.json"
DEFAULT_STATE_FILE = ROOT / "state" / "jarvis_intel" / "hermes_state.json"

_LAST_UPDATE_ID = 0


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def _api_url(method: str) -> str:
    tok = _bot_token()
    return f"https://api.telegram.org/bot{tok}/{method}"


async def send_message(text: str, parse_mode: str = "Markdown") -> bool:
    chat_id = _chat_id()
    tok = _bot_token()
    if not tok or not chat_id:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": int(chat_id), "text": text, "parse_mode": parse_mode},
            )
            return r.status_code == 200
    except Exception as exc:
        logger.warning("hermes: send failed: %s", exc)
        return False


async def send_alert(title: str, message: str, level: str = "INFO") -> bool:
    icon = {"INFO": "\u2139\ufe0f", "WARN": "\u26a0\ufe0f", "ERROR": "\u274c", "CRITICAL": "\ud83d\udd34", "KILL": "\ud83d\udc80"}
    prefix = icon.get(level, "\u2139\ufe0f")
    text = f"{prefix} *{title}*\n{message}"
    return await send_message(text)


async def send_verdict(verdict_dict: dict) -> bool:
    fv = verdict_dict.get("final_verdict", "?")
    subsys = verdict_dict.get("subsystem", "?")
    action = verdict_dict.get("action", "?")
    conf = verdict_dict.get("confidence", 0)
    narrative = verdict_dict.get("narrative_terse", "")
    icon = {"APPROVED": "\u2705", "CONDITIONAL": "\ud83d\udfe0", "DENIED": "\ud83d\uded1", "DEFERRED": "\u23f3"}
    prefix = icon.get(fv, "\u2753")
    text = f"{prefix} *{fv}* | {subsys} {action}\nConfidence: {conf:.0%}"
    if narrative:
        text += f"\n_{narrative}_"
    return await send_message(text)


async def poll_commands() -> list[dict]:
    global _LAST_UPDATE_ID
    tok = _bot_token()
    if not tok:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"https://api.telegram.org/bot{tok}/getUpdates",
                params={"offset": _LAST_UPDATE_ID + 1, "timeout": 5},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if not data.get("ok"):
                return []
            updates = data.get("result", [])
            commands = []
            for upd in updates:
                _LAST_UPDATE_ID = max(_LAST_UPDATE_ID, upd.get("update_id", 0))
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/") and chat_id == _chat_id():
                    commands.append({
                        "text": text,
                        "chat_id": chat_id,
                        "update_id": upd.get("update_id"),
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
            return commands
    except Exception as exc:
        logger.debug("hermes: poll failed: %s", exc)
        return []


async def process_command(cmd_text: str) -> str | None:
    """Process a Telegram command and return a response text."""
    cmd = cmd_text.lower().split()[0]
    args = cmd_text[len(cmd):].strip()

    try:
        if cmd == "/start":
            return (
                "*Jarvis Online* \ud83e\udd16\n\n"
                "Available commands:\n"
                "/status \u2014 Jarvis + broker health\n"
                "/strategies \u2014 Bot strategy status\n"
                "/mode \u2014 Current runtime mode\n"
                "/quantum \u2014 Quantum optimizer status\n"
                "/kaizen \u2014 Last kaizen cycle\n"
                "/kill \u2014 Emergency stop all"
            )

        elif cmd == "/status":
            return await _cmd_status()

        elif cmd == "/strategies":
            return await _cmd_strategies()

        elif cmd == "/mode":
            return await _cmd_mode()

        elif cmd == "/quantum":
            return await _cmd_quantum()

        elif cmd == "/kaizen":
            return await _cmd_kaizen()

        elif cmd == "/kill":
            return await _cmd_kill(args)

        else:
            return f"Unknown: {cmd}. Try /start"
    except Exception as exc:
        logger.warning("hermes: cmd %s error: %s", cmd, exc)
        return f"Error processing {cmd}: {exc}"


async def _cmd_status() -> str:
    health = {}
    if DEFAULT_HEALTH_FILE.exists():
        try:
            health = json.loads(DEFAULT_HEALTH_FILE.read_text())
        except Exception:
            pass
    h = health.get("health", "UNKNOWN")
    auth = "unknown"
    try:
        import ssl, urllib.request, json as j
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/auth/status", context=ctx, timeout=5) as r:
            auth = j.loads(r.read()).get("authenticated", False)
            auth = "authenticated" if auth else "unauthenticated"
    except Exception:
        auth = "unreachable"
    lines = [
        f"\ud83e\udd16 *Jarvis Status*",
        f"Health: {h}",
        f"IBKR: {auth}",
        f"Ticks: {health.get('metrics', {}).get('tick_count', '?')}",
    ]
    return "\n".join(lines)


async def _cmd_strategies() -> str:
    try:
        sys.path.insert(0, str(ROOT))
        from eta_engine.strategies.per_bot_registry import bots
        bot_list = list(bots())
        if not bot_list:
            return "No strategies registered"
        items = [f"\u2022 {b}" for b in bot_list[:10]]
        return f"*Registered Bots ({len(bot_list)})*\n" + "\n".join(items)
    except Exception as exc:
        return f"Error loading strategies: {exc}"


async def _cmd_mode() -> str:
    mode = os.environ.get("APEX_MODE", "unknown")
    provider = os.environ.get("FIRM_PAPER_LIVE_PROVIDER", "unknown")
    return f"*Runtime Mode*\nMode: {mode}\nProvider: {provider}"


async def _cmd_quantum() -> str:
    try:
        state_dir = ROOT / "state" / "jarvis_intel"
        quantum_files = list(state_dir.glob("quantum*.json"))
        if quantum_files:
            latest = max(quantum_files, key=lambda f: f.stat().st_mtime)
            data = json.loads(latest.read_text())
            return f"*Quantum Optimizer*\nLast run: {data.get('ts', '?')}\nObjective: {data.get('objective', '?')}"
        return "Quantum optimizer: no recent runs"
    except Exception as exc:
        return f"Quantum status: {exc}"


async def _cmd_kaizen() -> str:
    try:
        state_dir = ROOT / "state" / "jarvis_intel"
        kaizen_log = state_dir / "kaizen_log.jsonl"
        if kaizen_log.exists():
            lines = kaizen_log.read_text().strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                return f"*Last Kaizen Cycle*\n{json.dumps(last, indent=2)[:500]}"
        return "No kaizen cycles yet"
    except Exception as exc:
        return f"Kaizen status: {exc}"


async def _cmd_kill(args: str) -> str:
    if args.strip().lower() != "confirm":
        return (
            "\u2622\ufe0f *KILL COMMAND*\n"
            "This halts ALL trading. To confirm:\n"
            "`/kill confirm`"
        )
    try:
        # Write latch files for all codebases
        latch_paths = [
            ROOT / "state",
            ROOT.parent / "var" / "eta_engine" / "state",
            Path("C:\\TheFirm\\apex_predator"),
        ]
        for latch_dir in latch_paths:
            latch_dir.mkdir(parents=True, exist_ok=True)
            (latch_dir / "kill_switch_latch.json").write_text(
                json.dumps({"killed_by": "telegram", "ts": datetime.now(UTC).isoformat()})
            )

        # Stop FirmCore service
        try:
            import subprocess
            subprocess.run(["powershell", "-Command", "Stop-Service FirmCore -Force"],
                         capture_output=True, text=True, timeout=10)
            subprocess.run(["powershell", "-Command", "Stop-Service FirmWatchdog -Force"],
                         capture_output=True, text=True, timeout=10)
        except Exception:
            pass

        # Stop Jarvis daemon
        try:
            import subprocess
            subprocess.run(["schtasks", "/End", "/TN", "JarvisLiveDaemon"],
                         capture_output=True, text=True, timeout=10)
        except Exception:
            pass

        return "\ud83d\udc80 *KILL ENGAGED* \u2014 FirmCore stopped, Jarvis halted, all trading stopped"
    except Exception as exc:
        return f"Kill failed: {exc}"


async def tick_poll() -> list[str]:
    """Poll Telegram for commands and process them. Returns response texts."""
    commands = await poll_commands()
    responses = []
    for cmd in commands:
        resp = await process_command(cmd["text"])
        if resp:
            await send_message(resp)
            responses.append(resp)
    return responses
