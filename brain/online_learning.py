"""Online learning hook for bots (Tier-4 #13, 2026-04-27).

Production-safe per-bot learner for updating setup priors as fills
settle. BaseBot already calls this hook from ``record_fill_outcome``;
this module owns the reusable policy for observing realized R,
persisting learner state, and deriving a conservative sizing modifier.

Pattern::

    from eta_engine.brain.online_learning import OnlineUpdater

    class MyBot(BaseBot):
        def __init__(self, ..., online_updater: OnlineUpdater | None = None):
            ...
            self._online = online_updater or OnlineUpdater(bot_name=self.config.name)

        def on_fill(self, fill: Fill, *, intent: ActionType, confluence: float) -> None:
            super().on_fill(fill)
            # Feed the realized P&L of the trade back to the updater.
            r_multiple = self._compute_r_multiple(fill)
            self._online.observe(
                feature_bucket=f"confluence_{int(confluence)}",
                r_multiple=r_multiple,
            )
            # Subsequent pre-flight/sizing reads the updated priors via
            # online_updater.sizing_multiplier(feature_bucket).

This is intentionally a thin EWMA tracker, not a full RL pipeline. The
goal is to capture "are setups in confluence-bucket-7 still working
as well as they did in walk-forward?" -- a slow regime-shift detector
keyed off realized R-multiples. Default behavior is fail-safe: the
learner can shrink size on cold buckets, but will not expand above
the upstream JARVIS cap unless the caller opts in.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _BucketStats:
    n: int = 0
    ewma_r: float = 0.0
    ewma_alpha: float = 0.10  # how fast to forget old samples (~10 trades half-life)


@dataclass(frozen=True)
class OnlineSizingDecision:
    """Sizing decision derived from a bucket's live EWMA."""

    feature_bucket: str
    multiplier: float
    expected_r: float
    samples: int
    status: str
    reason: str


