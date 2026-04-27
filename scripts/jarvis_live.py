"""
EVOLUTIONARY TRADING ALGO  //  scripts.jarvis_live
======================================
Long-running daemon that keeps Jarvis ticking LIVE with supervision.

Why this exists
---------------
``brain.jarvis_admin`` makes Jarvis the admin of the fleet: every
subsystem calls ``request_approval()``. If Jarvis stops ticking, every
gate silently falls through to stale policy. ``daily_premarket.py``
produces a ONE-SHOT snapshot; this daemon keeps Jarvis LIVE and
watched end-to-end.

Responsibilities
----------------
  * Build a ``JarvisContextBuilder`` from simple file-based providers
    reading ``docs/premarket_inputs.json`` (hot-reloadable -- operator
    can overwrite the file and the next tick picks it up).
  * Wrap in ``JarvisContextEngine`` and run through ``JarvisSupervisor``
    so staleness / dominance / flatline / invalid are all caught.
  * Fan out health alerts via Telegram / Discord / Slack when env is
    set (``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` /
    ``DISCORD_WEBHOOK_URL`` / ``SLACK_WEBHOOK_URL``). No env -> no alerts,
    dry-run mode.
  * Emit per-tick health reports to:
      - ``docs/jarvis_live_health.json``    (latest only)
      - ``docs/jarvis_live_log.jsonl``      (append-only history)

Usage
-----
    # Foreground; Ctrl-C to stop.
    python -m eta_engine.scripts.jarvis_live

    # Faster cadence, bounded ticks (smoke test):
    python -m eta_engine.scripts.jarvis_live --interval 5 --max-ticks 3

    # Explicit inputs file:
    python -m eta_engine.scripts.jarvis_live \\
        --inputs docs/premarket_inputs.json \\
        --out-dir docs/ \\
        --interval 60

Design
------
All I/O is injected for testing. ``run_live()`` takes explicit
providers / alerter / paths, so tests wire stubs and bound the loop
with ``max_ticks``. The module-level ``main()`` is just a CLI shim.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.brain.jarvis_context import (  # noqa: E402
    EquitySnapshot,
    JarvisContextBuilder,
    JarvisContextEngine,
    JarvisMemory,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
)
from eta_engine.obs.jarvis_supervisor import (  # noqa: E402
    JarvisHealthReport,
    JarvisSupervisor,
    SupervisorPolicy,
)

if TYPE_CHECKING:
    from eta_engine.obs.alerts import MultiAlerter

logger = logging.getLogger(__name__)

DEFAULT_INPUTS = ROOT / "docs" / "premarket_inputs.json"
DEFAULT_OUT_DIR = ROOT / "docs"


# ---------------------------------------------------------------------------
# File-based providers (hot-reload on every call)
# ---------------------------------------------------------------------------


@dataclass
class _FileInputs:
    """Single source of truth parsed once per tick. Attribute access via
    providers below."""

    macro: MacroSnapshot
    equity: EquitySnapshot
    regime: RegimeSnapshot
    journal: JournalSnapshot


def _neutral_inputs() -> _FileInputs:
    return _FileInputs(
        macro=MacroSnapshot(vix_level=None, macro_bias="neutral"),
        equity=EquitySnapshot(
            account_equity=0.0,
            daily_pnl=0.0,
            daily_drawdown_pct=0.0,
            open_positions=0,
            open_risk_r=0.0,
        ),
        regime=RegimeSnapshot(regime="UNKNOWN", confidence=0.5),
        journal=JournalSnapshot(),
    )


def _load_inputs_file(path: Path) -> _FileInputs:
    if not path.exists():
        return _neutral_inputs()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("failed to parse %s; using neutral inputs", path)
        return _neutral_inputs()
    try:
        return _FileInputs(
            macro=MacroSnapshot(**raw.get("macro", {})),
            equity=EquitySnapshot(**raw["equity"]),
            regime=RegimeSnapshot(**raw["regime"]),
            journal=JournalSnapshot(**raw.get("journal", {})),
        )
    except Exception:
        logger.exception("invalid schema in %s; using neutral inputs", path)
        return _neutral_inputs()


class _FileBackedProviders:
    """Single object exposing the four provider methods by re-reading the
    inputs file each tick. Matches all four Protocols via duck typing.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # MacroProvider
    def get_macro(self) -> MacroSnapshot:
        return _load_inputs_file(self._path).macro

    # EquityProvider
    def get_equity(self) -> EquitySnapshot:
        return _load_inputs_file(self._path).equity

    # RegimeProvider
    def get_regime(self) -> RegimeSnapshot:
        return _load_inputs_file(self._path).regime

    # JournalProvider
    def get_journal_snapshot(self) -> JournalSnapshot:
        return _load_inputs_file(self._path).journal


# ---------------------------------------------------------------------------
# Output sinks
# ---------------------------------------------------------------------------


