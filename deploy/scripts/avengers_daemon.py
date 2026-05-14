"""
Deploy // avengers_daemon
=========================
Long-running daemon for the Avengers fleet dispatcher.

Responsibilities:
  * Hold the Fleet + CostGovernor + UsageTracker + Distiller in memory
    so background tasks can delegate without re-loading state every tick.
  * Serve a simple Unix-socket / HTTP interface for the bot fleet to
    request Claude escalations.
  * Persist state to JSON on graceful shutdown.

Invoked by: deploy/systemd/avengers-fleet.service
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.brain.avengers import (
    AvengersDispatch,
    Fleet,
)
from eta_engine.brain.jarvis_v3.claude_layer.cost_governor import (
    CostGovernor,
)
from eta_engine.brain.jarvis_v3.claude_layer.distillation import Distiller
from eta_engine.brain.jarvis_v3.claude_layer.usage_tracker import (
    UsageTracker,
)
from eta_engine.brain.llm_provider import DeepSeekExecutor
from eta_engine.scripts import workspace_roots

logger = logging.getLogger("avengers_daemon")


DEFAULT_STATE_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR
DEFAULT_LOG_DIR = workspace_roots.ETA_RUNTIME_LOG_DIR


class AvengersDaemon:
    """Minimal supervisor that keeps the Fleet + Governor alive."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.running = True

        # Load or bootstrap persistent components
        self.usage = UsageTracker.load(state_dir / "usage_tracker.json")
        self.distiller = Distiller.load(state_dir / "distiller.json")
        self.governor = CostGovernor(
            usage=self.usage,
            distiller=self.distiller,
        )
        # Fleet with DeepSeek-native executor by default.
        # If DEEPSEEK_API_KEY is missing, the provider fails closed.
        self.fleet = Fleet(executor=DeepSeekExecutor(), deepseek_personas=True)
        self.dispatch = AvengersDispatch(
            governor=self.governor,
            fleet=self.fleet,
        )

    def persist(self) -> None:
        """Save all stateful components to disk."""
        self.usage.save(self.state_dir / "usage_tracker.json")
        self.distiller.save(self.state_dir / "distiller.json")
        logger.info("state persisted to %s", self.state_dir)

    def heartbeat(self) -> dict:
        q = self.usage.quota_state()
        return {
            "ts": datetime.now(UTC).isoformat(),
            "quota_state": q.state.value,
            "hourly_pct": q.hourly_pct,
            "daily_pct": q.daily_pct,
            "cache_hit_rate": q.cache_hit_rate,
            "distiller_version": self.distiller.model.version,
            "distiller_trained": self.distiller.model.train_n > 0,
        }

    def stop(self, signum: int, _frame: object) -> None:
        logger.info("received signal %s -- stopping", signum)
        self.running = False

    def run(self, tick_seconds: float = 30.0) -> int:
        """Main loop. Writes heartbeat every ``tick_seconds``."""
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logger.info("avengers_daemon starting (state=%s)", self.state_dir)
        last_persist = time.monotonic()
        while self.running:
            hb = self.heartbeat()
            (self.state_dir / "avengers_heartbeat.json").write_text(
                json.dumps(hb, indent=2),
                encoding="utf-8",
            )
            # Persist every 5 minutes
            if time.monotonic() - last_persist > 300:
                self.persist()
                last_persist = time.monotonic()
            time.sleep(tick_seconds)
        self.persist()
        logger.info("avengers_daemon stopped cleanly")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--tick", type=float, default=30.0, help="heartbeat tick in seconds")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)
    daemon = AvengersDaemon(state_dir=Path(args.state_dir))
    return daemon.run(tick_seconds=args.tick)


if __name__ == "__main__":
    raise SystemExit(main())
