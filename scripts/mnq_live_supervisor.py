"""
MNQ live supervisor -- minimal MnqBot + JARVIS + IBKR paper loop.
================================================================

Drives an :class:`MnqBot` through a tick stream in paper mode. Mirrors
the structure of :mod:`scripts.live_supervisor` (BTC) but keeps the
surface deliberately thin because the MNQ bot already carries its own
JARVIS gating, session-gate, and kill-switch-latch plumbing -- the
supervisor just composes them.

What this loop does, per tick:

  1. Pull one bar from an injected :class:`_BarSource`.
  2. Hand it to ``bot.on_bar`` (which internally routes through JARVIS
     for ORDER_PLACE gating; denied orders return ``None``).
  3. Write a one-line heartbeat including JARVIS audit counters and
     the latest journal tail.
  4. Persist state each iteration so a crash + restart resumes cleanly.

Out of scope (intentionally):

  * Real tick ingest from a broker market-data WebSocket. The source
    is an injectable :class:`_BarSource` so tests + paper sims can
    hand in static bars or JSONL replay; a live implementation is a
    1-class wrapper around the IBKR md stream.
  * EoD flatten orchestration -- MnqBot.start/stop already handles
    that via the session-gate it's given.
  * Multi-bot fleet. One MnqBot per process; a fleet manager is a
    future layer on top.

The supervisor is safe-by-default: without an explicit router the
MnqBot runs in log-only mode (no venue calls), and without a JARVIS
instance it runs in legacy-unchecked mode. Production VPS wiring
supplies both.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from eta_engine.bots.mnq.bot import MnqBot  # noqa: E402
from eta_engine.brain.jarvis_admin import JarvisAdmin  # noqa: E402
from eta_engine.obs.decision_journal import DecisionJournal  # noqa: E402
from eta_engine.venues.ibkr import (  # noqa: E402
    IbkrClientPortalConfig,
    IbkrClientPortalVenue,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from eta_engine.brain.jarvis_context import JarvisContext
    from eta_engine.venues.base import (
        OrderRequest,
        OrderResult,
    )


logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR = ROOT / "docs" / "mnq_live"
DEFAULT_HEARTBEAT_NAME = "mnq_live"
DEFAULT_POLL_INTERVAL_S = 1.0


class _BarSource(Protocol):
    """Minimal async iterator protocol for tick/bar sources."""

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]: ...


class _Router(Protocol):
    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult: ...


class _IbkrRouterAdapter:
    """Adapt IbkrClientPortalVenue to the _Router protocol MnqBot expects."""

    def __init__(self, venue: IbkrClientPortalVenue) -> None:
        self._venue = venue

    async def place_with_failover(
        self,
        req: OrderRequest,
        *,
        urgency: str = "normal",
    ) -> OrderResult:
        _ = urgency  # IBKR has no urgency knob at this level; ignore.
        return await self._venue.place_order(req)


@dataclass
class MnqSupervisorState:
    """Persistent supervisor state written to disk each tick."""

    started_at_utc: str = ""
    heartbeat_count: int = 0
    bars_consumed: int = 0
    signals_routed: int = 0
    signals_blocked: int = 0
    last_heartbeat_utc: str = ""
    last_bar_ts: str = ""
    last_event: str = ""
    jarvis_audit_tail_len: int = 0
    router_name: str = ""
    paused: bool = False
    notes: list[str] = field(default_factory=list)


class MnqLiveSupervisor:
    """Drive an MnqBot through a bar source with JARVIS + IBKR wired in."""

    def __init__(
        self,
        *,
        bot: MnqBot,
        bar_source: _BarSource,
        out_dir: Path = DEFAULT_OUT_DIR,
        journal: DecisionJournal | None = None,
        jarvis: JarvisAdmin | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bot = bot
        self.bar_source = bar_source
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.journal = journal if journal is not None else DecisionJournal(self.out_dir / "mnq_live_decisions.jsonl")
        self.jarvis = jarvis
        self.clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self.state = MnqSupervisorState()
        self._state_file = self.out_dir / "mnq_live_state.json"

    async def start(self) -> None:
        await self.bot.start()
        self.state.started_at_utc = self.clock().isoformat()
        self.state.paused = self.bot.state.is_paused
        self.state.router_name = (
            type(self.bot._router).__name__ if getattr(self.bot, "_router", None) is not None else "log_only"
        )
        self._persist_state()

    async def stop(self) -> None:
        await self.bot.stop()
        self.state.last_event = "supervisor_stopped"
        self._persist_state()

    async def run_one_bar(self) -> dict[str, Any]:
        """Consume a single bar from the source and return a heartbeat dict.

        Returns ``{}`` if the source is exhausted.
        """
        source_iter = self.bar_source.__aiter__()
        try:
            bar = await source_iter.__anext__()
        except StopAsyncIteration:
            self.state.last_event = "bar_source_exhausted"
            self._persist_state()
            return {}
        return await self._consume(bar)

    async def run_n_bars(self, n: int) -> list[dict[str, Any]]:
        """Consume up to N bars. Stops early on source exhaustion."""
        snapshots: list[dict[str, Any]] = []
        source_iter = self.bar_source.__aiter__()
        for _ in range(n):
            try:
                bar = await source_iter.__anext__()
            except StopAsyncIteration:
                break
            snapshots.append(await self._consume(bar))
        return snapshots

    async def _consume(self, bar: dict[str, Any]) -> dict[str, Any]:
        prev_blocked = self._blocked_count()
        prev_routed = self._routed_count()
        try:
            await self.bot.on_bar(bar)
        except Exception as exc:  # noqa: BLE001 -- never crash the supervisor
            self.state.last_event = f"on_bar_error:{type(exc).__name__}"
            logger.warning("mnq supervisor on_bar raised %s: %s", type(exc).__name__, exc)
        self.state.heartbeat_count += 1
        self.state.bars_consumed += 1
        self.state.last_heartbeat_utc = self.clock().isoformat()
        bar_ts = bar.get("ts") or bar.get("timestamp") or ""
        self.state.last_bar_ts = str(bar_ts)
        self.state.paused = self.bot.state.is_paused
        # Derive counter deltas from the journal.
        self.state.signals_routed += max(0, self._routed_count() - prev_routed)
        self.state.signals_blocked += max(0, self._blocked_count() - prev_blocked)
        if self.jarvis is not None:
            self.state.jarvis_audit_tail_len = len(
                self.jarvis.audit_tail(n=500),
            )
        self._persist_state()
        return self._snapshot()

    def _routed_count(self) -> int:
        from eta_engine.obs.decision_journal import Outcome

        return sum(
            1 for ev in self.journal.read_all() if ev.intent == "mnq_order_routed" and ev.outcome == Outcome.EXECUTED
        )

    def _blocked_count(self) -> int:
        from eta_engine.obs.decision_journal import Outcome

        return sum(
            1 for ev in self.journal.read_all() if ev.intent == "mnq_order_blocked" and ev.outcome == Outcome.BLOCKED
        )

    def _snapshot(self) -> dict[str, Any]:
        return {
            "worker": DEFAULT_HEARTBEAT_NAME,
            "started_at_utc": self.state.started_at_utc,
            "heartbeat_count": self.state.heartbeat_count,
            "bars_consumed": self.state.bars_consumed,
            "signals_routed": self.state.signals_routed,
            "signals_blocked": self.state.signals_blocked,
            "last_bar_ts": self.state.last_bar_ts,
            "last_heartbeat_utc": self.state.last_heartbeat_utc,
            "last_event": self.state.last_event,
            "jarvis_audit_tail_len": self.state.jarvis_audit_tail_len,
            "router_name": self.state.router_name,
            "paused": self.state.paused,
            "symbol": self.bot.config.symbol,
            "tradovate_symbol": getattr(self.bot, "_tradovate_symbol", None),
        }

    def _persist_state(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps(self._snapshot(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("mnq supervisor state persist failed: %s", exc)


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def build_ibkr_router() -> _IbkrRouterAdapter | None:
    """Build the IBKR Client Portal router adapter from env, if credentials
    are present. Returns ``None`` otherwise so the supervisor falls back
    to log-only routing.
    """
    config = IbkrClientPortalConfig.from_env()
    if config.missing_requirements():
        return None
    return _IbkrRouterAdapter(IbkrClientPortalVenue(config))


def build_supervisor_from_env(
    bar_source: _BarSource,
    *,
    out_dir: Path = DEFAULT_OUT_DIR,
    tradovate_symbol: str | None = None,
    provide_ctx: Callable[[], JarvisContext] | None = None,
) -> MnqLiveSupervisor:
    """Compose the supervisor from operator env: IBKR router + JarvisAdmin.

    The bot is always instantiated with a :class:`JarvisAdmin`, so every
    order routes through JARVIS. The IBKR router is only attached when
    ``IBKR_ACCOUNT_ID`` (or ``IBKR_ACCOUNT_ID_FILE``) is present and
    the readiness check passes; otherwise the bot runs in log-only
    mode.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    journal = DecisionJournal(out_dir / "mnq_live_decisions.jsonl")
    jarvis = JarvisAdmin(audit_path=out_dir / "mnq_live_jarvis_audit.jsonl")
    router = build_ibkr_router()
    bot = MnqBot(
        jarvis=jarvis,
        journal=journal,
        provide_ctx=provide_ctx,
        router=router,
        tradovate_symbol=tradovate_symbol,
    )
    return MnqLiveSupervisor(
        bot=bot,
        bar_source=bar_source,
        out_dir=out_dir,
        journal=journal,
        jarvis=jarvis,
    )


# ---------------------------------------------------------------------------
# Simple JSONL bar source for replay / smoke tests
# ---------------------------------------------------------------------------


class JsonlBarSource:
    """Read bars from a JSONL file, one object per line."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive an MnqBot through a bar stream in paper mode.",
    )
    parser.add_argument(
        "--bars",
        type=Path,
        required=True,
        help="Path to a JSONL bar file (one bar object per line).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Directory for journal + heartbeat state.",
    )
    parser.add_argument(
        "--tradovate-symbol",
        default=None,
        help="Tradovate contract symbol override (e.g. MNQH6).",
    )
    parser.add_argument(
        "--max-bars",
        type=int,
        default=0,
        help="Stop after this many bars (0 = consume until exhausted).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    source = JsonlBarSource(args.bars)
    sup = build_supervisor_from_env(
        source,
        out_dir=args.out_dir,
        tradovate_symbol=args.tradovate_symbol,
    )

    async def _run() -> None:
        await sup.start()
        if args.max_bars > 0:
            await sup.run_n_bars(args.max_bars)
        else:
            while True:
                snap = await sup.run_one_bar()
                if not snap:
                    break
        await sup.stop()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
