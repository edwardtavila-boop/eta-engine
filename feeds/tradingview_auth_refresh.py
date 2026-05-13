"""
EVOLUTIONARY TRADING ALGO  //  scripts.tradingview_auth_refresh
===============================================================
Interactive auth-state refresher for the TradingView capture daemon.

Run this on a workstation (NOT the VPS). It opens a real, visible
Chromium window pointed at TradingView's signin page. The operator logs
in (including 2FA), navigates to the chart pages they want indicators
loaded on, and closes the window. The script then writes the resulting
``storage_state`` JSON to ``var/eta_engine/state/tradingview_auth.json``
(0600 mode) -- ``rsync`` that file to the VPS.

Usage::

    python -m eta_engine.scripts.tradingview_auth_refresh
    python -m eta_engine.scripts.tradingview_auth_refresh --out /tmp/tv.json

Flags:
    --out PATH        write target (default ``var/eta_engine/state/tradingview_auth.json``)
    --start-url URL   landing page (default ``https://www.tradingview.com/#signin``)
    --signed-in-host  hostname Playwright should reach before exit-on-close
                      (default ``www.tradingview.com``)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

log = logging.getLogger("eta_engine.tradingview.auth_refresh")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate a TradingView auth-state JSON for the capture daemon",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default var/eta_engine/state/tradingview_auth.json)",
    )
    p.add_argument(
        "--start-url",
        default="https://www.tradingview.com/#signin",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.stderr.write(
            "playwright not installed; run `pip install playwright && playwright install chromium`\n",
        )
        return 2

    from eta_engine.data.tradingview.auth import save_auth_state

    async def _run() -> int:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(args.start_url)
            log.info(
                "log into TradingView in the open window. "
                "Add the indicators you want recorded onto the charts. "
                "Close the window when done.",
            )
            # Wait until the user closes the page or navigates away.
            with contextlib.suppress(Exception):
                await page.wait_for_event("close", timeout=0)

            # Snapshot before closing context.
            try:
                state = await context.storage_state()
            finally:
                await browser.close()

        path = save_auth_state(state, args.out)
        log.info("tradingview auth state written: %s", path)
        if path.parent.exists():
            log.info(
                "copy %s to the VPS canonical workspace path: var/eta_engine/state/tradingview_auth.json",
                path,
            )
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
