"""Single global rate-limiter for alerts (Tier-3 #10, 2026-04-27).

Multiple alerters were each running their own cooldown logic, which meant
during a regime shift the operator could get 3 different pages from 3
different alerters within 30 seconds. This module is the shared state
that all alerters check before firing.

Backed by a JSON file under ``state/global_rate_limit.json`` so it
survives process restarts. Token-bucket style: each (event_class, level)
has a token capacity that refills over time. Fire only consumes a token
when the bucket has one.

Usage::

    from eta_engine.obs.global_rate_limiter import GlobalRateLimiter

    rl = GlobalRateLimiter()
    if rl.allow(event_class="jarvis", level="warn"):
        dispatcher.send(event, payload)
        rl.commit()  # only on successful send

    # Or, simpler one-shot:
    if rl.try_consume(event_class="jarvis", level="warn"):
        dispatcher.send(event, payload)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "state" / "global_rate_limit.json"


@dataclass
class _Bucket:
    capacity: int
    refill_per_min: float
    tokens: float = 0.0
    last_refill_ts: float = field(default_factory=time.time)


# Default bucket sizes per level. Critical alerts are uncapped (capacity=999)
# so kill-switch/etc. always fire. Warn is moderate. Info is heavily capped
# to prevent nominal-day pager fatigue.
DEFAULT_BUCKETS: dict[str, dict[str, float]] = {
    "critical": {"capacity": 999, "refill_per_min": 999.0},
    "warn": {"capacity": 5, "refill_per_min": 1.0},
    "info": {"capacity": 10, "refill_per_min": 0.5},
}


class GlobalRateLimiter:
    """Single source of truth for alert rate-limit decisions.

    Thread-safe via ``self._lock``. State persisted to a JSON file; if
    the file is missing or corrupted, falls back to fresh buckets.
    """

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        buckets: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        cfg = buckets or DEFAULT_BUCKETS
        for level, params in cfg.items():
            self._buckets[level] = _Bucket(
                capacity=int(params["capacity"]),
                refill_per_min=float(params["refill_per_min"]),
                tokens=float(params["capacity"]),  # start full
            )
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for level, snap in data.get("buckets", {}).items():
                if level in self._buckets:
                    self._buckets[level].tokens = float(snap.get("tokens", self._buckets[level].capacity))
                    self._buckets[level].last_refill_ts = float(snap.get("last_refill_ts", time.time()))
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            logger.warning("rate-limit state load failed (%s); using fresh buckets", exc)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_path.write_text(
                json.dumps(
                    {
                        "buckets": {
                            level: {"tokens": b.tokens, "last_refill_ts": b.last_refill_ts}
                            for level, b in self._buckets.items()
                        },
                        "saved_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("rate-limit state save failed (%s); buckets will reset on restart", exc)

    def _refill(self, b: _Bucket, *, now: float) -> None:
        elapsed_min = (now - b.last_refill_ts) / 60.0
        if elapsed_min <= 0:
            return
        added = elapsed_min * b.refill_per_min
        b.tokens = min(b.capacity, b.tokens + added)
        b.last_refill_ts = now

    def try_consume(self, *, event_class: str, level: str) -> bool:
        """Atomically check + consume a token. Returns True if allowed.

        ``event_class`` is currently informational (logged) but reserved
        for per-class buckets in a future iteration.
        """
        _ = event_class  # reserved
        with self._lock:
            level_norm = level.lower()
            if level_norm not in self._buckets:
                # Unknown level => default to "warn" semantics
                level_norm = "warn"
            b = self._buckets[level_norm]
            now = time.time()
            self._refill(b, now=now)
            if b.tokens < 1.0:
                self._save_state()
                return False
            b.tokens -= 1.0
            self._save_state()
            return True

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Read-only view of current bucket state. Diagnostic use."""
        with self._lock:
            return {
                level: {
                    "tokens": round(b.tokens, 3),
                    "capacity": float(b.capacity),
                    "refill_per_min": b.refill_per_min,
                }
                for level, b in self._buckets.items()
            }


# Module-level singleton for convenience.
_default: GlobalRateLimiter | None = None


def default_limiter() -> GlobalRateLimiter:
    global _default
    if _default is None:
        _default = GlobalRateLimiter()
    return _default
