"""Trade thesis tracker (Wave-13, 2026-04-27).

Every approved trade gets a written THESIS:
  * Why we're entering (the bullish narrative we're betting on)
  * Specific INVALIDATION RULES (concrete price/regime/time
    conditions under which the thesis is broken)

The runtime then watches each open trade and tells the bot to EXIT
EARLY when an invalidation rule trips -- BEFORE the stop hits.
This is the difference between "stop-loss exit at -1R" (mechanical
risk control) and "thesis-broken exit at -0.3R" (intelligent risk
control that compounds via better R-on-losers).

Invalidation rules supported (extensible):
  * regime_changed_to: bot's regime classifier flips to specified
  * price_breaks: price closes beyond a level
  * correlation_flips: ES1-MNQ correlation drops below threshold
  * time_in_position: trade exceeds duration without progress
  * stress_above: stress spikes beyond ceiling
  * macro_event_within: FOMC/NFP/CPI hits inside the window

Use case (paired with intelligence layer):

    from eta_engine.brain.jarvis_v3.thesis_tracker import (
        ThesisTracker, ThesisInvalidationRule,
    )

    tracker = ThesisTracker.default()
    tracker.open_thesis(
        signal_id="cascade_hunter_2026-04-27T15:32",
        narrative="EMA stack aligned with bullish_low_vol regime",
        invalidation_rules=[
            ThesisInvalidationRule(
                kind="regime_changed_to",
                params={"to": "bearish_high_vol"},
                description="regime flips bearish high-vol",
            ),
            ThesisInvalidationRule(
                kind="price_breaks",
                params={"level": 21420.0, "direction": "below"},
                description="price closes < 21420",
            ),
        ],
        opened_at_price=21450.0,
    )

    # Per-tick:
    breach = tracker.check(
        signal_id="cascade_hunter_...",
        current_state={"regime": "bullish_low_vol", "price": 21438.0},
    )
    if breach is not None:
        # Exit immediately
        bot.exit_position(reason=f"thesis broken: {breach.description}")
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_THESES_PATH = ROOT / "state" / "jarvis_intel" / "open_theses.json"
DEFAULT_BREACH_LOG = ROOT / "state" / "jarvis_intel" / "thesis_breaches.jsonl"


@dataclass
class ThesisInvalidationRule:
    """One concrete condition that breaks the thesis."""

    kind: str  # "regime_changed_to" / "price_breaks" / etc.
    params: dict
    description: str = ""


@dataclass
class TradeThesis:
    """The written thesis bound to one open trade."""

    signal_id: str
    bot_id: str
    direction: str
    narrative: str
    invalidation_rules: list[ThesisInvalidationRule]
    opened_at: str
    opened_at_price: float
    initial_regime: str = ""
    initial_stress: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class ThesisBreach:
    """Audit record of a thesis breach."""

    signal_id: str
    ts: str
    rule_kind: str
    rule_description: str
    current_state: dict


# ─── Rule evaluation ─────────────────────────────────────────────


def _evaluate_rule(
    rule: ThesisInvalidationRule,
    thesis: TradeThesis,
    current_state: dict,
) -> bool:
    """Return True iff the rule has been TRIPPED by the current state."""
    kind = rule.kind
    p = rule.params

    if kind == "regime_changed_to":
        target = str(p.get("to", ""))
        return str(current_state.get("regime", "")) == target

    if kind == "regime_left":
        # Trips if current regime is anything OTHER than the initial one
        return current_state.get("regime", "") != thesis.initial_regime

    if kind == "price_breaks":
        level = float(p.get("level", 0.0))
        direction = str(p.get("direction", "below"))
        cur_price = float(current_state.get("price", thesis.opened_at_price))
        return cur_price < level if direction == "below" else cur_price > level

    if kind == "stress_above":
        ceiling = float(p.get("ceiling", 0.7))
        return float(current_state.get("stress", 0.0)) > ceiling

    if kind == "correlation_flips":
        threshold = float(p.get("threshold", 0.3))
        cur_corr = float(current_state.get("correlation", 1.0))
        return cur_corr < threshold

    if kind == "time_in_position":
        max_minutes = float(p.get("max_minutes", 240))
        opened = datetime.fromisoformat(thesis.opened_at.replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        elapsed_min = (datetime.now(UTC) - opened).total_seconds() / 60.0
        return elapsed_min > max_minutes

    if kind == "macro_event_within":
        max_minutes = float(p.get("within_minutes", 30))
        try:
            from eta_engine.brain.jarvis_v3.macro_calendar import (
                DEFAULT_2026_USA_EVENTS,
                is_within_event_window,
            )

            now = datetime.now(UTC)
            event = is_within_event_window(
                now,
                events=DEFAULT_2026_USA_EVENTS,
                window_min_override=int(max_minutes),
            )
            return event is not None
        except Exception as exc:  # noqa: BLE001
            logger.debug("thesis_tracker: macro_event check failed (%s)", exc)
            return False

    logger.debug("thesis_tracker: unknown rule kind '%s'", kind)
    return False


# ─── Tracker ──────────────────────────────────────────────────────


class ThesisTracker:
    """Manages the open-theses dictionary + per-tick breach checks.

    State persists to ``state/jarvis_intel/open_theses.json`` so
    daemon restarts don't lose theses bound to still-open positions.
    Breach events are appended to ``thesis_breaches.jsonl``.
    """

    def __init__(
        self,
        *,
        theses_path: Path = DEFAULT_THESES_PATH,
        breach_log_path: Path = DEFAULT_BREACH_LOG,
    ) -> None:
        self.theses_path = theses_path
        self.breach_log_path = breach_log_path
        self._open: dict[str, TradeThesis] = {}
        self._load()

    @classmethod
    def default(cls) -> ThesisTracker:
        return cls()

    def open_thesis(
        self,
        *,
        signal_id: str,
        bot_id: str = "",
        direction: str = "long",
        narrative: str = "",
        invalidation_rules: list[ThesisInvalidationRule],
        opened_at_price: float = 0.0,
        initial_regime: str = "",
        initial_stress: float = 0.0,
        extra: dict | None = None,
    ) -> TradeThesis:
        thesis = TradeThesis(
            signal_id=signal_id,
            bot_id=bot_id,
            direction=direction,
            narrative=narrative,
            invalidation_rules=list(invalidation_rules),
            opened_at=datetime.now(UTC).isoformat(),
            opened_at_price=float(opened_at_price),
            initial_regime=initial_regime,
            initial_stress=float(initial_stress),
            extra=extra or {},
        )
        self._open[signal_id] = thesis
        self._save()
        return thesis

    def close_thesis(self, signal_id: str) -> TradeThesis | None:
        thesis = self._open.pop(signal_id, None)
        self._save()
        return thesis

    def list_open(self) -> list[TradeThesis]:
        return list(self._open.values())

    def check(
        self,
        *,
        signal_id: str,
        current_state: dict,
    ) -> ThesisBreach | None:
        """Evaluate every invalidation rule for this signal. Returns
        the FIRST tripped rule (and logs the breach), or None if all
        pass."""
        thesis = self._open.get(signal_id)
        if thesis is None:
            return None
        for rule in thesis.invalidation_rules:
            try:
                if _evaluate_rule(rule, thesis, current_state):
                    breach = ThesisBreach(
                        signal_id=signal_id,
                        ts=datetime.now(UTC).isoformat(),
                        rule_kind=rule.kind,
                        rule_description=rule.description or rule.kind,
                        current_state=dict(current_state),
                    )
                    self._log_breach(breach)
                    return breach
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "thesis_tracker: rule eval failed for %s/%s (%s)",
                    signal_id,
                    rule.kind,
                    exc,
                )
        return None

    def check_all_open(
        self,
        *,
        current_state_by_signal: dict[str, dict],
    ) -> list[ThesisBreach]:
        """Sweep every open thesis. Useful for the per-tick loop."""
        breaches: list[ThesisBreach] = []
        for sig in list(self._open.keys()):
            state = current_state_by_signal.get(sig)
            if state is None:
                continue
            b = self.check(signal_id=sig, current_state=state)
            if b is not None:
                breaches.append(b)
        return breaches

    # ── Persistence ──────────────────────────────────────────

    def _load(self) -> None:
        if not self.theses_path.exists():
            return
        try:
            data = json.loads(self.theses_path.read_text(encoding="utf-8"))
            for sig, raw in data.items():
                rules = [ThesisInvalidationRule(**r) for r in raw.get("invalidation_rules", [])]
                t = TradeThesis(
                    signal_id=raw["signal_id"],
                    bot_id=raw.get("bot_id", ""),
                    direction=raw.get("direction", "long"),
                    narrative=raw.get("narrative", ""),
                    invalidation_rules=rules,
                    opened_at=raw.get("opened_at", ""),
                    opened_at_price=float(raw.get("opened_at_price", 0.0)),
                    initial_regime=raw.get("initial_regime", ""),
                    initial_stress=float(raw.get("initial_stress", 0.0)),
                    extra=raw.get("extra", {}),
                )
                self._open[sig] = t
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("thesis_tracker: load failed (%s); fresh start", exc)

    def _save(self) -> None:
        try:
            self.theses_path.parent.mkdir(parents=True, exist_ok=True)
            self.theses_path.write_text(
                json.dumps(
                    {sig: asdict(t) for sig, t in self._open.items()},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("thesis_tracker: save failed (%s)", exc)

    def _log_breach(self, breach: ThesisBreach) -> None:
        try:
            self.breach_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.breach_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(breach)) + "\n")
        except OSError as exc:
            logger.warning("thesis_tracker: breach log append failed (%s)", exc)
