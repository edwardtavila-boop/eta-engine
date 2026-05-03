from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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
        with contextlib.suppress(Exception):
            health = json.loads(DEFAULT_HEALTH_FILE.read_text())
    h = health.get("health", "UNKNOWN")
    auth = "unknown"
    try:
        import json as j
        import ssl
        import urllib.request
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/auth/status", context=ctx, timeout=5) as r:
            auth = j.loads(r.read()).get("authenticated", False)
            auth = "authenticated" if auth else "unauthenticated"
    except Exception:
        auth = "unreachable"
    lines = [
        "\ud83e\udd16 *Jarvis Status*",
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
            ROOT.parent / "firm_command_center" / "var" / "data" / "runtime_state",
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


# ── Telegram Webhook (FastAPI) ──────────────────────────────────────

def create_webhook_app():
    """Return a FastAPI app with POST /webhook/telegram.

    Register with Telegram::
        curl -sk "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<host>/webhook/telegram"
    """
    from fastapi import FastAPI, Request
    app = FastAPI(title="Hermes")

    @app.post("/webhook/telegram")
    async def telegram_webhook(request: Request):
        body = await request.json()
        msg = body.get("message", {})
        text = msg.get("text", "").strip()
        cid = str(msg.get("chat", {}).get("id", ""))
        if text.startswith("/") and cid == _chat_id():
            resp = await process_command(text)
            if resp:
                await send_message(resp)
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok", "bot": "hermes"}

    return app


async def start_webhook(host: str = "127.0.0.1", port: int = 8842) -> None:
    """Start the webhook server (blocking)."""
    import uvicorn
    app = create_webhook_app()
    cfg = uvicorn.Config(app, host=host, port=port, log_level="info")
    srv = uvicorn.Server(cfg)
    with contextlib.suppress(KeyboardInterrupt):
        await srv.serve()


def start_webhook_bg(host: str = "127.0.0.1", port: int = 8842):
    """Start webhook in a daemon thread (for use from scheduled task)."""
    import asyncio
    import threading
    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_webhook(host=host, port=port))
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


