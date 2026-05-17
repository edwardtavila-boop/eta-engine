"""Monday-morning first-light check — fires once at 09:25 ET (5 min before RTH open).

Verifies the wave-25 stack is alive and ready, and pushes a GO/NO_GO
ping to the operator's Telegram if any channel is configured.

What it checks
--------------
  1. Supervisor heartbeat is fresh (< 5 min)
  2. Drawdown guard signal is OK (no overnight HALT carryover)
  3. At least one bot is opted into EVAL_LIVE or FUNDED_LIVE
  4. Telegram alert channel is configured (warn-only if not)
  5. Recent shadow-signal activity proves the gate is firing

If ALL checks pass → push a "FIRST LIGHT GO" message
If ANY check fails → push a "FIRST LIGHT NO_GO: <reason>" message

The Telegram push uses the same urllib pattern the wave-24 dispatcher
uses (no third-party deps). The check exits with the same exit codes
as ``prop_launch_check``:
  0 = GO  / 1 = HOLD / 2 = NO_GO

Schedule it as a one-shot trigger at 09:25 ET on prop-launch days.
"""
# ruff: noqa: T201, S310
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts.retune_advisory_cache import build_retune_advisory, summarize_active_experiment
from eta_engine.scripts import workspace_roots

WORKSPACE_ROOT = workspace_roots.WORKSPACE_ROOT
HEALTH_DIR = workspace_roots.ETA_RUNTIME_HEALTH_DIR
HEARTBEAT_PATH = workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH
DRAWDOWN_PATH = workspace_roots.ETA_DIAMOND_PROP_DRAWDOWN_GUARD_PATH
SHADOW_SIGNALS_PATH = workspace_roots.ETA_JARVIS_SHADOW_SIGNALS_PATH

HEARTBEAT_STALE_SECONDS = 300  # 5 min
SHADOW_RECENT_SECONDS = 600  # 10 min
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


@dataclass
class Check:
    name: str
    status: str  # "GO" | "HOLD" | "NO_GO"
    detail: str


def _safe_load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _health_dir() -> Path:
    return HEALTH_DIR


def _load_json_dict(path: Path) -> dict:
    payload = _safe_load_json(path)
    if isinstance(payload, dict):
        return payload
    return {}