def _write_health(report: JarvisHealthReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "jarvis_live_health.json"
    log = out_dir / "jarvis_live_log.jsonl"
    payload = report.model_dump(mode="json")
    latest.write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")


# ---------------------------------------------------------------------------
# Alerter factory from environment
# ---------------------------------------------------------------------------


def build_alerter_from_env() -> MultiAlerter | None:
    """Inspect env for webhook/token config and construct a MultiAlerter.

    Returns None if no transport is configured (dry-run mode).
    """
    from eta_engine.obs.alerts import (  # noqa: PLC0415
        DiscordAlerter,
        MultiAlerter,
        SlackAlerter,
        TelegramAlerter,
    )

    alerters = []
    tg_tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_tok and tg_chat:
        alerters.append(TelegramAlerter(bot_token=tg_tok, chat_id=tg_chat))
    disc = os.environ.get("DISCORD_WEBHOOK_URL")
    if disc:
        alerters.append(DiscordAlerter(webhook_url=disc))
    slack = os.environ.get("SLACK_WEBHOOK_URL")
    if slack:
        alerters.append(SlackAlerter(webhook_url=slack))
    if not alerters:
        return None
    return MultiAlerter(alerters)


# ---------------------------------------------------------------------------
# run_live: the daemon body (testable)
# ---------------------------------------------------------------------------


async def run_live(
    *,
    supervisor: JarvisSupervisor,
    alerter: MultiAlerter | None,
    out_dir: Path = DEFAULT_OUT_DIR,
    interval_s: float = 60.0,
    max_ticks: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> list[JarvisHealthReport]:
    """Run the supervised tick loop until ``stop_event`` fires or
    ``max_ticks`` elapses. Returns all health reports recorded.

    This is a lower-level primitive than ``JarvisSupervisor.run``:
    we need per-tick output to ``docs/jarvis_live_*`` and an
    externally-triggered stop (for signal handlers).
    """
    if interval_s <= 0.0:
        raise ValueError("interval_s must be > 0")
    stop_event = stop_event or asyncio.Event()
    reports: list[JarvisHealthReport] = []
    i = 0
    try:
        while not stop_event.is_set() and (max_ticks is None or i < max_ticks):
            try:
                supervisor.tick()
            except Exception:
                logger.exception("supervisor.tick() raised (continuing)")
            report = supervisor.snapshot_health()
            reports.append(report)
            try:
                _write_health(report, out_dir)
            except Exception:
                logger.exception("failed writing jarvis_live health outputs")
            if report.degraded:
                try:
                    await supervisor.alert(alerter, report)
                except Exception:
                    logger.exception("supervisor.alert failed")
            i += 1
            if max_ticks is not None and i >= max_ticks:
                break
            # Sleep cancellable by stop_event.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=interval_s,
                )
    finally:
        if alerter is not None:
            try:
                await alerter.close()
            except Exception:
                logger.exception("alerter.close raised")
    return reports


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_default_supervisor(inputs_path: Path) -> JarvisSupervisor:
    providers = _FileBackedProviders(inputs_path)
    builder = JarvisContextBuilder(
        macro_provider=providers,
        equity_provider=providers,
        regime_provider=providers,
        journal_provider=providers,
    )
    engine = JarvisContextEngine(builder=builder, memory=JarvisMemory(maxlen=64))
    return JarvisSupervisor(engine=engine, policy=SupervisorPolicy())


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGINT / SIGTERM to stop_event. Best-effort on Windows where
    the loop's add_signal_handler is not supported.
    """
    loop = asyncio.get_event_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        # Windows / already-set handler fallback: rely on KeyboardInterrupt.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


async def _async_main(
    *,
    inputs_path: Path,
    out_dir: Path,
    interval_s: float,
    max_ticks: int | None,
) -> int:
    supervisor = _build_default_supervisor(inputs_path)
    alerter = build_alerter_from_env()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    logger.info(
        "jarvis_live starting: inputs=%s interval=%.1fs alerter=%s max_ticks=%s",
        inputs_path,
        interval_s,
        "on" if alerter is not None else "dry-run",
        max_ticks,
    )
    try:
        reports = await run_live(
            supervisor=supervisor,
            alerter=alerter,
            out_dir=out_dir,
            interval_s=interval_s,
            max_ticks=max_ticks,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        stop_event.set()
        reports = []
    logger.info("jarvis_live stopped after %d reports", len(reports))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EVOLUTIONARY TRADING ALGO Jarvis live supervisor daemon",
    )
    parser.add_argument(
        "--inputs", type=Path, default=DEFAULT_INPUTS, help="Path to premarket_inputs.json (hot-reloaded)"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Directory for jarvis_live_health.json + log"
    )
    parser.add_argument("--interval", type=float, default=60.0, help="Tick interval in seconds (default 60)")
    parser.add_argument("--max-ticks", type=int, default=None, help="Stop after N ticks (default: run forever)")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    sys.stdout.write(
        f"[{datetime.now(UTC).isoformat()}] jarvis_live starting\n",
    )
    return asyncio.run(
        _async_main(
            inputs_path=args.inputs,
            out_dir=args.out_dir,
            interval_s=args.interval,
            max_ticks=args.max_ticks,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