async def register_webhook(public_url: str) -> bool:
    """Register webhook URL with Telegram so POSTs come to us."""
    tok = _bot_token()
    if not tok or not public_url:
        return False
    import httpx
    try:
        url = f"{public_url.rstrip('/')}/webhook/telegram"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.telegram.org/bot{tok}/setWebhook",
                          params={"url": url})
            ok = r.json().get("ok", False)
            logger.info("hermes webhook %s: %s", "registered" if ok else "failed", url)
            return ok
    except Exception as exc:
        logger.warning("hermes webhook error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Structured notification classes
# ---------------------------------------------------------------------------


class MessagePriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class JarvisNotification:
    priority: MessagePriority
    title: str
    body: str
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict | None = None


    def as_telegram_html(self) -> str:
        icon = {"low": "", "normal": "", "high": "", "critical": "\u26a0\ufe0f "}
        emoji = icon.get(self.priority.value, "")
        return f"<b>{emoji}{self.title}</b>\n{self.body}"


# ---------------------------------------------------------------------------
# HermesBridge — structured notification bridge with store-and-forward
# ---------------------------------------------------------------------------


_HERMES_BRIDGE: HermesBridge | None = None


class HermesBridge:
    """Structured notification bridge to Telegram with store-and-forward."""

    STORE_AND_FORWARD_PATH: Path | None = None

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enable_telegram: bool = True,
        push_hook: Callable | None = None,
    ) -> None:
        self._bot_token = bot_token or _bot_token()
        self._chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._enable_telegram = enable_telegram and bool(self._bot_token and self._chat_id)
        self._push_hook = push_hook
        self._queue: asyncio.Queue[JarvisNotification] = asyncio.Queue()
        self._sent_count: int = 0
        self._worker_task: asyncio.Task | None = None

    def notify_autonomous_trade(
        self,
        subsystem: str = "bot.mnq",
        action: str = "ORDER_PLACE",
        verdict: str = "APPROVED",
        symbol: str = "",
        size: float = 1.0,
        pnl: float | None = None,
    ) -> None:
        body_lines = [f"Action: {action}", f"Symbol: {symbol}", f"Size: {size*100:.0f}%", f"Verdict: {verdict}"]
        if pnl is not None:
            body_lines.append(f"PnL: {pnl:+.2f}")
        self._enqueue(JarvisNotification(
            priority=MessagePriority.HIGH,
            title=f"Trade: {subsystem}",
            body="\n".join(body_lines),
            metadata={"subsystem": subsystem, "action": action, "symbol": symbol, "size": size, "pnl": pnl},
        ))

    def notify_kaizen_cycle(
        self,
        cycle_id: str,
        proposals_approved: int = 0,
        proposals_rejected: int = 0,
        strategies_promoted: list | None = None,
        strategies_retired: list | None = None,
        quantum_count: int = 0,
        quantum_cost: float = 0.0,
        duration_ms: int = 0,
    ) -> None:
        pro = strategies_promoted or []
        ret = strategies_retired or []
        body = (
            f"Approved: {proposals_approved} / Rejected: {proposals_rejected}\n"
            f"Promoted: {', '.join(pro) if pro else 'none'}\n"
            f"Retired: {', '.join(ret) if ret else 'none'}\n"
            f"Quantum: {quantum_count} ops ${quantum_cost:.4f}\n"
            f"Duration: {duration_ms}ms"
        )
        self._enqueue(JarvisNotification(
            priority=MessagePriority.NORMAL,
            title=f"Kaizen {cycle_id}",
            body=body,
            metadata={"cycle_id": cycle_id, "approved": proposals_approved, "rejected": proposals_rejected,
                       "promoted": pro, "retired": ret},
        ))

    def notify_strategy_lifecycle(
        self, strategy_name: str, instrument: str = "MNQ",
        from_status: str = "paper", to_status: str = "live",
    ) -> None:
        self._enqueue(JarvisNotification(
            priority=MessagePriority.CRITICAL,
            title=f"Strategy {from_status}\u2192{to_status}: {instrument}/{strategy_name}",
            body=f"Lifecycle transition: {from_status} \u2192 {to_status}",
            metadata={"strategy": strategy_name, "instrument": instrument, "from": from_status, "to": to_status},
        ))

    def notify_kill_switch(self, trigger: str = "drawdown", action: str = "flatten_all") -> None:
        self._enqueue(JarvisNotification(
            priority=MessagePriority.CRITICAL,
            title="\u2620\ufe0f KILL SWITCH: " + trigger,
            body=f"Jarvis triggered kill switch. Action: {action}",
            metadata={"trigger": trigger, "action": action},
        ))

    def notify_system_health(self, health_score: float = 1.0, verdict: str = "healthy") -> None:
        pct = int(health_score * 100)
        self._enqueue(JarvisNotification(
            priority=MessagePriority.LOW if health_score >= 0.8 else MessagePriority.HIGH,
            title=f"\U0001f7e2 System Health: {pct}%",
            body=f"Verdict: {verdict}",
            metadata={"health_score": health_score, "verdict": verdict},
        ))

    # -- internal --

    def _enqueue(self, note: JarvisNotification) -> None:
        self._queue.put_nowait(note)
        self._store(note)
        if self._push_hook:
            self._push_hook(note.title, note.body)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._delivery_loop())

    def _store(self, note: JarvisNotification) -> None:
        path = self.STORE_AND_FORWARD_PATH
        if path is None:
            path = _default_saf_path()
            self.STORE_AND_FORWARD_PATH = path
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": note.ts.isoformat(), "priority": note.priority.value,
            "title": note.title, "body": note.body,
            "metadata": note.metadata or {},
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    async def _delivery_loop(self) -> None:
        while True:
            try:
                note = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except TimeoutError:
                break
            await self._deliver(note)
            self._queue.task_done()

    async def _deliver(self, note: JarvisNotification) -> None:
        if not self._enable_telegram:
            return
        try:
            html = note.as_telegram_html()
            ok = await send_message(html, parse_mode="HTML")
            if ok:
                self._sent_count += 1
        except Exception:
            pass

    async def flush_store_and_forward(self) -> int:
        path = self.STORE_AND_FORWARD_PATH
        if not path or not path.exists():
            return 0
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return 0
        sent = 0
        for line in lines:
            try:
                record = json.loads(line)
                note = JarvisNotification(
                    priority=MessagePriority(record["priority"]),
                    title=record["title"], body=record["body"],
                    metadata=record.get("metadata"),
                )
                if await self._deliver(note):
                    sent += 1
                await asyncio.sleep(0.5)
            except Exception:
                continue
        path.write_text("", encoding="utf-8")
        return sent


def _default_saf_path() -> Path:
    return ROOT / "state" / "hermes" / "store_and_forward.jsonl"


def get_bridge() -> HermesBridge:
    global _HERMES_BRIDGE
    if _HERMES_BRIDGE is None:
        _HERMES_BRIDGE = HermesBridge()
    return _HERMES_BRIDGE
