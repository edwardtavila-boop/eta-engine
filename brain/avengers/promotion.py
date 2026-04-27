"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.promotion
===========================================
Canonical gatekeeper for the strategy lifecycle:
``SHADOW -> PAPER -> LIVE_1LOT -> LIVE_FULL``.

Why this exists
---------------
Every strategy the Generator ships must earn its way to live sizing. The
old implicit rule ("looks good in backtest, push it live") is how
accounts die. This module enforces an explicit, auditable gate:

  1. **SHADOW**   -- reads live data, no orders. Must rack up N trades +
                    days + Sharpe + max-DD before graduating.
  2. **PAPER**    -- sends simulated orders to the live account. Tighter
                    thresholds + reconciliation check vs SHADOW numbers.
  3. **LIVE_1LOT** -- real money, one micro contract. Measure real
                     slippage and real PnL for a minimum window.
  4. **LIVE_FULL** -- full sizing per risk budget.

Each transition requires ALL of:
  * time-in-stage >= ``min_days``
  * trades >= ``min_trades``
  * sharpe >= ``min_sharpe``
  * max_dd_pct <= ``max_dd_pct``
  * win_rate >= ``min_win_rate``

Failure to meet thresholds keeps the strategy at its current stage
(HOLD). A hard break (drawdown spike, Sharpe collapse) triggers DEMOTE
back one stage, or RETIRE if already at SHADOW.

State
-----
JSON file at ``~/.jarvis/promotion.json`` with a dict of specs keyed by
``strategy_id``. Append-only JSONL audit log at
``~/.jarvis/promotion.jsonl``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PROMOTION_STATE: Path = Path.home() / ".jarvis" / "promotion.json"
PROMOTION_JOURNAL: Path = Path.home() / ".jarvis" / "promotion.jsonl"


