"""Wave-25 status snapshot — unified operator dashboard surface.

Combines five wave-25 facts into one JSON receipt the operator can read
at a glance:

  1. Per-bot lifecycle state (EVAL_LIVE / EVAL_PAPER / FUNDED_LIVE / RETIRED)
  2. Live trade count per bot (last 24h, total)
  3. Paper trade count per bot
  4. Shadow signal count per bot (signals routed-to-paper)
  5. Time since last live trade per bot (stuck-bot detection)

Output: ``var/eta_engine/state/diamond_wave25_status_latest.json``

Designed for the prelaunch dryrun's freshness check + the operator-facing
ops dashboard. Cron-friendly: idempotent, exit 0 always.

Run::

    python -m eta_engine.scripts.diamond_wave25_status
    python -m eta_engine.scripts.diamond_wave25_status --json
"""
# ruff: noqa: PLR2004
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

OUT_LATEST = WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_wave25_status_latest.json"


def _safe_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _bots_to_check() -> list[str]:
    """Return the union of DIAMOND_BOTS + any bot with a lifecycle entry."""
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        BOT_LIFECYCLE_STATE_PATH,
        DIAMOND_BOTS,
    )

    bots: set[str] = set(DIAMOND_BOTS)
    if BOT_LIFECYCLE_STATE_PATH.exists():
        try:
            data = json.loads(BOT_LIFECYCLE_STATE_PATH.read_text(encoding="utf-8"))
            for k in (data.get("bots") or {}):
                bots.add(str(k))
        except (OSError, json.JSONDecodeError):
            pass
    return sorted(bots)


