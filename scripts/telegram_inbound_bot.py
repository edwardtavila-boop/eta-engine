"""
Telegram inbound bot — long-poll loop that turns Telegram into a JARVIS terminal.

The outbound channel already pushes PnL, anomalies, celebrations, and
preflight reports to the operator's phone. This module closes the loop:
the operator can TEXT the bot back and get an instant structured answer
without SSH-ing to the VPS.

Architecture
------------

Telegram offers two delivery modes:
  * Webhook  — bot subscribes to a public HTTPS URL (requires reverse proxy)
  * Long-poll — bot calls getUpdates in a loop (no inbound network exposure)

We use long-poll: simpler to deploy on a Windows VPS behind a residential
network, no public TLS surface to maintain, no extra moving parts. The
loop calls ``getUpdates`` with a long-poll timeout of 25 s. Each update
is dispatched to either a slash-command handler or (future) Hermes
free-text routing.

Security
--------

The bot accepts commands ONLY from chat_ids in the allowlist
(``TELEGRAM_CHAT_ID`` env var). Messages from any other chat are
acknowledged with a polite refusal and logged for review. This prevents
random Telegram users who discover the bot username from running
operator commands.

Slash commands (v1)
-------------------

* ``/pnl``         — today / week / month PnL summary
* ``/anomalies``   — recent anomaly hits + suggested skills
* ``/preflight``   — live-cutover Go/No-Go report
* ``/zeus``        — unified brain snapshot
* ``/silence Nm``  — mute the outbound pulse for N minutes
* ``/ack KEY``     — remove one anomaly from the dedup log (reopen for re-fire)
* ``/help``        — list commands

State
-----

The bot persists its last-seen update_id to
``var/telegram_inbound_offset.json`` so a restart doesn't replay old
messages. The silence file ``var/telegram_silence_until.json`` is read
by the outbound pulse to suppress sends.

Failure modes
-------------

* Telegram API down: exponential backoff on the poll loop
* Command dispatch raises: caught + reported back to operator, NOT to log
* Unknown command: replied with "unknown — try /help"
* Non-whitelisted sender: replied with "this bot is private" + logged
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.scripts.telegram_inbound_bot")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_VAR_ROOT = _WORKSPACE / "var"
_OFFSET_PATH = _VAR_ROOT / "telegram_inbound_offset.json"
_SILENCE_PATH = _VAR_ROOT / "telegram_silence_until.json"
_LOG_PATH = _VAR_ROOT / "telegram_inbound.jsonl"

POLL_TIMEOUT_S = 25  # Telegram long-poll
SHORT_BACKOFF_S = 5
LONG_BACKOFF_S = 60
MAX_REPLY_BODY = 3500  # keep below Telegram 4000 ceiling


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _append_audit(record: dict[str, Any]) -> None:
    """Best-effort write to the inbound audit log. Never raises."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        logger.warning("inbound audit log append failed: %s", exc)