class PromotionStage(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_1LOT = "LIVE_1LOT"
    LIVE_FULL = "LIVE_FULL"
    RETIRED = "RETIRED"


# Linear ordering for promote / demote.
_ORDER: list[PromotionStage] = [
    PromotionStage.SHADOW,
    PromotionStage.PAPER,
    PromotionStage.LIVE_1LOT,
    PromotionStage.LIVE_FULL,
]


class StageThresholds(BaseModel):
    """Gate thresholds for one stage-transition step."""

    model_config = ConfigDict(frozen=True)

    min_days: float = Field(ge=0.0, default=14.0)
    min_trades: int = Field(ge=0, default=50)
    min_sharpe: float = 1.0
    max_dd_pct: float = Field(ge=0.0, default=5.0)
    min_win_rate: float = Field(ge=0.0, le=1.0, default=0.45)
    max_slip_bps: float = Field(ge=0.0, default=3.0)


# Default thresholds per transition. Hardened as we climb.
_DEFAULT_THRESHOLDS: dict[PromotionStage, StageThresholds] = {
    # Leaving SHADOW -> PAPER: minimum walk.
    PromotionStage.SHADOW: StageThresholds(
        min_days=14.0,
        min_trades=50,
        min_sharpe=1.0,
        max_dd_pct=5.0,
        min_win_rate=0.45,
        max_slip_bps=3.0,
    ),
    # Leaving PAPER -> LIVE_1LOT: stricter; slippage + reconciliation.
    PromotionStage.PAPER: StageThresholds(
        min_days=21.0,
        min_trades=100,
        min_sharpe=1.3,
        max_dd_pct=4.0,
        min_win_rate=0.48,
        max_slip_bps=2.5,
    ),
    # Leaving LIVE_1LOT -> LIVE_FULL: real-money proof.
    PromotionStage.LIVE_1LOT: StageThresholds(
        min_days=30.0,
        min_trades=150,
        min_sharpe=1.5,
        max_dd_pct=3.5,
        min_win_rate=0.50,
        max_slip_bps=2.0,
    ),
    # LIVE_FULL is terminal upward. Entry here is promotion-only; no
    # further upshift.
    PromotionStage.LIVE_FULL: StageThresholds(
        min_days=0,
        min_trades=0,
        min_sharpe=0.0,
        max_dd_pct=100.0,
        min_win_rate=0.0,
        max_slip_bps=999.0,
    ),
}


class StageMetrics(BaseModel):
    """Current rolling metrics for a strategy inside its stage."""

    model_config = ConfigDict(frozen=True)

    trades: int = Field(ge=0, default=0)
    days_active: float = Field(ge=0.0, default=0.0)
    sharpe: float = 0.0
    max_dd_pct: float = Field(ge=0.0, default=0.0)
    win_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    mean_slippage_bps: float = Field(ge=0.0, default=0.0)
    pnl: float = 0.0


class PromotionAction(StrEnum):
    PROMOTE = "PROMOTE"
    HOLD = "HOLD"
    DEMOTE = "DEMOTE"
    RETIRE = "RETIRE"


class PromotionDecision(BaseModel):
    """One gate evaluation output."""

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    from_stage: PromotionStage
    to_stage: PromotionStage
    action: PromotionAction
    reasons: list[str]
    metrics: StageMetrics


class PromotionSpec(BaseModel):
    """State of one strategy inside the pipeline."""

    model_config = ConfigDict(frozen=False)

    strategy_id: str
    current_stage: PromotionStage
    entered_stage_at: datetime
    metrics: StageMetrics


# --- red team gate ------------------------------------------------------------


class RedTeamVerdict(BaseModel):
    """Output of a Red Team review called on a promote decision.

    Parameters
    ----------
    approve
        If False, the PromotionGate converts the PROMOTE into a HOLD
        with the red team's reasons attached. If True, PROMOTE proceeds.
    reasons
        Human-readable bullet list of why the red team ruled the way it
        did. Written to the promotion journal for audit.
    risk_score
        Optional continuous risk score in [0, 1]. 0 is safe, 1 is very
        risky. The gate itself doesn't use this number directly -- it's
        for dashboards and trend analysis.
    """

    model_config = ConfigDict(frozen=True)

    approve: bool
    reasons: list[str] = Field(default_factory=list)
    risk_score: float = Field(default=0.0, ge=0.0, le=1.0)


# The callable injected into ``PromotionGate`` on construction. Must not
# raise -- any error in the red team is treated as a veto (fail-closed).
RedTeamGate = Callable[[PromotionSpec, "PromotionDecision"], RedTeamVerdict]


# Stage transitions that require red-team approval before the gate
# converts a PROMOTE into a committed stage move. LIVE_1LOT -> LIVE_FULL
# is intentionally excluded here -- it's a sizing decision, and the
# risk-budget engine owns it. The red team veto covers the two
# transitions where an un-vetted strategy first touches external systems
# (PAPER) or real money (LIVE_1LOT).
RED_TEAM_GATED_TRANSITIONS: frozenset[tuple[PromotionStage, PromotionStage]] = frozenset(
    {
        (PromotionStage.SHADOW, PromotionStage.PAPER),
        (PromotionStage.PAPER, PromotionStage.LIVE_1LOT),
    }
)


# --- default red team -------------------------------------------------------
#
# The promotion gate's red_team_gate hook was historically opt-in: if no
# callable was injected the gate approved every transition. That made the
# whole RED_TEAM_GATED_TRANSITIONS frozenset a decoration -- the gate was
# silently open. The default callable below closes that gap by performing
# a deterministic, stdlib-only structural review that fails closed on
# fragile clearances. Operators can still pass a richer (e.g. LLM-backed)
# callable via ``red_team_gate=`` to override.
#
# What "fragile clearance" means here: the promotion gate already verified
# the strategy cleared its stage thresholds. The red team's job is to ask
# how much *margin* that clearance had. A strategy that cleared trades by
# one trade or sharpe by 0.01 has almost certainly been optimized against
# the threshold and is unlikely to survive out-of-sample. We veto those.

DEFAULT_TIGHT_MARGIN_PCT = 0.10  # flag if within 10% of threshold
DEFAULT_TRADES_SAFETY_FACTOR = 1.30  # require trades >= 1.3 * min_trades
DEFAULT_MIN_LIVE_SLIPPAGE_BPS = 0.25  # slippage lower than this is unrealistic


def default_red_team_gate(
    spec: PromotionSpec,
    decision: PromotionDecision,
    *,
    thresholds: dict[PromotionStage, StageThresholds] | None = None,
    tight_margin_pct: float = DEFAULT_TIGHT_MARGIN_PCT,
    trades_safety_factor: float = DEFAULT_TRADES_SAFETY_FACTOR,
    min_live_slippage_bps: float = DEFAULT_MIN_LIVE_SLIPPAGE_BPS,
) -> RedTeamVerdict:
    """Deterministic, stdlib-only default red-team callable.

    Runs in the promotion hot path so must be cheap and must never raise.
    Escalates to VETO if any of the following fragility conditions hit:

    1. **Tight sharpe clearance** -- ``sharpe < min_sharpe * (1 + margin)``.
    2. **Tight drawdown clearance** -- ``max_dd_pct > max_dd_pct * (1 - margin)``.
    3. **Tight win-rate clearance** -- ``win_rate < min_win_rate * (1 + margin)``.
    4. **Undersampled** -- ``trades < min_trades * trades_safety_factor``.
    5. **Slippage floor** -- realised slippage < ``min_live_slippage_bps``
       on a PAPER->LIVE_1LOT transition. Backtest-like fills in a paper
       account signal an unrealistic fill model (see microstructure audit F).

    The ``risk_score`` is the normalised fraction of the above signals
    that fired, so dashboards can show "severity" rather than just a
    boolean.
    """
    thr_map = thresholds or _DEFAULT_THRESHOLDS
    thr = thr_map.get(decision.from_stage)
    if thr is None:
        # No thresholds registered -- safest response is to approve;
        # PromotionGate has already done its own gating.
        return RedTeamVerdict(approve=True, reasons=[], risk_score=0.0)

    m = spec.metrics
    reasons: list[str] = []

    # --- tight clearances ---------------------------------------------
    if thr.min_sharpe > 0.0:
        required = thr.min_sharpe * (1.0 + tight_margin_pct)
        if m.sharpe < required:
            reasons.append(
                f"sharpe={m.sharpe:.2f} clears min={thr.min_sharpe:.2f} "
                f"but under {tight_margin_pct:.0%} margin (needs >= {required:.2f})",
            )

    if thr.max_dd_pct > 0.0:
        ceiling = thr.max_dd_pct * (1.0 - tight_margin_pct)
        if m.max_dd_pct > ceiling:
            reasons.append(
                f"max_dd_pct={m.max_dd_pct:.2f} clears {thr.max_dd_pct:.2f} "
                f"but within {tight_margin_pct:.0%} margin "
                f"(needs <= {ceiling:.2f})",
            )

    if thr.min_win_rate > 0.0:
        required_wr = thr.min_win_rate * (1.0 + tight_margin_pct)
        if m.win_rate < required_wr:
            reasons.append(
                f"win_rate={m.win_rate:.2%} clears {thr.min_win_rate:.2%} "
                f"but under {tight_margin_pct:.0%} margin "
                f"(needs >= {required_wr:.2%})",
            )

    # --- sample-size adequacy ----------------------------------------
    if thr.min_trades > 0:
        required_n = int(thr.min_trades * trades_safety_factor)
        if m.trades < required_n:
            reasons.append(
                f"trades={m.trades} below safety floor {required_n} "
                f"({trades_safety_factor:.1f}x min_trades={thr.min_trades})",
            )

    # --- slippage realism (only for the PAPER -> LIVE transition) ----
    if (
        decision.from_stage is PromotionStage.PAPER
        and decision.to_stage is PromotionStage.LIVE_1LOT
        and m.mean_slippage_bps < min_live_slippage_bps
    ):
        reasons.append(
            f"mean_slippage_bps={m.mean_slippage_bps:.2f} below realism floor "
            f"{min_live_slippage_bps:.2f} -- paper fills look like backtest fills",
        )

    # --- verdict ------------------------------------------------------
    # Risk score = fraction of the 5 checks that fired. 0 on clean pass,
    # 1.0 if every check flagged. Clamp just in case.
    n_checks = 5
    risk_score = max(0.0, min(1.0, len(reasons) / n_checks))
    approve = len(reasons) == 0
    return RedTeamVerdict(
        approve=approve,
        reasons=reasons,
        risk_score=risk_score,
    )


# --- the gate -----------------------------------------------------------------


class PromotionGate:
    """Decides PROMOTE / HOLD / DEMOTE / RETIRE per strategy.

    Parameters
    ----------
    state_path
        JSON file holding ``{strategy_id: PromotionSpec}``. Defaults to
        ``~/.jarvis/promotion.json``.
    thresholds
        Per-stage thresholds. Defaults to the hardcoded ladder.
    demote_dd_pct
        If realised drawdown exceeds this (percent), DEMOTE one level
        even if time-in-stage hasn't elapsed.
    demote_sharpe
        If realised Sharpe drops below this, DEMOTE.
    """

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        journal_path: Path | None = None,
        thresholds: dict[PromotionStage, StageThresholds] | None = None,
        demote_dd_pct: float = 10.0,
        demote_sharpe: float = -0.5,
        red_team_gate: RedTeamGate | None = default_red_team_gate,
        clock: callable | None = None,
    ) -> None:
        # ``red_team_gate`` defaults to ``default_red_team_gate`` so that
        # any ``PromotionGate()`` built without arguments gets a real
        # red-team veto by default. Pass ``red_team_gate=None`` to disable
        # (old behaviour), or pass your own callable (LLM-backed, etc.)
        # to override.
        self.state_path = state_path or PROMOTION_STATE
        self.journal_path = journal_path or PROMOTION_JOURNAL
        self.thresholds = thresholds or _DEFAULT_THRESHOLDS
        self.demote_dd_pct = demote_dd_pct
        self.demote_sharpe = demote_sharpe
        self.red_team_gate = red_team_gate
        self._clock = clock or (lambda: datetime.now(UTC))
        self._specs: dict[str, PromotionSpec] = {}
        # Last red-team verdict keyed by strategy_id -- persisted through
        # apply() into the journal so dashboards can show "why did this
        # PROMOTE get vetoed".
        self._last_rt_verdict: dict[str, RedTeamVerdict] = {}
        self._load_state()

    # --- state i/o ---------------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for sid, payload in (raw or {}).items():
            try:
                self._specs[sid] = PromotionSpec.model_validate(payload)
            except ValueError:
                continue

    def _persist_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {sid: spec.model_dump(mode="json") for sid, spec in self._specs.items()}
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)

    def _append_journal(self, record: dict) -> None:
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            return

    # --- public API --------------------------------------------------------

    def register(
        self,
        strategy_id: str,
        *,
        stage: PromotionStage = PromotionStage.SHADOW,
    ) -> PromotionSpec:
        """Create a new spec. Idempotent -- returns existing if present."""
        existing = self._specs.get(strategy_id)
        if existing is not None:
            return existing
        spec = PromotionSpec(
            strategy_id=strategy_id,
            current_stage=stage,
            entered_stage_at=self._clock(),
            metrics=StageMetrics(),
        )
        self._specs[strategy_id] = spec
        self._persist_state()
        self._append_journal(
            {
                "ts": self._clock().isoformat(),
                "event": "register",
                "strategy_id": strategy_id,
                "stage": stage.value,
            }
        )
        return spec

    def update_metrics(
        self,
        strategy_id: str,
        metrics: StageMetrics,
    ) -> PromotionSpec:
        spec = self._specs.get(strategy_id)
        if spec is None:
            spec = self.register(strategy_id)
        spec.metrics = metrics
        self._specs[strategy_id] = spec
        self._persist_state()
        return spec

    def evaluate(self, strategy_id: str) -> PromotionDecision:
        """Decide what to do with a strategy. Does NOT mutate state."""
        spec = self._specs.get(strategy_id)
        if spec is None:
            # Never seen -- caller must register first.
            return PromotionDecision(
                strategy_id=strategy_id,
                from_stage=PromotionStage.SHADOW,
                to_stage=PromotionStage.SHADOW,
                action=PromotionAction.HOLD,
                reasons=["unknown strategy; register() first"],
                metrics=StageMetrics(),
            )

        stage = spec.current_stage
        metrics = spec.metrics

        if stage is PromotionStage.RETIRED:
            return PromotionDecision(
                strategy_id=strategy_id,
                from_stage=stage,
                to_stage=stage,
                action=PromotionAction.HOLD,
                reasons=["strategy is RETIRED"],
                metrics=metrics,
            )

        # --- hard-break demote check ---
        hard_break: list[str] = []
        if metrics.max_dd_pct >= self.demote_dd_pct:
            hard_break.append(
                f"max_dd_pct={metrics.max_dd_pct:.2f} >= demote_dd_pct={self.demote_dd_pct:.2f}",
            )
        if metrics.sharpe <= self.demote_sharpe and metrics.trades > 10:
            hard_break.append(
                f"sharpe={metrics.sharpe:.2f} <= demote_sharpe={self.demote_sharpe:.2f}",
            )

        if hard_break:
            if stage is PromotionStage.SHADOW:
                return PromotionDecision(
                    strategy_id=strategy_id,
                    from_stage=stage,
                    to_stage=PromotionStage.RETIRED,
                    action=PromotionAction.RETIRE,
                    reasons=["hard break at SHADOW", *hard_break],
                    metrics=metrics,
                )
            down = self._down(stage)
            return PromotionDecision(
                strategy_id=strategy_id,
                from_stage=stage,
                to_stage=down,
                action=PromotionAction.DEMOTE,
                reasons=hard_break,
                metrics=metrics,
            )

        # --- promote check ---
        if stage is PromotionStage.LIVE_FULL:
            return PromotionDecision(
                strategy_id=strategy_id,
                from_stage=stage,
                to_stage=stage,
                action=PromotionAction.HOLD,
                reasons=["already at LIVE_FULL; terminal"],
                metrics=metrics,
            )

        thr = self.thresholds.get(stage, _DEFAULT_THRESHOLDS[stage])
        gate_reasons: list[str] = []

        if metrics.days_active < thr.min_days:
            gate_reasons.append(
                f"days_active={metrics.days_active:.1f} < min_days={thr.min_days:.1f}",
            )
        if metrics.trades < thr.min_trades:
            gate_reasons.append(
                f"trades={metrics.trades} < min_trades={thr.min_trades}",
            )
        if metrics.sharpe < thr.min_sharpe:
            gate_reasons.append(
                f"sharpe={metrics.sharpe:.2f} < min_sharpe={thr.min_sharpe:.2f}",
            )
        if metrics.max_dd_pct > thr.max_dd_pct:
            gate_reasons.append(
                f"max_dd_pct={metrics.max_dd_pct:.2f} > max_dd_pct={thr.max_dd_pct:.2f}",
            )
        if metrics.win_rate < thr.min_win_rate:
            gate_reasons.append(
                f"win_rate={metrics.win_rate:.2f} < min_win_rate={thr.min_win_rate:.2f}",
            )
        if metrics.mean_slippage_bps > thr.max_slip_bps:
            gate_reasons.append(
                f"mean_slip_bps={metrics.mean_slippage_bps:.2f} > max={thr.max_slip_bps:.2f}",
            )

        if gate_reasons:
            return PromotionDecision(
                strategy_id=strategy_id,
                from_stage=stage,
                to_stage=stage,
                action=PromotionAction.HOLD,
                reasons=gate_reasons,
                metrics=metrics,
            )

        up = self._up(stage)
        tentative = PromotionDecision(
            strategy_id=strategy_id,
            from_stage=stage,
            to_stage=up,
            action=PromotionAction.PROMOTE,
            reasons=["all thresholds cleared"],
            metrics=metrics,
        )

        # --- red team gate ------------------------------------------------
        # Only consult on the transitions where a strategy first touches
        # external systems or real money. Failure in the red team callable
        # is fail-closed: any exception becomes a HOLD with an explicit
        # veto reason so the operator notices the gate is broken.
        if self.red_team_gate is not None and (stage, up) in RED_TEAM_GATED_TRANSITIONS:
            try:
                verdict = self.red_team_gate(spec, tentative)
            except Exception as exc:  # noqa: BLE001
                verdict = RedTeamVerdict(
                    approve=False,
                    reasons=[f"red_team callable raised: {type(exc).__name__}: {exc}"],
                    risk_score=1.0,
                )
            self._last_rt_verdict[strategy_id] = verdict
            if not verdict.approve:
                reasons = [
                    f"red_team_blocked ({stage.value}->{up.value}): risk_score={verdict.risk_score:.2f}",
                    *verdict.reasons,
                ]
                return PromotionDecision(
                    strategy_id=strategy_id,
                    from_stage=stage,
                    to_stage=stage,
                    action=PromotionAction.HOLD,
                    reasons=reasons,
                    metrics=metrics,
                )
            # Approved -- fall through to PROMOTE.

        return tentative

    def apply(self, decision: PromotionDecision) -> PromotionSpec:
        """Commit a decision. Mutates state and writes journal entry."""
        spec = self._specs.get(decision.strategy_id)
        if spec is None:
            spec = self.register(decision.strategy_id)

        if decision.action in {
            PromotionAction.PROMOTE,
            PromotionAction.DEMOTE,
            PromotionAction.RETIRE,
        }:
            spec.current_stage = decision.to_stage
            spec.entered_stage_at = self._clock()
            # Reset metrics on stage change: new stage, new clock.
            spec.metrics = StageMetrics()

        self._specs[decision.strategy_id] = spec
        self._persist_state()
        record: dict = {
            "ts": self._clock().isoformat(),
            "event": "apply",
            "strategy_id": decision.strategy_id,
            "from_stage": decision.from_stage.value,
            "to_stage": decision.to_stage.value,
            "action": decision.action.value,
            "reasons": decision.reasons,
        }
        # If a red-team verdict was recorded during the matching
        # ``evaluate()``, stamp it into the journal. The dashboard and
        # post-mortem tooling reads this to explain vetoes.
        rt = self._last_rt_verdict.pop(decision.strategy_id, None)
        if rt is not None:
            record["red_team"] = rt.model_dump(mode="json")
        self._append_journal(record)
        return spec

    # --- red-team verdict introspection -----------------------------------

    def last_red_team_verdict(self, strategy_id: str) -> RedTeamVerdict | None:
        """Return the most recent red-team verdict for ``strategy_id``.

        The verdict is only retained until the matching ``apply()`` call
        consumes it into the journal; outside that window this returns
        ``None``. Useful for "show me why the gate blocked this" UIs.
        """
        return self._last_rt_verdict.get(strategy_id)

    def snapshot(self) -> list[PromotionSpec]:
        return sorted(self._specs.values(), key=lambda s: s.strategy_id)

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _up(stage: PromotionStage) -> PromotionStage:
        try:
            i = _ORDER.index(stage)
        except ValueError:
            return stage
        return _ORDER[min(i + 1, len(_ORDER) - 1)]

    @staticmethod
    def _down(stage: PromotionStage) -> PromotionStage:
        try:
            i = _ORDER.index(stage)
        except ValueError:
            return stage
        return _ORDER[max(i - 1, 0)]


__all__ = [
    "DEFAULT_MIN_LIVE_SLIPPAGE_BPS",
    "DEFAULT_TIGHT_MARGIN_PCT",
    "DEFAULT_TRADES_SAFETY_FACTOR",
    "PROMOTION_JOURNAL",
    "PROMOTION_STATE",
    "RED_TEAM_GATED_TRANSITIONS",
    "PromotionAction",
    "PromotionDecision",
    "PromotionGate",
    "PromotionSpec",
    "PromotionStage",
    "RedTeamGate",
    "RedTeamVerdict",
    "StageMetrics",
    "StageThresholds",
    "default_red_team_gate",
]