def _per_bot_status(bot_id: str) -> dict:
    from eta_engine.feeds.capital_allocator import (  # noqa: PLC0415
        get_bot_lifecycle,
    )
    from eta_engine.scripts.closed_trade_ledger import (  # noqa: PLC0415
        DATA_SOURCE_LIVE,
        DATA_SOURCE_PAPER,
        load_close_records,
    )
    from eta_engine.scripts.shadow_signal_logger import (  # noqa: PLC0415
        read_shadow_signals,
    )

    lifecycle = get_bot_lifecycle(bot_id)

    # Live trades (last 7d for stuck-bot detection)
    live_rows = load_close_records(
        bot_filter=bot_id,
        data_sources=frozenset({DATA_SOURCE_LIVE}),
        since_days=7,
    )
    paper_rows = load_close_records(
        bot_filter=bot_id,
        data_sources=frozenset({DATA_SOURCE_PAPER}),
        since_days=7,
    )

    n_live_24h = sum(
        1
        for r in live_rows
        if (datetime.now(UTC) - datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00"))).total_seconds()
        < 86400
    )
    n_paper_24h = sum(
        1
        for r in paper_rows
        if (datetime.now(UTC) - datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00"))).total_seconds()
        < 86400
    )

    last_live_ts = max((r.get("ts", "") for r in live_rows), default=None)
    seconds_since_live = None
    if last_live_ts:
        try:
            dt = datetime.fromisoformat(str(last_live_ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            seconds_since_live = (datetime.now(UTC) - dt).total_seconds()
        except ValueError:
            seconds_since_live = None

    # Shadow signals (last 7d)
    since = datetime.now(UTC) - timedelta(days=7)
    shadow = read_shadow_signals(bot_filter=bot_id, since=since)

    return {
        "bot_id": bot_id,
        "lifecycle": lifecycle,
        "n_live_7d": len(live_rows),
        "n_live_24h": n_live_24h,
        "n_paper_7d": len(paper_rows),
        "n_paper_24h": n_paper_24h,
        "n_shadow_7d": len(shadow),
        "last_live_ts": last_live_ts,
        "seconds_since_last_live_trade": seconds_since_live,
    }


def _alert_channel_status() -> dict:
    """Detect which push channels are configured (without leaking creds)."""
    import os

    telegram_ok = bool(
        os.environ.get("ETA_TELEGRAM_BOT_TOKEN") and os.environ.get("ETA_TELEGRAM_CHAT_ID"),
    )
    return {
        "telegram_configured": telegram_ok,
        "discord_configured": bool(os.environ.get("ETA_DISCORD_WEBHOOK_URL")),
        "generic_webhook_configured": bool(os.environ.get("ETA_GENERIC_WEBHOOK_URL")),
    }


def _ledger_truth_summary() -> dict:
    """Per-data-source counts across ALL records (the pollution snapshot)."""
    from eta_engine.scripts.closed_trade_ledger import (  # noqa: PLC0415
        load_close_records,
    )

    all_rows = load_close_records(data_sources=None)
    counts: dict[str, int] = {}
    for r in all_rows:
        ds = str(r.get("_data_source") or "?")
        counts[ds] = counts.get(ds, 0) + 1
    return dict(sorted(counts.items()))


def build_status_report() -> dict:
    bots = _bots_to_check()
    bot_status = [_per_bot_status(b) for b in bots]

    n_eval_live = sum(1 for b in bot_status if b["lifecycle"] == "EVAL_LIVE")
    n_eval_paper = sum(1 for b in bot_status if b["lifecycle"] == "EVAL_PAPER")
    n_funded_live = sum(1 for b in bot_status if b["lifecycle"] == "FUNDED_LIVE")
    n_retired = sum(1 for b in bot_status if b["lifecycle"] == "RETIRED")
    total_live_24h = sum(b["n_live_24h"] for b in bot_status)
    total_paper_24h = sum(b["n_paper_24h"] for b in bot_status)
    total_shadow_7d = sum(b["n_shadow_7d"] for b in bot_status)

    return {
        "ts": _safe_iso(datetime.now(UTC)),
        "n_bots_total": len(bots),
        "lifecycle_breakdown": {
            "EVAL_LIVE": n_eval_live,
            "EVAL_PAPER": n_eval_paper,
            "FUNDED_LIVE": n_funded_live,
            "RETIRED": n_retired,
        },
        "totals_24h": {
            "live_trades": total_live_24h,
            "paper_trades": total_paper_24h,
        },
        "totals_7d": {
            "shadow_signals": total_shadow_7d,
        },
        "alert_channels": _alert_channel_status(),
        "ledger_pollution_snapshot": _ledger_truth_summary(),
        "per_bot": bot_status,
    }


def _print_table(report: dict) -> None:
    print()
    print("=" * 100)
    print(f" WAVE-25 STATUS  ({report['ts']})  bots={report['n_bots_total']}")
    print("=" * 100)
    lc = report["lifecycle_breakdown"]
    lc_line = (
        f"  lifecycle: EVAL_LIVE={lc['EVAL_LIVE']} EVAL_PAPER={lc['EVAL_PAPER']} "
        f"FUNDED_LIVE={lc['FUNDED_LIVE']} RETIRED={lc['RETIRED']}"
    )
    print(lc_line)
    t24 = report["totals_24h"]
    print(f"  24h: live_trades={t24['live_trades']}  paper_trades={t24['paper_trades']}")
    print(f"  7d : shadow_signals={report['totals_7d']['shadow_signals']}")
    ac = report["alert_channels"]
    chan = []
    if ac["telegram_configured"]:
        chan.append("telegram")
    if ac["discord_configured"]:
        chan.append("discord")
    if ac["generic_webhook_configured"]:
        chan.append("generic_webhook")
    chan_text = "+".join(chan) if chan else "(NONE -- HALT will only show on dashboard)"
    print(f"  alerts: {chan_text}")
    print("  ledger pollution snapshot:")
    for ds, n in report["ledger_pollution_snapshot"].items():
        print(f"    {ds}: {n}")
    print()
    header = (
        f"  {'bot_id':<28} {'lifecycle':<14} "
        f"{'live_24h':>9} {'paper_24h':>10} {'shadow_7d':>10} {'sec_since_live':>14}"
    )
    print(header)
    print("  " + "-" * 90)
    for b in report["per_bot"]:
        sec = b["seconds_since_last_live_trade"]
        sec_s = f"{int(sec)}" if sec is not None else "-"
        row = (
            f"  {b['bot_id']:<28} {b['lifecycle']:<14} "
            f"{b['n_live_24h']:>9} {b['n_paper_24h']:>10} "
            f"{b['n_shadow_7d']:>10} {sec_s:>14}"
        )
        print(row)


def write_report(report: dict, path: Path = OUT_LATEST) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    report = build_status_report()
    if not args.no_write:
        write_report(report)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
