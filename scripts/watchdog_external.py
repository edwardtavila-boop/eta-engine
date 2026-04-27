"""
EVOLUTIONARY TRADING ALGO  //  scripts.watchdog_external
============================================
External kill-switch watchdog — runs OFF the trading VPS.

Red Team blocker #1 from the pre-build memo and post-build flag from
the Risk-Execution review: if the trading VPS goes dark, the on-VPS
kill-switch is dead with it. The operator's positions keep racking up
funding, can get liquidated, and there's no way to flatten.

This module implements the cloud-side watchdog: it runs on a cheap
small VPS (or as a cron-triggered cloud function), polls the trading
VPS's heartbeat endpoint, and when the heartbeat goes stale longer
than the configured timeout, it hits each venue's PRIVATE cancel-all
+ flatten endpoints directly using independent API credentials.

Deployment
----------
1. Spin up a cheap secondary VPS in a different region (AWS Lightsail
   / DigitalOcean / Hetzner).
2. Copy a REDUCED-SCOPE API key for each venue to this machine —
   cancel-all + close-positions permissions only. NOT trade/read/deposit.
3. Copy the trading VPS's heartbeat URL.
4. Run this script as a systemd service or Windows Task Scheduler task.
5. Alerts go to Telegram + SMS (via Twilio). Configure via env.

This is a SKELETON. The cancel-all logic delegates to venue adapters
that the operator fills in with the reduced-scope credentials. Read
this file end to end before deploying.

Environment variables
---------------------
    WATCHDOG_HEARTBEAT_URL=https://trading-vps.example.com/hb
    WATCHDOG_HEARTBEAT_TIMEOUT_S=90
    WATCHDOG_POLL_INTERVAL_S=15
    WATCHDOG_KRAKEN_KEY / WATCHDOG_KRAKEN_SECRET
    WATCHDOG_IBKR_ACCOUNT_ID  (uses Client Portal Gateway)
    WATCHDOG_HYPERLIQUID_ENABLED=false
    WATCHDOG_HYPERLIQUID_SIGNER=/path/to/encrypted/key
    WATCHDOG_TELEGRAM_TOKEN / WATCHDOG_TELEGRAM_CHAT_ID
    WATCHDOG_TWILIO_ACCOUNT_SID / WATCHDOG_TWILIO_AUTH_TOKEN / WATCHDOG_SMS_TO
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

logger = logging.getLogger("watchdog_external")


class WatchdogState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    TRIGGERED = "triggered"


@dataclass
class WatchdogConfig:
    heartbeat_url: str
    heartbeat_timeout_s: float = 90.0
    poll_interval_s: float = 15.0
    degraded_threshold_s: float = 45.0  # alert-only below timeout
    kraken_enabled: bool = True
    ibkr_enabled: bool = True
    hyperliquid_enabled: bool = False
    telegram_enabled: bool = True
    sms_enabled: bool = True
    dry_run: bool = False  # when True, skips real flatten calls

    @classmethod
    def from_env(cls) -> WatchdogConfig:
        def _bool(name: str, default: bool) -> bool:
            v = os.environ.get(name, "")
            return v.lower() in {"1", "true", "yes"} if v else default

        return cls(
            heartbeat_url=os.environ.get("WATCHDOG_HEARTBEAT_URL", ""),
            heartbeat_timeout_s=float(os.environ.get("WATCHDOG_HEARTBEAT_TIMEOUT_S", "90")),
            poll_interval_s=float(os.environ.get("WATCHDOG_POLL_INTERVAL_S", "15")),
            degraded_threshold_s=float(
                os.environ.get("WATCHDOG_DEGRADED_THRESHOLD_S", "45"),
            ),
            kraken_enabled=_bool("WATCHDOG_KRAKEN_ENABLED", True),
            ibkr_enabled=_bool("WATCHDOG_IBKR_ENABLED", True),
            hyperliquid_enabled=_bool("WATCHDOG_HYPERLIQUID_ENABLED", False),
            telegram_enabled=_bool("WATCHDOG_TELEGRAM_ENABLED", True),
            sms_enabled=_bool("WATCHDOG_SMS_ENABLED", True),
            dry_run=_bool("WATCHDOG_DRY_RUN", False),
        )


@dataclass
class WatchdogRuntime:
    """Running state. Persists across poll iterations."""

    last_heartbeat_ts: float = 0.0
    last_heartbeat_body: str = ""
    state: WatchdogState = WatchdogState.HEALTHY
    triggers_fired: int = 0
    alerts_sent: int = 0
    last_check_ts: float = field(default_factory=time.time)


async def poll_heartbeat(
    config: WatchdogConfig,
    runtime: WatchdogRuntime,
) -> bool:
    """Hit the heartbeat endpoint. Return True on healthy response."""
    if not config.heartbeat_url:
        logger.error("WATCHDOG_HEARTBEAT_URL not set — cannot poll")
        return False
    try:
        import aiohttp  # noqa: PLC0415
    except ImportError:
        logger.error("aiohttp not available in watchdog env")
        return False
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as session,
            session.get(config.heartbeat_url) as resp,
        ):
            body = await resp.text()
            if resp.status == 200:
                runtime.last_heartbeat_ts = time.time()
                runtime.last_heartbeat_body = body
                return True
            logger.warning(
                "heartbeat status=%s body=%s",
                resp.status,
                body[:200],
            )
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat poll failed: %s", exc)
        return False


def classify_state(
    config: WatchdogConfig,
    runtime: WatchdogRuntime,
) -> WatchdogState:
    """Decide the current state based on heartbeat age."""
    if runtime.last_heartbeat_ts == 0.0:
        return WatchdogState.STALE
    age_s = time.time() - runtime.last_heartbeat_ts
    if age_s >= config.heartbeat_timeout_s:
        return WatchdogState.STALE
    if age_s >= config.degraded_threshold_s:
        return WatchdogState.DEGRADED
    return WatchdogState.HEALTHY


async def flatten_kraken_positions(config: WatchdogConfig) -> bool:
    """Call Kraken cancel-all-open-orders + flatten margin positions.

    Uses the reduced-scope WATCHDOG_KRAKEN_* credentials to:
      1. POST /0/private/CancelAll  -- cancel every open order
      2. Iterate OpenPositions; for each, submit an opposite-side
         market order sized to the exact open volume.
      3. Return True if all closes acknowledged by the server.
    """
    if config.dry_run:
        logger.warning("[DRY_RUN] kraken flatten would fire now")
        return True
    if not config.kraken_enabled:
        return False

    key = os.environ.get("WATCHDOG_KRAKEN_KEY", "")
    secret = os.environ.get("WATCHDOG_KRAKEN_SECRET", "")
    if not (key and secret):
        logger.error(
            "flatten_kraken_positions: WATCHDOG_KRAKEN_KEY/SECRET not set",
        )
        return False

    # Lazy import to keep watchdog importable on systems without aiohttp.
    try:
        from eta_engine.venues.base import OrderRequest, OrderType, Side  # noqa: PLC0415
        from eta_engine.venues.kraken import KrakenVenue  # noqa: PLC0415
    except ImportError as exc:
        logger.error("flatten_kraken_positions: import failed: %s", exc)
        return False

    venue = KrakenVenue(api_key=key, api_secret=secret)
    try:
        status, data = await venue._private_post("CancelAll", {})  # noqa: SLF001
        if status != 200 or (data.get("error") or []):
            logger.error(
                "kraken CancelAll http=%s error=%s",
                status,
                data.get("error"),
            )
            # Continue to position flatten anyway — cancel-all is a courtesy
        positions = await venue.get_positions()
        all_ok = True
        for pos in positions:
            qty = abs(float(pos.get("qty") or 0.0))
            if qty <= 0:
                continue
            symbol = pos.get("symbol") or pos.get("pair_native", "")
            close_side = Side.SELL if pos.get("side") == "long" else Side.BUY
            req = OrderRequest(
                symbol=str(symbol),
                side=close_side,
                qty=qty,
                order_type=OrderType.MARKET,
                reduce_only=True,
            )
            result = await venue.place_order(req)
            if result.status.name in {"REJECTED", "EXPIRED"}:
                logger.error(
                    "kraken flatten failed for %s qty=%s: %s",
                    symbol,
                    qty,
                    result.raw,
                )
                all_ok = False
        return all_ok
    except Exception as exc:  # noqa: BLE001
        logger.error("flatten_kraken_positions raised: %s", exc, exc_info=True)
        return False
    finally:
        await venue.close()


async def flatten_ibkr_positions(config: WatchdogConfig) -> bool:
    """Call IBKR Client Portal global-cancel + per-position close.

    Uses the operator's Client Portal Gateway (cookie-authenticated
    session on the watchdog host). Calls:
      1. POST /iserver/account/{acct}/orders/global-cancel
      2. For each open position, POST a closing market order.
    """
    if config.dry_run:
        logger.warning("[DRY_RUN] ibkr flatten would fire now")
        return True
    if not config.ibkr_enabled:
        return False

    acct = os.environ.get("WATCHDOG_IBKR_ACCOUNT_ID", "")
    base_url = os.environ.get(
        "WATCHDOG_IBKR_BASE_URL",
        "https://127.0.0.1:5000/v1/api",
    ).rstrip("/")
    if not acct:
        logger.error("flatten_ibkr_positions: WATCHDOG_IBKR_ACCOUNT_ID not set")
        return False

    try:
        import aiohttp  # noqa: PLC0415
    except ImportError:
        logger.error("flatten_ibkr_positions: aiohttp not available")
        return False

    all_ok = True
    try:
        connector = aiohttp.TCPConnector(ssl=False)  # Client Portal self-signed cert
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=10.0),
        ) as session:
            # 1. Global cancel
            cancel_url = f"{base_url}/iserver/account/{acct}/orders/global-cancel"
            async with session.post(cancel_url) as resp:
                if resp.status >= 400:
                    logger.error(
                        "ibkr global-cancel http=%s body=%s",
                        resp.status,
                        (await resp.text())[:200],
                    )
                    all_ok = False

            # 2. Fetch positions and close each
            pos_url = f"{base_url}/portfolio/{acct}/positions/0"
            async with session.get(pos_url) as resp:
                if resp.status >= 400:
                    logger.error(
                        "ibkr positions http=%s body=%s",
                        resp.status,
                        (await resp.text())[:200],
                    )
                    return False
                positions = await resp.json()

            if not isinstance(positions, list):
                return all_ok

            for pos in positions:
                qty = float(pos.get("position") or 0.0)
                if qty == 0.0:
                    continue
                conid = pos.get("conid")
                if conid is None:
                    continue
                # Opposite side, absolute qty
                order_side = "SELL" if qty > 0 else "BUY"
                payload = {
                    "orders": [
                        {
                            "acctId": acct,
                            "conid": int(conid),
                            "orderType": "MKT",
                            "side": order_side,
                            "quantity": abs(qty),
                            "tif": "DAY",
                        }
                    ],
                }
                order_url = f"{base_url}/iserver/account/{acct}/orders"
                async with session.post(order_url, json=payload) as resp:
                    if resp.status >= 400:
                        logger.error(
                            "ibkr close conid=%s http=%s body=%s",
                            conid,
                            resp.status,
                            (await resp.text())[:200],
                        )
                        all_ok = False
        return all_ok
    except Exception as exc:  # noqa: BLE001
        logger.error("flatten_ibkr_positions raised: %s", exc, exc_info=True)
        return False


async def flatten_hyperliquid_positions(config: WatchdogConfig) -> bool:
    """Sign an on-chain cancel-all + close-all via Hyperliquid L1.

    Disabled by default — only fires if operator explicitly enables HL
    both in the trading config AND the watchdog config.
    """
    if not config.hyperliquid_enabled:
        return False
    if config.dry_run:
        logger.warning("[DRY_RUN] hyperliquid flatten would fire now")
        return True
    logger.error(
        "flatten_hyperliquid_positions STUB CALLED — operator must wire EIP-712 signer before production use",
    )
    return False


async def _send_telegram(token: str, chat_id: str, message: str) -> bool:
    """POST to Telegram Bot API sendMessage. Returns True on HTTP 200."""
    try:
        import aiohttp  # noqa: PLC0415
    except ImportError:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8.0),
            ) as session,
            session.post(
                url,
                json={"chat_id": chat_id, "text": message},
            ) as resp,
        ):
            if resp.status == 200:
                return True
            logger.warning(
                "telegram http=%s body=%s",
                resp.status,
                (await resp.text())[:200],
            )
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram send failed: %s", exc)
        return False


async def _send_twilio_sms(
    sid: str,
    auth: str,
    from_number: str,
    to_number: str,
    message: str,
) -> bool:
    """POST to Twilio Messages API via Basic Auth. Returns True on HTTP 2xx."""
    try:
        import aiohttp  # noqa: PLC0415
    except ImportError:
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8.0),
            ) as session,
            session.post(
                url,
                data={"From": from_number, "To": to_number, "Body": message[:1600]},
                auth=aiohttp.BasicAuth(sid, auth),
            ) as resp,
        ):
            if 200 <= resp.status < 300:
                return True
            logger.warning(
                "twilio http=%s body=%s",
                resp.status,
                (await resp.text())[:200],
            )
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("twilio send failed: %s", exc)
        return False


async def send_alert(
    config: WatchdogConfig,
    runtime: WatchdogRuntime,
    severity: str,
    message: str,
) -> None:
    """Send alerts via Telegram + SMS, gated on env-supplied credentials."""
    runtime.alerts_sent += 1
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    logger.warning("[%s] watchdog_alert %s | %s", severity, ts, message)
    full_message = f"[{severity}] {ts} | {message}"

    if config.telegram_enabled:
        token = os.environ.get("WATCHDOG_TELEGRAM_TOKEN", "")
        chat = os.environ.get("WATCHDOG_TELEGRAM_CHAT_ID", "")
        if token and chat:
            await _send_telegram(token, chat, full_message)

    if config.sms_enabled:
        sid = os.environ.get("WATCHDOG_TWILIO_ACCOUNT_SID", "")
        auth = os.environ.get("WATCHDOG_TWILIO_AUTH_TOKEN", "")
        from_n = os.environ.get("WATCHDOG_TWILIO_FROM", "")
        to_n = os.environ.get("WATCHDOG_SMS_TO", "")
        if sid and auth and from_n and to_n:
            await _send_twilio_sms(sid, auth, from_n, to_n, full_message)


async def execute_trigger(
    config: WatchdogConfig,
    runtime: WatchdogRuntime,
) -> None:
    """Fire all flatten-venue calls concurrently + alert the operator."""
    runtime.triggers_fired += 1
    runtime.state = WatchdogState.TRIGGERED
    await send_alert(
        config,
        runtime,
        severity="CRITICAL",
        message=(f"VPS heartbeat stale >{config.heartbeat_timeout_s}s. Firing flatten on all venues."),
    )
    results = await asyncio.gather(
        flatten_kraken_positions(config),
        flatten_ibkr_positions(config),
        flatten_hyperliquid_positions(config),
        return_exceptions=True,
    )
    success = [bool(r) and not isinstance(r, Exception) for r in results]
    summary = f"flatten results: kraken={success[0]} ibkr={success[1]} hyperliquid={success[2]}"
    await send_alert(config, runtime, severity="CRITICAL", message=summary)
    if not all(success):
        await send_alert(
            config,
            runtime,
            severity="CRITICAL",
            message="AT LEAST ONE VENUE FLATTEN FAILED — MANUAL INTERVENTION",
        )


async def run_forever(
    config: WatchdogConfig | None = None,
) -> None:
    """Main loop. Runs until killed."""
    config = config or WatchdogConfig.from_env()
    runtime = WatchdogRuntime()
    logger.info(
        "watchdog starting url=%s timeout=%ss poll=%ss dry_run=%s",
        config.heartbeat_url,
        config.heartbeat_timeout_s,
        config.poll_interval_s,
        config.dry_run,
    )
    while True:
        await poll_heartbeat(config, runtime)
        new_state = classify_state(config, runtime)
        if new_state != runtime.state:
            logger.info(
                "watchdog state transition %s -> %s",
                runtime.state.value,
                new_state.value,
            )
            if new_state is WatchdogState.DEGRADED:
                await send_alert(
                    config,
                    runtime,
                    severity="WARNING",
                    message="heartbeat degraded (approaching timeout)",
                )
            elif new_state is WatchdogState.STALE and runtime.state != WatchdogState.TRIGGERED:
                await execute_trigger(config, runtime)
            runtime.state = new_state
        runtime.last_check_ts = time.time()
        await asyncio.sleep(config.poll_interval_s)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        logger.info("watchdog stopped by operator")