def _load_offset() -> int:
    rec = _read_json(_OFFSET_PATH) or {}
    try:
        return int(rec.get("offset", 0))
    except (TypeError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    _write_json(_OFFSET_PATH, {"offset": offset, "asof": _now_iso()})


def _allowed_chat_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    out: set[int] = set()
    if not raw:
        return out
    # support a single id or comma-separated list
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def is_silenced(now: datetime | None = None) -> bool:
    """True if the outbound pulse should suppress sends right now.

    Used by the outbound pulse (``anomaly_telegram_pulse``) to honour
    operator-requested silence windows.
    """
    rec = _read_json(_SILENCE_PATH)
    if not rec:
        return False
    until = rec.get("silence_until")
    if not isinstance(until, str):
        return False
    try:
        until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = now or datetime.now(UTC)
    return until_dt > now


def silence_for(minutes: int) -> str:
    """Persist a silence-until timestamp. Returns the ISO timestamp."""
    until_dt = datetime.now(UTC) + timedelta(minutes=minutes)
    _write_json(
        _SILENCE_PATH,
        {"silence_until": until_dt.isoformat(), "minutes": minutes, "set_at": _now_iso()},
    )
    return until_dt.isoformat()


# ---------------------------------------------------------------------------
# Telegram client (httpx if available, fallback to urllib)
# ---------------------------------------------------------------------------


def _telegram_call(method: str, params: dict[str, Any], timeout_s: float = 30.0) -> dict[str, Any]:
    """POST to Telegram Bot API. Returns the parsed response or {ok:false}."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN unset"}
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        import httpx  # noqa: PLC0415 — optional dep
    except ImportError:
        # Fall back to urllib
        import urllib.error
        import urllib.request

        try:
            data = json.dumps(params).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
            return {"ok": False, "error": str(exc)[:200]}

    try:
        resp = httpx.post(url, json=params, timeout=timeout_s)
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def get_updates(offset: int, timeout_s: int = POLL_TIMEOUT_S) -> list[dict[str, Any]]:
    """Long-poll one batch of updates. Returns [] on failure or no messages."""
    resp = _telegram_call(
        "getUpdates",
        {"offset": offset, "timeout": timeout_s, "allowed_updates": ["message"]},
        timeout_s=timeout_s + 5,
    )
    if not resp.get("ok"):
        logger.warning("getUpdates failed: %s", resp.get("error") or resp.get("description"))
        return []
    return resp.get("result") or []


def send_reply(chat_id: int, text: str, reply_to_message_id: int | None = None) -> dict[str, Any]:
    """Send a Markdown message back. Truncates if oversize."""
    if len(text) > MAX_REPLY_BODY:
        text = text[: MAX_REPLY_BODY - 10] + "\n...(truncated)"
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        params["reply_to_message_id"] = reply_to_message_id
    return _telegram_call("sendMessage", params)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _cmd_help() -> str:
    return (
        "*JARVIS Telegram bot — slash commands*\n"
        "\n"
        "*Daily ops:*\n"
        "`/pnl`           today / 7d / 30d PnL summary\n"
        "`/anomalies`     recent anomaly hits in last 24h\n"
        "`/preflight`     live-cutover Go/No-Go\n"
        "`/zeus`          unified brain snapshot\n"
        "`/debrief`       on-demand end-of-day digest\n"
        "\n"
        "*Prop firm:*\n"
        "`/accounts`      every prop firm account scorecard\n"
        "`/route SIG`     pick best account for a signal\n"
        "`/killall RSN`   emergency kill_all (requires reason)\n"
        "\n"
        "*Bot control:*\n"
        "`/bots`          list active overrides\n"
        "`/pause BOT`     halt one bot (size_modifier=0)\n"
        "`/resume BOT`    clear override for one bot\n"
        "`/size BOT N`    set size_modifier to N (0..1)\n"
        "\n"
        "*Channel:*\n"
        "`/silence Nm`    mute outbound pulse for N minutes\n"
        "`/unsilence`     clear active silence\n"
        "`/ack KEY`       remove one dedup key (reopen for re-fire)\n"
        "`/help`          this list\n"
    )


def _cmd_pnl() -> str:
    try:
        from eta_engine.brain.jarvis_v3 import pnl_summary

        multi = pnl_summary.multi_window_summary()
    except Exception as exc:  # noqa: BLE001
        return f"_pnl unavailable_: `{str(exc)[:100]}`"

    def _fmt(w: dict[str, Any]) -> str:
        r = float(w.get("total_r", 0.0) or 0.0)
        n = int(w.get("n_trades", 0) or 0)
        wins = int(w.get("n_wins", 0) or 0)
        losses = int(w.get("n_losses", 0) or 0)
        wr = float(w.get("win_rate", 0.0) or 0.0) * 100
        return f"{r:+.2f}R   ({n} trades, W/L {wins}/{losses}, {wr:.1f}%)"

    today = multi.get("today") or {}
    week = multi.get("week") or {}
    month = multi.get("month") or {}

    return f"*PnL — multi-window*\n`Today : {_fmt(today)}`\n`7-day : {_fmt(week)}`\n`30-day: {_fmt(month)}`\n"


def _cmd_anomalies() -> str:
    try:
        from eta_engine.brain.jarvis_v3 import anomaly_watcher

        hits = anomaly_watcher.recent_hits(since_hours=24)
    except Exception as exc:  # noqa: BLE001
        return f"_anomaly recent unavailable_: `{str(exc)[:100]}`"

    if not hits:
        return "_Clean — no anomalies in last 24h._"

    lines = [f"*Anomalies — last 24h ({len(hits)} hits)*", ""]
    sev_order = {"critical": 0, "warn": 1, "info": 2}
    for h in sorted(
        hits[-15:],
        key=lambda x: (sev_order.get(str(x.get("severity") or "info"), 9), str(x.get("asof"))),
    ):
        pattern = str(h.get("pattern") or "")
        bot = str(h.get("bot_id") or "?")
        sev = str(h.get("severity") or "info").upper()
        detail = str(h.get("detail") or "")[:80]
        lines.append(f"`[{sev:<5}]` `{pattern}` `{bot}` — {detail}")
    return "\n".join(lines)


def _cmd_preflight() -> str:
    try:
        from eta_engine.brain.jarvis_v3 import preflight

        report = preflight.run_preflight()
    except Exception as exc:  # noqa: BLE001
        return f"_preflight unavailable_: `{str(exc)[:100]}`"

    head = f"*Preflight* — verdict: *{report.verdict}*"
    counts = f"PASS={report.n_pass} WARN={report.n_warn} FAIL={report.n_fail}"
    body_lines = [head, counts, ""]
    # surface only non-PASS lines
    non_pass = [c for c in report.checks if c.status != "PASS"]
    if not non_pass:
        body_lines.append("_All systems green. Push the button._")
    else:
        for c in non_pass:
            body_lines.append(f"`[{c.status}]` `{c.name}` — {c.detail[:80]}")
    return "\n".join(body_lines)


def _cmd_zeus() -> str:
    try:
        from eta_engine.brain.jarvis_v3 import zeus

        snap = zeus.snapshot(force_refresh=False, trace_n=5).to_dict()
    except Exception as exc:  # noqa: BLE001
        return f"_zeus unavailable_: `{str(exc)[:100]}`"

    # Surface the highest-signal subset
    fleet = snap.get("fleet_status") or {}
    regime = snap.get("current_regime") or {}
    overrides = snap.get("active_overrides") or {}

    lines = [
        "*Zeus snapshot*",
        f"`n_bots`:      {fleet.get('n_bots', '?')}",
        f"`top5_elite`:  {[b.get('bot_id') for b in (fleet.get('top5_elite') or [])][:5]}",
    ]
    if regime:
        lines.append(f"`regime`:      {regime.get('label', '?')}")
    if isinstance(overrides, dict):
        n_ov = sum(len(v) if isinstance(v, dict) else 0 for v in overrides.values() if v is not None)
        lines.append(f"`overrides`:   {n_ov} active")
    return "\n".join(lines)


def _cmd_silence(arg: str) -> str:
    """`/silence 30m` or `/silence 2h`."""
    m = re.match(r"^\s*(\d+)\s*([mh]?)\s*$", arg)
    if not m:
        return "_usage: `/silence 30m` or `/silence 2h`_"
    n = int(m.group(1))
    unit = m.group(2) or "m"
    minutes = n * (60 if unit == "h" else 1)
    if minutes <= 0 or minutes > 24 * 60:
        return "_silence window must be 1m..24h_"
    until_iso = silence_for(minutes)
    return f"_Outbound pulse silenced for {minutes} minutes. Until: `{until_iso[:19]}`_"


def _cmd_unsilence() -> str:
    if _SILENCE_PATH.exists():
        try:
            _SILENCE_PATH.unlink()
        except OSError as exc:
            return f"_unsilence failed_: `{exc}`"
    return "_Silence cleared. Outbound pulse will fire normally on next tick._"


def _cmd_accounts() -> str:
    """Show every prop firm account scorecard."""
    try:
        from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

        snaps = g.aggregate_status()
    except Exception as exc:  # noqa: BLE001
        return f"_prop firm guardrails unavailable_: `{str(exc)[:100]}`"

    if not snaps:
        return "_no prop firm accounts registered_"

    lines = ["*Prop firm accounts*", ""]
    sev_emoji = {"blown": "💀", "critical": "🚨", "warn": "⚠️", "ok": "✅"}
    for snap in snaps:
        rules = snap.rules
        state = snap.state
        emoji = sev_emoji.get(snap.severity, "")
        # Always show: account, severity, current_balance, day_pnl, daily_loss_remaining
        bal = state.current_balance
        pnl = state.day_pnl_usd
        dlr = snap.daily_loss_remaining if snap.daily_loss_remaining is not None else 0.0
        ddr = snap.trailing_dd_remaining if snap.trailing_dd_remaining is not None else 0.0
        lines.append(f"{emoji} `{rules.account_id}` [{snap.severity}]")
        lines.append(f"   balance ${bal:,.0f}   day {pnl:+,.0f}   DLR ${dlr:,.0f}   DDR ${ddr:,.0f}")
        if rules.profit_target is not None and snap.pct_to_target is not None:
            tgt_pct = snap.pct_to_target * 100
            lines.append(f"   profit-to-target: {tgt_pct:+.0f}%   target ${rules.profit_target:,.0f}")
        if snap.blockers:
            lines.append(f"   blockers: `{', '.join(snap.blockers)}`")
        if not rules.automation_allowed:
            lines.append("   _automation disallowed by TOS_")
        lines.append("")
    return "\n".join(lines)


def _cmd_debrief() -> str:
    """On-demand end-of-day debrief — returns the formatted body for Telegram.

    Unlike the cron version which sends through send_from_env, this returns
    the body directly so it goes back in the slash-command reply pipeline.
    """
    try:
        from eta_engine.scripts import daily_debrief

        envelope = daily_debrief.build_debrief()
        return envelope["markdown"]
    except Exception as exc:  # noqa: BLE001
        return f"_debrief unavailable_: `{str(exc)[:120]}`"


def _cmd_bots() -> str:
    """List active size_modifier + school_weight overrides."""
    try:
        from eta_engine.brain.jarvis_v3 import hermes_overrides

        summary = hermes_overrides.active_overrides_summary()
    except Exception as exc:  # noqa: BLE001
        return f"_overrides unavailable_: `{str(exc)[:120]}`"

    sm = summary.get("size_modifiers") or {}
    sw = summary.get("school_weights") or {}
    if not sm and not sw:
        return "_no active overrides_"

    lines = ["*Active overrides*"]
    if sm:
        lines.append("")
        lines.append("*Size modifiers (per bot)*")
        for bot_id, info in sorted(sm.items()):
            if isinstance(info, dict):
                mod = info.get("modifier")
                ttl = info.get("ttl_minutes_remaining")
                reason = str(info.get("reason") or "")[:30]
                lines.append(f"`{bot_id:<22}  ×{mod}   ttl={ttl}m`  _{reason}_")
            else:
                lines.append(f"`{bot_id:<22}  ×{info}`")
    if sw:
        lines.append("")
        lines.append("*School weights (per asset)*")
        for key, info in sorted(sw.items()):
            if isinstance(info, dict):
                w = info.get("weight")
                ttl = info.get("ttl_minutes_remaining")
                lines.append(f"`{key:<22}  w={w}   ttl={ttl}m`")
            else:
                lines.append(f"`{key:<22}  {info}`")
    return "\n".join(lines)


def _cmd_pause(arg: str) -> str:
    """`/pause botname` — set size_modifier=0 for one bot."""
    bot_id = arg.strip()
    if not bot_id:
        return "_usage: `/pause <bot_id>`_"
    try:
        from eta_engine.brain.jarvis_v3 import hermes_overrides

        result = hermes_overrides.apply_size_modifier(
            bot_id=bot_id,
            modifier=0.0,
            reason="telegram /pause by operator",
            ttl_minutes=360,  # 6h default — operator can /resume sooner
            source="telegram_inbound_bot",
        )
    except Exception as exc:  # noqa: BLE001
        return f"_pause failed_: `{str(exc)[:120]}`"
    status = result.get("status") if isinstance(result, dict) else "?"
    return f"⏸ *Paused* `{bot_id}` — status: `{status}` (auto-resume in 6h or /resume sooner)"


def _cmd_resume(arg: str) -> str:
    """`/resume botname` — clear size_modifier for one bot."""
    bot_id = arg.strip()
    if not bot_id:
        return "_usage: `/resume <bot_id>`_"
    try:
        from eta_engine.brain.jarvis_v3 import hermes_overrides

        result = hermes_overrides.clear_override(bot_id=bot_id)
    except Exception as exc:  # noqa: BLE001
        return f"_resume failed_: `{str(exc)[:120]}`"
    status = result.get("status") if isinstance(result, dict) else "?"
    return f"▶️ *Resumed* `{bot_id}` — status: `{status}`"


def _cmd_size(arg: str) -> str:
    """`/size botname 0.5` — set size_modifier to a specific value."""
    parts = arg.strip().split()
    if len(parts) != 2:
        return "_usage: `/size <bot_id> <modifier>` e.g. `/size mnq_floor 0.5`_"
    bot_id = parts[0].strip()
    try:
        modifier = float(parts[1])
    except ValueError:
        return "_modifier must be a float, e.g. 0.5_"
    if not 0.0 <= modifier <= 1.0:
        return "_modifier must be in [0.0, 1.0]_"
    try:
        from eta_engine.brain.jarvis_v3 import hermes_overrides

        result = hermes_overrides.apply_size_modifier(
            bot_id=bot_id,
            modifier=modifier,
            reason="telegram /size by operator",
            ttl_minutes=360,
            source="telegram_inbound_bot",
        )
    except Exception as exc:  # noqa: BLE001
        return f"_size failed_: `{str(exc)[:120]}`"
    status = result.get("status") if isinstance(result, dict) else "?"
    return f"📏 *Resize* `{bot_id}` ×{modifier} — status: `{status}` (ttl=6h)"


def _cmd_route(arg: str) -> str:
    """`/route MNQ 1.0 2` — find the best registered account for this signal.

    Parses ``symbol stop_r size`` and runs evaluate() against each
    automation-allowed account, returns the one with most headroom.
    """
    parts = arg.strip().split()
    if len(parts) < 3:
        return "_usage: `/route <SYMBOL> <stop_r> <size>` e.g. `/route MNQ 1.0 2`_"
    symbol = parts[0].upper()
    try:
        stop_r = float(parts[1])
        size = int(parts[2])
    except ValueError:
        return "_stop_r must be float, size must be int_"

    try:
        from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

        signal = {"symbol": symbol, "stop_r": stop_r, "size": size}
        candidates: list[tuple[str, g.GuardrailVerdict, g.AccountSnapshot]] = []
        for account_id in g.list_known_accounts():
            rules = g.get_rules(account_id)
            if rules is None or not rules.automation_allowed:
                continue
            state = g.account_state_from_trades(account_id)
            verdict = g.evaluate(rules, state, signal)
            if not verdict.allowed:
                continue
            snap = g.snapshot_one(account_id)
            if snap is None:
                continue
            candidates.append((account_id, verdict, snap))
    except Exception as exc:  # noqa: BLE001
        return f"_route failed_: `{str(exc)[:120]}`"

    if not candidates:
        return f"_no automation-allowed account passes the guardrail for {symbol} {stop_r}R ×{size}_"

    # Pick the account with the most daily-loss-remaining (most headroom)
    def _headroom(item: tuple) -> float:
        _, _, snap = item
        return snap.daily_loss_remaining or 0.0

    best_id, best_verdict, best_snap = max(candidates, key=_headroom)

    lines = [
        f"*Routing* `{symbol}` `{stop_r}R` ×{size}",
        f"worst-case loss: `${best_verdict.worst_case_loss_usd:,.0f}`",
        "",
        f"🎯 *Best account*: `{best_id}`",
        f"daily-loss remaining: ${best_snap.daily_loss_remaining:,.0f}",
        f"trailing-DD remaining: ${best_snap.trailing_dd_remaining:,.0f}",
    ]
    if len(candidates) > 1:
        lines.append("")
        lines.append(f"_{len(candidates) - 1} other candidates also pass; this has most headroom._")
    return "\n".join(lines)


def _cmd_killall(reason: str) -> str:
    """EMERGENCY: engage kill_all in hermes_state.json. Requires reason."""
    reason = reason.strip()
    if not reason:
        return "_usage: `/killall <reason>` — reason is required for prop-firm audit_"
    workspace = _WORKSPACE
    state_path = workspace / "var" / "eta_engine" / "state" / "jarvis_intel" / "hermes_state.json"
    payload = {
        "kill_all": True,
        "reason": f"telegram /killall: {reason}",
        "asof": _now_iso(),
        "source": "telegram_inbound_bot",
    }
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError as exc:
        return f"_killall FAILED to write state file_: `{exc}`"
    return (
        f"🚨 *KILL SWITCH ENGAGED* 🚨\n"
        f"reason: `{reason}`\n"
        f"all bots halted. clear hermes_state.kill_all manually to resume."
    )


def _cmd_ack(key: str) -> str:
    """Remove one anomaly key from the dedup log so it can re-fire."""
    key = key.strip()
    if not key:
        return "_usage: `/ack PATTERN:BOT:STREAK` (copy from /anomalies output)_"
    hits_log = _WORKSPACE / "var" / "anomaly_watcher.jsonl"
    if not hits_log.exists():
        return "_no anomaly log to ack against_"
    try:
        with hits_log.open(encoding="utf-8") as fh:
            kept = [line for line in fh if not line.strip() or json.loads(line).get("key") != key]
        with hits_log.open("w", encoding="utf-8") as fh:
            fh.writelines(kept)
    except (OSError, json.JSONDecodeError) as exc:
        return f"_ack failed_: `{exc}`"
    return f"_Cleared dedup key `{key}` — will re-fire on next scan._"


# Public registry for tests
COMMANDS: dict[str, Any] = {
    "/help": (_cmd_help, False),
    "/pnl": (_cmd_pnl, False),
    "/anomalies": (_cmd_anomalies, False),
    "/preflight": (_cmd_preflight, False),
    "/zeus": (_cmd_zeus, False),
    "/debrief": (_cmd_debrief, False),
    "/accounts": (_cmd_accounts, False),
    "/route": (_cmd_route, True),
    "/killall": (_cmd_killall, True),
    "/bots": (_cmd_bots, False),
    "/pause": (_cmd_pause, True),
    "/resume": (_cmd_resume, True),
    "/size": (_cmd_size, True),
    "/silence": (_cmd_silence, True),
    "/unsilence": (_cmd_unsilence, False),
    "/ack": (_cmd_ack, True),
}


def dispatch_command(text: str) -> str:
    """Parse the leading slash-command and return the reply text.

    Returns "unknown command" reply for un-recognized inputs.
    """
    text = text.strip()
    if not text:
        return "_empty message_"
    try:
        parts = shlex.split(text)
    except ValueError:
        # Unmatched quotes etc. — fall back to simple split
        parts = text.split()
    if not parts:
        return "_empty message_"
    cmd = parts[0].lower()
    # Telegram appends @botname to commands in groups: strip that
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    if cmd not in COMMANDS:
        return f"_unknown command_: `{cmd}` — try `/help`"
    handler, takes_arg = COMMANDS[cmd]
    arg = " ".join(parts[1:]) if takes_arg else ""
    try:
        if takes_arg:
            return handler(arg)
        return handler()
    except Exception as exc:  # noqa: BLE001
        logger.exception("command %s crashed", cmd)
        return f"_`{cmd}` crashed_: `{str(exc)[:120]}`"


# ---------------------------------------------------------------------------
# Update processing
# ---------------------------------------------------------------------------


_HERMES_EXE = r"C:\Users\Administrator\.hermes\hermes-agent\.venv\Scripts\hermes.exe"
_HERMES_TIMEOUT_S = 90
# Hermes session name used for Telegram chats. ``hermes chat --continue
# <name>`` resumes the same conversation history so multi-turn dialog
# stays coherent across messages. One name per chat_id keeps the
# threads isolated — even though the bot only ever talks to one
# operator today, a future allowlist expansion gets clean separation
# for free.
_HERMES_SESSION_PREFIX = "telegram"
# How long a Telegram session stays "warm" before we start a fresh one.
# Resets the conversation on big gaps so Hermes doesn't try to thread
# yesterday's questions with tonight's, which usually hurts more than helps.
_HERMES_SESSION_TTL_S = 6 * 60 * 60  # 6 hours
_HERMES_LAST_CHAT_PATH = _VAR_ROOT / "telegram_hermes_last_chat.json"


def _session_name_for_chat(chat_id: int | str | None) -> str:
    """Stable Hermes session name per Telegram chat_id.

    Hermes ``chat --continue <name>`` resumes the conversation matching
    that name. We persist the most recent activity ts for the chat to a
    small JSON so a long gap (>``_HERMES_SESSION_TTL_S``) automatically
    rolls into a fresh session — yesterday's context doesn't drift into
    tonight's questions.
    """
    base = f"{_HERMES_SESSION_PREFIX}-{chat_id if chat_id is not None else 'default'}"
    rec = _read_json(_HERMES_LAST_CHAT_PATH) or {}
    entry = rec.get(str(chat_id)) if isinstance(rec, dict) else None
    if not isinstance(entry, dict):
        entry = {}
    last_ts_str = entry.get("ts")
    epoch_now = datetime.now(UTC).timestamp()
    last_ts_epoch = 0.0
    if isinstance(last_ts_str, str):
        try:
            last_ts_epoch = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00")).timestamp()
        except ValueError:
            last_ts_epoch = 0.0
    cycle = int(entry.get("cycle") or 0)
    if epoch_now - last_ts_epoch > _HERMES_SESSION_TTL_S:
        cycle += 1
    name = f"{base}-{cycle}"
    # Persist updated timestamp + cycle so the next call sees it.
    if isinstance(rec, dict):
        rec[str(chat_id)] = {"ts": _now_iso(), "cycle": cycle, "name": name}
        with contextlib.suppress(OSError):
            _write_json(_HERMES_LAST_CHAT_PATH, rec)
    return name


def _ask_hermes(prompt: str, *, chat_id: int | str | None = None) -> str:
    """Route a free-text operator message to Hermes Agent for a natural reply.

    Spawns ``hermes chat -q "<prompt>" -Q --source tool --continue <session>``
    in a subprocess, captures stdout, returns the formatted reply for Telegram.
    The ``--continue`` flag stitches multi-turn conversations together so
    follow-up messages keep context (e.g. "what about mes_sweep_reclaim?"
    after a "how's the fleet" message will know which fleet to look at).

    Why subprocess instead of the 8642 HTTP API: the gateway requires bearer
    auth that's harder to manage from the inbound bot. The CLI is the same
    binary the operator already uses; one-shot mode (-q + -Q) gives a clean
    string back without the chat banner.

    Truncation: Telegram reply cap is 4000 chars; we cap subprocess output
    at 3500 chars and let the send_reply truncate the rest. Multi-step tool
    use can produce LONG outputs — we want Hermes to summarize but if it
    doesn't, we still send something.

    NEVER raises. On any failure (subprocess crash, timeout, missing exe)
    returns a polite error string so the operator gets a Telegram reply.
    """
    import subprocess  # local import — only used on free-text path

    if not prompt or not prompt.strip():
        return "_empty message_"
    hermes_exe = os.environ.get("ETA_HERMES_CLI", _HERMES_EXE).strip()
    if not os.path.exists(hermes_exe):
        return f"_hermes CLI not found at_ `{hermes_exe}` — try `/help` for slash commands"
    safe_prompt = (
        "You are Hermes replying to the allowlisted ETA operator via Telegram. "
        "Default to read-only diagnostics and concise guidance. Do not place "
        "orders, flatten positions, start live trading, edit secrets, bypass "
        "readiness gates, bypass prop drawdown controls, or run destructive "
        "commands from this free-text channel. If the operator asks for one of "
        "those actions, explain the explicit approval or slash-command path "
        "required instead.\n\n"
        f"Operator message: {prompt.strip()}"
    )
    cmd = [
        hermes_exe,
        "chat",
        "-q",
        safe_prompt,
        "-Q",  # quiet: no banner / spinner / tool previews
        "--source",
        "tool",  # tag as third-party so it doesn't litter session lists
    ]
    # Continuity: same session name across messages from the same chat
    # so Hermes remembers prior turns. TTL-aware: a >6h gap auto-rolls
    # into a fresh session name. Disabled by setting ETA_TELEGRAM_HERMES_NO_CONTINUE=1.
    if not _env_truthy("ETA_TELEGRAM_HERMES_NO_CONTINUE"):
        session_name = _session_name_for_chat(chat_id)
        cmd.extend(["--continue", session_name])
    if _env_truthy("ETA_TELEGRAM_HERMES_ACCEPT_HOOKS"):
        cmd.append("--accept-hooks")
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=_HERMES_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"_hermes took too long to respond (>{_HERMES_TIMEOUT_S}s)._  Try a shorter prompt or use a `/command`."
    except FileNotFoundError as exc:
        return f"_hermes binary missing_: `{exc}`"
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes subprocess crashed: %s", exc)
        return f"_hermes invocation failed_: `{str(exc)[:200]}`"

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        logger.warning(
            "hermes returned rc=%s stderr=%s",
            proc.returncode,
            stderr[:200],
        )
        return f"_hermes exit {proc.returncode}_: `{stderr[:200] or '(no stderr)'}`"

    out = (proc.stdout or "").strip()
    if not out:
        return "_hermes returned empty output._  Try rephrasing."
    if len(out) > 3500:
        out = out[:3500].rstrip() + "\n\n_…(truncated)_"
    return out


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_allowed(chat_id: int, allowed: set[int]) -> bool:
    if not allowed:
        # No allowlist configured — treat as locked-down (fail closed)
        return False
    return chat_id in allowed


def process_update(update: dict[str, Any], allowed: set[int]) -> dict[str, Any] | None:
    """Handle one update from Telegram. Returns audit record (or None if not a message)."""
    msg = update.get("message") or {}
    if not msg:
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = str(msg.get("text") or "").strip()
    if not isinstance(chat_id, int) or not text:
        return None

    record: dict[str, Any] = {
        "asof": _now_iso(),
        "update_id": update.get("update_id"),
        "chat_id": chat_id,
        "from_username": (msg.get("from") or {}).get("username"),
        "text": text[:200],
        "allowed": False,
        "command": None,
        "reply_preview": None,
    }

    if not _is_allowed(chat_id, allowed):
        record["allowed"] = False
        send_reply(
            chat_id,
            "_This bot is private and not authorized for your chat._",
            reply_to_message_id=msg.get("message_id"),
        )
        _append_audit(record)
        return record

    record["allowed"] = True

    if text.startswith("/"):
        # First whitespace-separated token is the command name
        cmd_token = text.split()[0].split("@", 1)[0].lower()
        record["command"] = cmd_token
        reply = dispatch_command(text)
    else:
        # Free-text Hermes routing is powerful enough to deserve an explicit
        # operator opt-in. Slash commands remain the fail-closed default.
        if not _env_truthy("ETA_TELEGRAM_HERMES_FREE_TEXT"):
            record["command"] = "<free_text_blocked>"
            reply = (
                "_I only handle slash commands right now._  Try `/help` for the list. "
                "Set `ETA_TELEGRAM_HERMES_FREE_TEXT=1` to enable read-only Hermes replies."
            )
        else:
            record["command"] = "<free_text>"
            reply = _ask_hermes(text, chat_id=chat_id)

    record["reply_preview"] = reply[:200]
    send_reply(chat_id, reply, reply_to_message_id=msg.get("message_id"))
    _append_audit(record)
    return record


# ---------------------------------------------------------------------------
# Long-poll loop
# ---------------------------------------------------------------------------


_RUNNING = True


def _install_sigterm_handler() -> None:
    def _h(_signum: int, _frame: Any) -> None:  # noqa: ANN401
        global _RUNNING  # noqa: PLW0603
        _RUNNING = False
        logger.info("SIGTERM received, shutting down loop")

    for sig in (signal.SIGINT, signal.SIGTERM):
        # signal can't be set in some environments (Windows non-main thread)
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, _h)


def run_loop(once: bool = False) -> int:
    """Long-poll loop. Returns total updates processed."""
    _install_sigterm_handler()
    allowed = _allowed_chat_ids()
    if not allowed:
        logger.error("TELEGRAM_CHAT_ID env unset — refusing to run (fail-closed)")
        return 0

    offset = _load_offset()
    total = 0
    backoff_s = SHORT_BACKOFF_S

    while _RUNNING:
        try:
            updates = get_updates(offset)
        except Exception as exc:  # noqa: BLE001
            logger.exception("getUpdates loop crashed: %s", exc)
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, LONG_BACKOFF_S)
            continue
        backoff_s = SHORT_BACKOFF_S

        if not updates:
            if once:
                break
            continue

        for u in updates:
            try:
                process_update(u, allowed)
            except Exception as exc:  # noqa: BLE001
                logger.exception("process_update crashed: %s", exc)
            new_id = int(u.get("update_id", 0) or 0) + 1
            if new_id > offset:
                offset = new_id
                _save_offset(offset)
            total += 1

        if once:
            break

    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JARVIS Telegram inbound bot — long-poll listener.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain one getUpdates batch and exit (useful for smoke tests)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    n = run_loop(once=args.once)
    print(f"[telegram_inbound] processed {n} updates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