class OnlineUpdater:
    """Per-bot online tracker for realized R-multiples by feature bucket.

    Backward-compatible: when not used, the bot behaves exactly as before.
    Forward-compatible: when wired, the bot's sizing/throttle logic can
    multiply size by ``expected_r(bucket)`` to lean into hot buckets and
    fade cold ones.
    """

    def __init__(self, *, bot_name: str = "unknown", alpha: float = 0.10) -> None:
        self.bot_name = bot_name
        self._buckets: dict[str, _BucketStats] = {}
        self.alpha = alpha

    def observe(self, *, feature_bucket: str, r_multiple: float) -> None:
        """Record a realized R-multiple for a (bucket) and update the EWMA.

        ``r_multiple`` should be expressed as a multiple of risk:
        ``+1.0`` = winner of size = the configured stop distance,
        ``-1.0`` = loser stopped at the stop, ``0.0`` = scratch.
        """
        b = self._buckets.setdefault(feature_bucket, _BucketStats(ewma_alpha=self.alpha))
        b.n += 1
        if b.n == 1:
            b.ewma_r = r_multiple
        else:
            b.ewma_r = (b.ewma_alpha * r_multiple) + ((1.0 - b.ewma_alpha) * b.ewma_r)

    def expected_r(self, feature_bucket: str) -> float:
        """Return the current EWMA R-multiple for a bucket, or 0.0 if unseen."""
        b = self._buckets.get(feature_bucket)
        return b.ewma_r if b else 0.0

    def confidence(self, feature_bucket: str) -> int:
        """Sample count for the bucket (proxy for confidence)."""
        b = self._buckets.get(feature_bucket)
        return b.n if b else 0

    def snapshot(self) -> Mapping[str, dict[str, float]]:
        return {
            bucket: {"n": float(s.n), "ewma_r": round(s.ewma_r, 4), "alpha": s.ewma_alpha}
            for bucket, s in self._buckets.items()
        }

    def sizing_decision(
        self,
        feature_bucket: str,
        *,
        min_samples: int = 5,
        cold_threshold_r: float = -0.25,
        hot_threshold_r: float = 0.35,
        cold_multiplier: float = 0.50,
        hot_multiplier: float = 1.10,
        allow_expansion: bool = False,
    ) -> OnlineSizingDecision:
        """Return a conservative sizing modifier for a feature bucket.

        The default is deliberately asymmetric: cold buckets can reduce
        exposure after enough samples, while hot buckets only annotate
        status unless ``allow_expansion`` is true. That keeps the learner
        useful in live/paper flows without letting a small sample of wins
        bypass JARVIS, correlation, or broker risk caps.
        """
        samples = self.confidence(feature_bucket)
        expected = self.expected_r(feature_bucket)
        if samples < min_samples:
            return OnlineSizingDecision(
                feature_bucket=feature_bucket,
                multiplier=1.0,
                expected_r=round(expected, 4),
                samples=samples,
                status="warming",
                reason=f"need {min_samples} samples before online sizing",
            )
        if expected <= cold_threshold_r:
            return OnlineSizingDecision(
                feature_bucket=feature_bucket,
                multiplier=max(0.0, min(1.0, cold_multiplier)),
                expected_r=round(expected, 4),
                samples=samples,
                status="cold",
                reason=f"bucket EWMA {expected:+.2f}R <= {cold_threshold_r:+.2f}R",
            )
        if expected >= hot_threshold_r:
            multiplier = hot_multiplier if allow_expansion else 1.0
            return OnlineSizingDecision(
                feature_bucket=feature_bucket,
                multiplier=round(max(0.0, multiplier), 4),
                expected_r=round(expected, 4),
                samples=samples,
                status="hot",
                reason=f"bucket EWMA {expected:+.2f}R >= {hot_threshold_r:+.2f}R",
            )
        return OnlineSizingDecision(
            feature_bucket=feature_bucket,
            multiplier=1.0,
            expected_r=round(expected, 4),
            samples=samples,
            status="stable",
            reason="bucket inside neutral EWMA band",
        )

    def sizing_multiplier(self, feature_bucket: str, **kwargs: object) -> float:
        """Convenience wrapper returning only the sizing multiplier."""
        return self.sizing_decision(feature_bucket, **kwargs).multiplier

    def health_summary(self) -> dict[str, object]:
        """Fleet/status payload for Command Center and pre-flight logs."""
        cold = []
        hot = []
        warming = []
        for bucket in sorted(self._buckets):
            decision = self.sizing_decision(bucket)
            row = {
                "bucket": bucket,
                "expected_r": decision.expected_r,
                "samples": decision.samples,
                "multiplier": decision.multiplier,
                "status": decision.status,
            }
            if decision.status == "cold":
                cold.append(row)
            elif decision.status == "hot":
                hot.append(row)
            else:
                warming.append(row)
        status = "cold" if cold else "hot" if hot else "stable"
        return {
            "bot_name": self.bot_name,
            "status": status,
            "n_buckets": len(self._buckets),
            "cold_buckets": cold,
            "hot_buckets": hot,
            "other_buckets": warming,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "bot_name": self.bot_name,
            "alpha": self.alpha,
            "buckets": {
                bucket: {"n": s.n, "ewma_r": s.ewma_r, "ewma_alpha": s.ewma_alpha}
                for bucket, s in self._buckets.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OnlineUpdater:
        updater = cls(
            bot_name=str(data.get("bot_name") or "unknown"),
            alpha=float(data.get("alpha", 0.10)),
        )
        raw_buckets = data.get("buckets") or {}
        if isinstance(raw_buckets, dict):
            for bucket, raw in raw_buckets.items():
                if not isinstance(raw, dict):
                    continue
                updater._buckets[str(bucket)] = _BucketStats(
                    n=int(raw.get("n", 0)),
                    ewma_r=float(raw.get("ewma_r", 0.0)),
                    ewma_alpha=float(raw.get("ewma_alpha", updater.alpha)),
                )
        return updater

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, *, bot_name: str = "unknown", alpha: float = 0.10) -> OnlineUpdater:
        if not path.exists():
            return cls(bot_name=bot_name, alpha=alpha)
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("OnlineUpdater.load failed for %s: %s", path, exc)
            return cls(bot_name=bot_name, alpha=alpha)