def _dict_field(payload: dict, key: str) -> dict:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _string_list(payload: dict, key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _retune_advisory() -> dict:
    return build_retune_advisory(_health_dir())


def _check_supervisor_alive() -> Check:
    if not HEARTBEAT_PATH.exists():
        return Check("supervisor", "NO_GO", f"heartbeat missing at {HEARTBEAT_PATH}")
    age = datetime.now(UTC).timestamp() - HEARTBEAT_PATH.stat().st_mtime
    if age > HEARTBEAT_STALE_SECONDS:
        return Check("supervisor", "NO_GO", f"heartbeat stale ({age:.0f}s); task may be hung")
    d = _safe_load_json(HEARTBEAT_PATH) or {}
    return Check(
        "supervisor",
        "GO",
        f"alive: tick={d.get('tick_count')} mode={d.get('mode')} bots={d.get('n_bots')}",
    )


def _check_drawdown_clear() -> Check:
    d = _safe_load_json(DRAWDOWN_PATH)
    if d is None:
        return Check("drawdown_guard", "HOLD", "no receipt; cron may not have run yet")
    signal = d.get("signal", "?")
    if signal == "HALT":
        return Check("drawdown_guard", "NO_GO", f"HALT carryover: {d.get('rationale', '')}")
    if signal == "WATCH":
        return Check("drawdown_guard", "HOLD", "WATCH active: supervisor will halve sizes")
    return Check("drawdown_guard", "GO", f"signal={signal}")


def _check_lifecycle_opt_in() -> Check:
    from eta_engine.feeds.capital_allocator import (
        DIAMOND_BOTS,
        LIFECYCLE_EVAL_LIVE,
        LIFECYCLE_FUNDED_LIVE,
        get_bot_lifecycle,
    )

    live = [b for b in DIAMOND_BOTS if get_bot_lifecycle(b) in {LIFECYCLE_EVAL_LIVE, LIFECYCLE_FUNDED_LIVE}]
    if not live:
        return Check(
            "lifecycle",
            "NO_GO",
            "no bot opted into live execution; fleet is paper-only",
        )
    return Check("lifecycle", "GO", f"{len(live)} bot(s) live: {live}")


def _check_alert_channel() -> Check:
    from eta_engine.scripts import alert_channel_config  # noqa: PLC0415

    telegram = alert_channel_config.telegram_configured()
    discord = alert_channel_config.discord_configured()
    generic = alert_channel_config.generic_configured()
    if not (telegram or discord or generic):
        return Check(
            "alert_channel",
            "HOLD",
            "no push channels configured; HALT will only show on dashboard",
        )
    channels = []
    if telegram:
        channels.append("telegram")
    if discord:
        channels.append("discord")
    if generic:
        channels.append("generic")
    return Check("alert_channel", "GO", f"channels: {'+'.join(channels)}")


def _check_recent_shadow_activity() -> Check:
    """Wave-25 gate must be firing — recent shadow signal proves it."""
    if not SHADOW_SIGNALS_PATH.exists():
        return Check("gate_activity", "HOLD", "no shadow_signals.jsonl yet (system new or gate not firing)")
    age = datetime.now(UTC).timestamp() - SHADOW_SIGNALS_PATH.stat().st_mtime
    if age > SHADOW_RECENT_SECONDS:
        return Check("gate_activity", "HOLD", f"last shadow signal {age:.0f}s ago — gate may be idle")
    return Check("gate_activity", "GO", f"last shadow signal {age:.0f}s ago — gate is firing")


def _push_telegram(message: str) -> tuple[bool, str]:
    from eta_engine.scripts import alert_channel_config  # noqa: PLC0415

    token = alert_channel_config.get_telegram_bot_token()
    chat_id = alert_channel_config.get_telegram_chat_id()
    if not (token and chat_id):
        return False, "telegram not configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                return False, f"telegram api not-ok: {body}"
            return True, "sent"
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        return False, f"network error: {exc}"


def _aggregate(checks: list[Check]) -> tuple[str, str]:
    if any(c.status == "NO_GO" for c in checks):
        ng = [c.name for c in checks if c.status == "NO_GO"]
        return "NO_GO", f"NO_GO: {', '.join(ng)}"
    if any(c.status == "HOLD" for c in checks):
        h = [c.name for c in checks if c.status == "HOLD"]
        return "HOLD", f"HOLD: {', '.join(h)}"
    return "GO", "all checks green"


def _format_telegram_body(
    verdict: str,
    summary: str,
    checks: list[Check],
    retune_advisory: dict | None = None,
) -> str:
    emoji = {"GO": "🟢", "HOLD": "🟡", "NO_GO": "🔴"}.get(verdict, "❓")
    lines = [
        f"{emoji} *FIRST LIGHT {verdict}*",
        f"_{datetime.now(UTC).isoformat()}_",
        f"`{summary}`",
        "",
    ]
    if retune_advisory and retune_advisory.get("available"):
        focus_pnl = retune_advisory.get("focus_total_realized_pnl")
        focus_pf = retune_advisory.get("focus_profit_factor")
        pnl_text = f"${focus_pnl:+.2f}" if isinstance(focus_pnl, int | float) else "n/a"
        pf_text = f"{focus_pf:.2f}" if isinstance(focus_pf, int | float) else "n/a"
        lines.append(
            "Retune truth: "
            f"{retune_advisory.get('focus_bot')} "
            f"{retune_advisory.get('focus_state')} "
            f"issue={retune_advisory.get('focus_issue')}"
        )
        lines.append(
            "Broker proof: "
            f"closes={retune_advisory.get('focus_closed_trade_count')} "
            f"pnl={pnl_text} pf={pf_text}"
        )
        if retune_advisory.get("diagnosis"):
            lines.append(f"Local drift: {retune_advisory.get('diagnosis')}")
        if retune_advisory.get("preferred_warning"):
            lines.append(f"Warning: {retune_advisory.get('preferred_warning')}")
        experiment = summarize_active_experiment(retune_advisory.get("active_experiment"))
        if experiment:
            lines.append(f"Post-fix experiment: {experiment['headline']}")
            lines.append(
                f"partial_profit_enabled={experiment['partial_profit_enabled_text']} "
                f"closes={experiment['post_change_closed_trade_count_text']} "
                f"pnl={experiment['post_change_total_realized_pnl_text']} "
                f"pf={experiment['post_change_profit_factor_text']}"
            )
        lines.append("")
    for c in checks:
        mark = {"GO": "OK", "HOLD": "??", "NO_GO": "XX"}.get(c.status, "?")
        lines.append(f"[{mark}] {c.name}: {c.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Skip the Telegram push (use for dry-runs / smoke tests)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    checks = [
        _check_supervisor_alive(),
        _check_drawdown_clear(),
        _check_lifecycle_opt_in(),
        _check_alert_channel(),
        _check_recent_shadow_activity(),
    ]
    retune_advisory = _retune_advisory()
    verdict, summary = _aggregate(checks)

    report = {
        "ts": datetime.now(UTC).isoformat(),
        "verdict": verdict,
        "summary": summary,
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        "retune_advisory": retune_advisory,
    }

    # Push to Telegram unless --no-push
    push_result = None
    if not args.no_push:
        body = _format_telegram_body(verdict, summary, checks, retune_advisory=retune_advisory)
        ok, detail = _push_telegram(body)
        push_result = {"sent": ok, "detail": detail}
        report["telegram"] = push_result

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        emoji = {"GO": "[OK]", "HOLD": "[??]", "NO_GO": "[XX]"}.get(verdict, "[?]")
        print()
        print("=" * 70)
        print(f"  {emoji} FIRST LIGHT CHECK  verdict={verdict}")
        print("=" * 70)
        print(f"  {summary}")
        print()
        for c in checks:
            mark = {"GO": " OK", "HOLD": " ??", "NO_GO": " XX"}.get(c.status, "  ?")
            print(f"  [{mark}] {c.name:<18}  {c.detail}")
        if retune_advisory.get("available"):
            focus_pnl = retune_advisory.get("focus_total_realized_pnl")
            focus_pf = retune_advisory.get("focus_profit_factor")
            broker_mtd = retune_advisory.get("broker_mtd_pnl")
            pnl_text = f"${focus_pnl:+.2f}" if isinstance(focus_pnl, int | float) else "n/a"
            pf_text = f"{focus_pf:.2f}" if isinstance(focus_pf, int | float) else "n/a"
            mtd_text = f"${broker_mtd:+.2f}" if isinstance(broker_mtd, int | float) else "n/a"
            print()
            print(
                "  retune advisory: "
                f"{retune_advisory.get('focus_bot')} "
                f"{retune_advisory.get('focus_state')} "
                f"issue={retune_advisory.get('focus_issue')}"
            )
            print(
                "                   "
                f"closes={retune_advisory.get('focus_closed_trade_count')} "
                f"pnl={pnl_text} pf={pf_text} mtd={mtd_text}"
            )
            if retune_advisory.get("diagnosis"):
                print(f"                   drift={retune_advisory.get('diagnosis')}")
            experiment = summarize_active_experiment(retune_advisory.get("active_experiment"))
            if experiment:
                print(f"                   post-fix experiment: {experiment['headline']}")
                print(
                    "                                       "
                    f"partial_profit_enabled={experiment['partial_profit_enabled_text']} "
                    f"closes={experiment['post_change_closed_trade_count_text']} "
                    f"pnl={experiment['post_change_total_realized_pnl_text']} "
                    f"pf={experiment['post_change_profit_factor_text']}"
                )
        if push_result is not None:
            sent_mark = "OK" if push_result["sent"] else "FAIL"
            print()
            print(f"  Telegram push: [{sent_mark}] {push_result['detail']}")

    return {"GO": 0, "HOLD": 1, "NO_GO": 2}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
