"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_tradingview_capture
==============================================================
Headless TradingView capture daemon.

Operator flow (VPS-side, runs as a systemd service)::

    python -m eta_engine.scripts.run_tradingview_capture \
        --config        configs/tradingview.yaml          \
        --auth-state    ~/.local/state/eta_engine/tradingview_auth.json \
        --data-root     ~/apex_data/tradingview

The auth-state file MUST be generated locally on a workstation via
``scripts.tradingview_auth_refresh`` and then ``rsync``-ed to the VPS at
``~/.local/state/eta_engine/tradingview_auth.json`` (mode 0600).

Exit codes:
    0  clean shutdown (signal received)
    1  config / auth missing or malformed
    2  playwright not installed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.data.tradingview.client import TradingViewConfig

log = logging.getLogger("eta_engine.tradingview.capture")


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        sys.stderr.write("PyYAML required for tradingview.yaml; pip install pyyaml\n")
        raise SystemExit(1) from None
    if not path.exists():
        sys.stderr.write(f"tradingview config not found: {path}\n")
        raise SystemExit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_config(raw: dict[str, Any]) -> TradingViewConfig:
    from eta_engine.data.tradingview.client import ChartTarget, TradingViewConfig

    poll = raw.get("poll", {}) or {}
    targets_raw = raw.get("targets", []) or []
    targets = tuple(
        ChartTarget(
            symbol=t["symbol"],
            interval=str(t.get("interval", "1")),
            indicators=tuple(t.get("indicators", []) or ()),
        )
        for t in targets_raw
        if isinstance(t, dict) and t.get("symbol")
    )
    return TradingViewConfig(
        targets=targets,
        watchlist_url=raw.get("watchlist_url", "https://www.tradingview.com/watchlist/"),
        alerts_url=raw.get("alerts_url", "https://www.tradingview.com/alerts/"),
        chart_url_template=raw.get(
            "chart_url_template",
            "https://www.tradingview.com/chart/?symbol={symbol}&interval={interval}",
        ),
        poll_indicators_seconds=float(poll.get("indicators_seconds", 5.0)),
        poll_watchlist_seconds=float(poll.get("watchlist_seconds", 15.0)),
        poll_alerts_seconds=float(poll.get("alerts_seconds", 30.0)),
        headless=bool(raw.get("headless", True)),
        nav_timeout_ms=int(raw.get("nav_timeout_ms", 60_000)),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EVOLUTIONARY TRADING ALGO TradingView capture daemon")
    p.add_argument(
        "--config", type=Path, default=Path("configs/tradingview.yaml"),
        help="path to tradingview.yaml",
    )
    p.add_argument(
        "--auth-state", type=Path, default=None,
        help="path to Playwright storage_state JSON (default: "
             "~/.local/state/eta_engine/tradingview_auth.json)",
    )
    p.add_argument(
        "--data-root", type=Path, default=None,
        help="output directory (default ~/apex_data/tradingview)",
    )
    p.add_argument(
        "--max-runtime-seconds", type=int, default=0,
        help="max wall-clock seconds; 0 = run forever (until signal)",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    from eta_engine.data.tradingview.auth import AuthStateError, load_auth_state
    from eta_engine.data.tradingview.client import (
        TradingViewClient,
        TradingViewUnavailable,
    )
    from eta_engine.data.tradingview.journal import TradingViewJournal
    from eta_engine.obs.watchdog import (
        WatchdogPinger,
        notify_ready,
        notify_stopping,
    )

    if not TradingViewClient.is_available():
        sys.stderr.write(
            "playwright not installed; run "
            "`pip install playwright && playwright install chromium`\n",
        )
        return 2

    try:
        auth = load_auth_state(args.auth_state)
    except AuthStateError as e:
        sys.stderr.write(f"auth state error: {e}\n")
        return 1

    raw = _load_yaml(args.config)
    config = _build_config(raw)
    journal = TradingViewJournal(args.data_root)
    client = TradingViewClient(config, journal, auth_state=auth)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def _request_stop(*_: Any) -> None:  # noqa: ANN401 -- signal handler signature
        log.info("tradingview capture: stop signal received")
        client.request_stop()
        loop.call_soon_threadsafe(stop_event.set)

    for sig_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, sig_name):
            signal.signal(getattr(signal, sig_name), _request_stop)

    async def _supervised() -> None:
        # Tell systemd we're up; start sd_notify(WATCHDOG=1) keepalive.
        notify_ready()
        async with WatchdogPinger():
            run_task = asyncio.create_task(client.run())
            watchdog: asyncio.Task[None] | None = None
            if args.max_runtime_seconds > 0:
                async def _runtime_cap() -> None:
                    await asyncio.sleep(args.max_runtime_seconds)
                    client.request_stop()
                    stop_event.set()
                watchdog = asyncio.create_task(_runtime_cap())
            await stop_event.wait()
            run_task.cancel()
            if watchdog:
                watchdog.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            except TradingViewUnavailable as e:
                log.error("tradingview capture: %s", e)
        notify_stopping()

    try:
        loop.run_until_complete(_supervised())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
