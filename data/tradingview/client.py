"""
EVOLUTIONARY TRADING ALGO  //  data.tradingview.client
======================================================
Headless-Chrome TradingView client driven by Playwright.

Architecture
------------

A single :class:`TradingViewClient` owns a Playwright ``BrowserContext``
(chromium, headless) loaded with the operator's saved auth state. It
opens four tabs in parallel:

* **Charts tab(s)** -- one per symbol/interval. Subscribes to
  ``Network.webSocketFrameReceived`` via CDP and forwards every frame
  through :func:`parsers.parse_quote_frame` -> :class:`TradingViewJournal`.
* **Indicator polling tab(s)** -- same chart pages also expose the
  legend tooltips. A periodic ``page.evaluate`` snapshot reads the
  legend text and forwards it through :func:`parsers.parse_indicator_tooltip`.
* **Watchlist tab** -- ``/watchlist/`` page; periodic DOM scrape of the
  symbol-list rows.
* **Alerts tab** -- ``/alerts/`` page; periodic DOM scrape + the
  alert-fired notification observer (via a CDP DOM-mutation hook).

Playwright is imported lazily inside :meth:`run` -- the module stays
importable without it. ``is_available()`` reports whether the runtime
is installed.

Determinism + safety
--------------------

* The client never executes JavaScript that issues TradingView API calls.
  It is read-only: it observes the same DOM/WS the human user does.
* Each tab carries its own try/except wall: a single tab dying logs an
  error and keeps the rest running.
* No symbol/indicator config is hardcoded; the caller passes a
  :class:`TradingViewConfig` (typically loaded from ``configs/tradingview.yaml``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eta_engine.data.tradingview.auth import AuthState, load_auth_state
from eta_engine.data.tradingview.journal import (
    AlertEntry,
    BarEntry,
    IndicatorEntry,
    TradingViewJournal,
    WatchlistSnapshot,
    now_iso,
)
from eta_engine.data.tradingview.parsers import (
    parse_alert_row,
    parse_indicator_tooltip,
    parse_quote_frame,
    parse_watchlist_row,
)

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


class TradingViewClientError(Exception):
    """Configuration or runtime error in the TradingView client."""


class TradingViewUnavailable(TradingViewClientError):  # noqa: N818 -- "Unavailable" reads better than "UnavailableError" at call sites
    """Raised when ``playwright`` isn't installed and ``run()`` is called."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChartTarget:
    """A single chart to watch.

    ``symbol`` is the TradingView convention ``EXCHANGE:TICKER`` (e.g.
    ``BINANCE:BTCUSDT``, ``CME_MINI:NQ1!``). ``interval`` is one of
    ``1, 5, 15, 60, 240, 1D``. ``indicators`` are the human-readable
    legend names to record (e.g. ``["RSI", "MACD", "Volume MA"]``); the
    indicator scrape recognizes any legend row whose name starts with
    one of these.
    """

    symbol: str
    interval: str = "1"
    indicators: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradingViewConfig:
    targets: tuple[ChartTarget, ...] = field(default_factory=tuple)
    watchlist_url: str = "https://www.tradingview.com/watchlist/"
    alerts_url: str = "https://www.tradingview.com/alerts/"
    chart_url_template: str = "https://www.tradingview.com/chart/?symbol={symbol}&interval={interval}"
    poll_indicators_seconds: float = 5.0
    poll_watchlist_seconds: float = 15.0
    poll_alerts_seconds: float = 30.0
    headless: bool = True
    nav_timeout_ms: int = 60_000


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TradingViewClient:
    """Headless TradingView capture driver (Playwright-based)."""

    def __init__(
        self,
        config: TradingViewConfig,
        journal: TradingViewJournal,
        auth_state: AuthState | None = None,
        auth_path: Path | str | None = None,
    ) -> None:
        if not config.targets and not config.watchlist_url and not config.alerts_url:
            raise TradingViewClientError(
                "TradingViewConfig has no targets, watchlist, or alerts URL",
            )
        self.config = config
        self.journal = journal
        self.auth_state = auth_state or load_auth_state(auth_path)
        if not self.auth_state.has_session_cookie:
            log.warning(
                "tradingview client: auth state has no sessionid cookie; auth refresh likely required",
            )
        self._stop = False

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------
    @staticmethod
    def is_available() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    def request_stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Open tabs, register listeners, poll until ``request_stop``."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise TradingViewUnavailable(
                "playwright is not installed; install with `pip install playwright` "
                "and run `playwright install chromium`",
            ) from e

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.config.headless)
            try:
                context = await browser.new_context(
                    storage_state=self.auth_state.to_storage_state(),
                )
                tasks: list[asyncio.Task[None]] = []
                for target in self.config.targets:
                    tasks.append(asyncio.create_task(self._run_chart_tab(context, target)))
                if self.config.watchlist_url:
                    tasks.append(asyncio.create_task(self._run_watchlist_tab(context)))
                if self.config.alerts_url:
                    tasks.append(asyncio.create_task(self._run_alerts_tab(context)))
                if not tasks:
                    return
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*tasks)
            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Chart tab: WS frame intercept + indicator legend poll
    # ------------------------------------------------------------------
    async def _run_chart_tab(self, context: Any, target: ChartTarget) -> None:  # noqa: ANN401 -- Playwright BrowserContext is optional at import time
        url = self.config.chart_url_template.format(
            symbol=target.symbol,
            interval=target.interval,
        )
        try:
            page = await context.new_page()
            await page.goto(url, timeout=self.config.nav_timeout_ms)

            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")

            def _on_ws_frame(event: dict[str, Any]) -> None:  # noqa: ANN401 -- CDP event is dynamically-shaped JSON
                payload = event.get("response", {}).get("payloadData", "")
                for rec in parse_quote_frame(payload):
                    if rec["kind"] == "bar":
                        self.journal.record_bar(
                            BarEntry(
                                ts=rec["ts"],
                                symbol=rec["symbol"],
                                interval=target.interval,
                                o=rec["o"],
                                h=rec["h"],
                                l=rec["l"],
                                c=rec["c"],
                                v=rec["v"],
                            )
                        )

            cdp.on("Network.webSocketFrameReceived", _on_ws_frame)

            await self._poll_indicators(page, target)
        except Exception as e:  # noqa: BLE001 -- isolate per-tab faults
            log.error("tradingview client: chart tab %s crashed: %s", target.symbol, e)

    async def _poll_indicators(self, page: Any, target: ChartTarget) -> None:  # noqa: ANN401 -- Playwright Page is optional at import time
        legend_js = "Array.from(document.querySelectorAll('[data-name=\"legend-source-item\"]')).map(e=>e.innerText)"
        while not self._stop:
            try:
                rows: list[str] = await page.evaluate(legend_js)
                for row in rows:
                    parsed = parse_indicator_tooltip(row)
                    if not parsed:
                        continue
                    if target.indicators and not _name_matches(
                        parsed["indicator"],
                        target.indicators,
                    ):
                        continue
                    self.journal.record_indicator(
                        IndicatorEntry(
                            ts=now_iso(),
                            symbol=target.symbol,
                            interval=target.interval,
                            indicator=parsed["indicator"],
                            params=parsed["params"],
                            value=parsed["value"],
                            all=parsed["all"],
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("tradingview client: indicator poll error: %s", e)
            await asyncio.sleep(self.config.poll_indicators_seconds)

    # ------------------------------------------------------------------
    # Watchlist tab
    # ------------------------------------------------------------------
    async def _run_watchlist_tab(self, context: Any) -> None:  # noqa: ANN401 -- Playwright BrowserContext is optional at import time
        page = None
        try:
            page = await context.new_page()
            await page.goto(self.config.watchlist_url, timeout=self.config.nav_timeout_ms)
        except Exception as e:  # noqa: BLE001
            log.error("tradingview client: watchlist tab open failed: %s", e)
            return
        scrape_js = (
            "Array.from("
            "document.querySelectorAll('.tv-screener-table__row, [data-rowkey]')"
            ").map(r=>({"
            "  symbol: r.querySelector('[data-field=symbol], .tv-symbol-name')?.innerText||'',"
            "  last:   r.querySelector('[data-field=last_price], .tv-last-price')?.innerText||'',"
            "  chg:    r.querySelector('[data-field=change_pct], .tv-change-pct')?.innerText||'',"
            "  vol:    r.querySelector('[data-field=volume], .tv-volume')?.innerText||''"
            "}))"
        )
        while not self._stop:
            try:
                rows: list[dict[str, Any]] = await page.evaluate(scrape_js)
                normalized = [r for r in (parse_watchlist_row(x) for x in rows) if r]
                self.journal.record_watchlist(
                    WatchlistSnapshot(
                        ts=now_iso(),
                        lists={"default": normalized},
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.warning("tradingview client: watchlist scrape error: %s", e)
            await asyncio.sleep(self.config.poll_watchlist_seconds)

    # ------------------------------------------------------------------
    # Alerts tab
    # ------------------------------------------------------------------
    async def _run_alerts_tab(self, context: Any) -> None:  # noqa: ANN401 -- Playwright BrowserContext is optional at import time
        page = None
        try:
            page = await context.new_page()
            await page.goto(self.config.alerts_url, timeout=self.config.nav_timeout_ms)
        except Exception as e:  # noqa: BLE001
            log.error("tradingview client: alerts tab open failed: %s", e)
            return
        scrape_js = (
            "Array.from("
            "document.querySelectorAll('.alerts-list-item, [data-name=alert-list-item]')"
            ").map(r=>({"
            "  name:      r.querySelector('.title, [data-name=alert-title]')?.innerText||'',"
            "  symbol:    r.dataset.symbol||r.querySelector('[data-field=symbol]')?.innerText||'',"
            "  condition: r.querySelector('[data-name=alert-condition]')?.innerText||'',"
            "  value:     r.querySelector('[data-name=alert-value]')?.innerText||'',"
            "  active:    !r.classList.contains('disabled'),"
            "  fired_at:  r.querySelector('[data-name=alert-last-fired]')?.dateTime||null"
            "}))"
        )
        while not self._stop:
            try:
                rows: list[dict[str, Any]] = await page.evaluate(scrape_js)
                for raw in rows:
                    parsed = parse_alert_row(raw)
                    if not parsed:
                        continue
                    self.journal.record_alert(
                        AlertEntry(
                            ts=now_iso(),
                            kind="fired" if parsed.get("fired_at") else "definition",
                            symbol=parsed["symbol"],
                            name=parsed["name"],
                            condition=parsed["condition"],
                            value=parsed["value"],
                            active=parsed["active"],
                            fired_at=parsed["fired_at"],
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("tradingview client: alerts scrape error: %s", e)
            await asyncio.sleep(self.config.poll_alerts_seconds)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _name_matches(name: str, prefixes: tuple[str, ...]) -> bool:
    name_l = name.lower()
    return any(name_l.startswith(p.lower()) for p in prefixes)
