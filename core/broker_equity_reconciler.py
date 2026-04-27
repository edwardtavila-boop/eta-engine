"""
EVOLUTIONARY TRADING ALGO  //  core.broker_equity_reconciler
================================================
R1 closure (partial) -- drift detection between logical (bot-computed)
equity and broker-reported mark-to-market equity.

Why this exists
---------------
``TrailingDDTracker`` consumes the equity stream fed by the bot runtime
(from realized P/L journals plus open-position mark-to-market). That's
the *logical* equity -- it's a function of our own fill journal, our own
commission model, and our own unrealized-PnL formula.

Apex enforces the trailing DD against the **broker**'s reported equity
(net liquidation / account value). The two can silently drift due to:

  * Commissions we didn't account for (IBKR cash commissions, regulatory
    fees, execution fees not in our fill model).
  * Slippage between our assumed fill and the broker's actual fill.
  * Overnight carry, assignment premiums, margin-interest on short options.
  * Clock skew between our tick and the broker's snapshot.

If the broker's equity is $120 lower than ours, our tracker says
"distance = $500" when the eval is actually at $380. The cushion evaporates
silently.

What this module does
---------------------
  * Accepts a ``broker_equity_source`` callable that returns the broker's
    current net liquidation USD (or ``None`` if unavailable -- paper,
    dry-run, or broker adapter not yet wired).
  * On every reconcile tick, compares logical equity to broker equity.
  * Emits a ``ReconcileResult`` with drift in USD and percent.
  * Above a configurable tolerance, flips an "out-of-tolerance" flag
    that the runtime can surface as an alert. Does NOT itself pause or
    flatten -- policy is the KillSwitch's job; this module is observation.

What this module does NOT do (yet -- v0.2.x)
---------------------------------------------
  * Does NOT fetch broker equity directly. The reconciler is fed by a
    caller-supplied function because the broker adapter surface for
    account-value polling is not yet uniform (``IBKRAdapter.get_balance``
    returns an empty dict today). Wiring that up per broker is v0.2.x
    scope.
  * Does NOT synthesize a KillVerdict on out-of-tolerance. The bot
    equity stream continues to feed the tracker; we surface the drift
    as an observability signal so the operator can decide whether to
    trust the logical equity or pause.
  * Does NOT replace logical equity with broker equity. When broker data
    is available, the runtime may choose to feed broker-equity-minus-
    open-pnl to the tracker instead -- but that swap is a venue-
    integration choice, not a reconciler responsibility.

Usage
-----
    def _ibkr_net_liq() -> float | None:
        # v0.2.x: poll IBKR /iserver/account/summary net_liq field.
        return None  # placeholder for pre-wiring

    rec = BrokerEquityReconciler(
        broker_equity_source=_ibkr_net_liq,
        tolerance_usd=50.0,
        tolerance_pct=0.001,  # 0.1% of logical
    )
    result = rec.reconcile(logical_equity_usd=50_123.45)
    if not result.in_tolerance:
        alert.warn("equity drift", evidence=result.as_dict())

The design is additive: v0.1.59 ships the module with no broker data
source wired, so it's effectively a no-op. v0.2.x wires each broker's
adapter `get_balance()` through to `broker_equity_source` and flips the
runtime to respond to out-of-tolerance events.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)


def _sanitize_for_json(value: float | None) -> float | None:
    """Return ``value`` unchanged unless it is ``inf`` / ``-inf`` / ``NaN``.

    H5 closure (v0.1.65). The Python ``json`` module emits ``inf`` and
    ``NaN`` as the literals ``Infinity`` / ``NaN`` -- which are valid
    JavaScript but **not** valid JSON per RFC 8259 §6. Downstream
    consumers (the dashboard, the alerts log parser, anything reading
    ``runtime_log.jsonl`` with a stricter parser) will choke. Treat
    those sentinels as ``None`` for serialisation.
    """
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


@dataclass(frozen=True)
class ReconcileResult:
    """One reconcile snapshot: logical vs broker equity.

    Attributes
    ----------
    ts:
        ISO-UTC timestamp of the reconcile tick.
    logical_equity_usd:
        Equity computed by the bot runtime (from fill journals).
    broker_equity_usd:
        Equity reported by the broker. ``None`` if broker source has
        not returned a value yet (e.g. pre-wired, adapter dormant).
    drift_usd:
        ``logical - broker``. Positive means our logical equity is
        *above* the broker -- i.e. our cushion is over-stated.
        ``None`` when broker equity is unavailable.
    drift_pct_of_logical:
        ``drift_usd / logical_equity_usd`` as a ratio (0.001 == 0.1%).
        ``None`` when broker equity is unavailable.
    in_tolerance:
        ``True`` when both drift_usd and drift_pct are within the
        configured tolerances. ``True`` by convention when broker
        equity is unavailable (we can't assert drift we can't see).
    reason:
        Human-readable classification. One of:
        * ``"no_broker_data"`` -- broker source returned ``None``
        * ``"within_tolerance"`` -- drift is small
        * ``"broker_below_logical"`` -- broker < logical, cushion over-stated
        * ``"broker_above_logical"`` -- broker > logical, cushion under-stated
    is_in_drift_state:
        H3 closure (v0.1.66). Tracks the latched drift state across
        ticks. ``True`` when the reconciler has classified at least
        one tick as ``broker_below_logical`` and has not yet seen a
        clean recovery into the (tighter) clear band. Distinct from
        ``in_tolerance`` which describes only the current tick: a
        single noisy tick can flip ``in_tolerance`` False/True/False
        without ``is_in_drift_state`` ever clearing, because clearing
        requires sustained recovery into the hysteresis clear band.
    transition:
        H3 closure (v0.1.66). One of:
        * ``"entered_drift"`` -- this tick crossed the trigger band
          while the prior state was ARMED (not in drift).
        * ``"exited_drift"`` -- this tick crossed back into the clear
          band while the prior state was DRIFTING.
        * ``"stable"`` -- no transition this tick. The runtime uses
          ``transition`` + ``is_in_drift_state`` to decide whether to
          fire an entry alert, a sustained-drift re-alert, a
          recovery alert, or stay quiet.
    """

    ts: str
    logical_equity_usd: float
    broker_equity_usd: float | None
    drift_usd: float | None
    drift_pct_of_logical: float | None
    in_tolerance: bool
    reason: str
    is_in_drift_state: bool = False
    transition: str = "stable"

    def as_dict(self) -> dict[str, Any]:
        """Serialisation view -- defensive against inf / NaN floats.

        H5 closure (v0.1.65): every numeric field is sanitised through
        :func:`_sanitize_for_json` so a stale state object that somehow
        carries ``inf`` / ``NaN`` (e.g. constructed manually in a test,
        or built before the v0.1.65 ``min_logical_usd`` guard landed)
        cannot produce RFC-8259-violating output.
        """
        return {
            "ts": self.ts,
            "logical_equity_usd": _sanitize_for_json(self.logical_equity_usd),
            "broker_equity_usd": _sanitize_for_json(self.broker_equity_usd),
            "drift_usd": _sanitize_for_json(self.drift_usd),
            "drift_pct_of_logical": _sanitize_for_json(self.drift_pct_of_logical),
            "in_tolerance": self.in_tolerance,
            "reason": self.reason,
            "is_in_drift_state": self.is_in_drift_state,
            "transition": self.transition,
        }


@dataclass
class ReconcileStats:
    """Running counters over the lifetime of the reconciler."""

    checks_total: int = 0
    checks_no_data: int = 0
    checks_in_tolerance: int = 0
    checks_out_of_tolerance: int = 0
    max_drift_usd_abs: float = 0.0
    last_result: ReconcileResult | None = field(default=None)
    # H3 closure (v0.1.66): hysteresis transition counters.
    drift_state_entries: int = 0
    drift_state_exits: int = 0
    # L2 closure (v0.1.67): windowed max drift, computed over the last
    # ``drift_window_size`` reconcile ticks (no_broker_data ticks not
    # included in the window). The lifetime ``max_drift_usd_abs`` stays
    # for forensic posterity; the windowed value gives the H1
    # calibration harness a moving statistic that ages out stale
    # spikes from earlier in the eval.
    windowed_max_drift_usd_abs: float = 0.0
    drift_window_size: int = 0


class BrokerEquityReconciler:
    """Drift detector for logical vs broker-reported equity.

    Parameters
    ----------
    broker_equity_source:
        Zero-arg callable returning broker net-liq in USD, or ``None``
        if the broker integration is not wired up / the adapter is
        dormant / the data is stale.
    tolerance_usd:
        Absolute drift threshold in USD. Drift strictly above this
        (regardless of sign) flips out-of-tolerance.
    tolerance_pct:
        Fractional drift threshold (0.001 == 0.1%). Drift above this
        flips out-of-tolerance. Both thresholds are enforced jointly;
        we go out-of-tolerance when EITHER is exceeded.
    min_logical_usd:
        Minimum logical equity for which a percentage drift is
        defined. When ``logical_equity_usd < min_logical_usd``, the
        reconcile result is classified as ``no_broker_data`` (we
        cannot meaningfully divide by a zero-or-tiny denominator,
        and producing ``inf`` would corrupt the JSON tick log).
        Defaults to 1.0 USD -- below 1 dollar of equity the eval is
        already over and drift detection is moot. H5 closure
        (v0.1.65).
    name:
        Optional identifier used in log lines. Defaults to
        ``"broker_equity_reconciler"``.
    """

    def __init__(
        self,
        broker_equity_source: Callable[[], float | None],
        *,
        tolerance_usd: float = 50.0,
        tolerance_pct: float = 0.001,
        tolerance_below_usd: float | None = None,
        tolerance_below_pct: float | None = None,
        tolerance_above_usd: float | None = None,
        tolerance_above_pct: float | None = None,
        min_logical_usd: float = 1.0,
        clear_tolerance_below_usd: float | None = None,
        clear_tolerance_below_pct: float | None = None,
        drift_window_size: int = 1000,
        name: str = "broker_equity_reconciler",
    ) -> None:
        # H2 closure (Red Team v0.1.64 review): asymmetric tolerances.
        # ``tolerance_usd`` / ``tolerance_pct`` are the SYMMETRIC default
        # (used when no per-direction override is supplied). When the
        # caller supplies ``tolerance_below_*`` and/or ``tolerance_above_*``
        # those override the per-direction effective threshold:
        #
        #   * broker_below_logical  -> use tolerance_below_*  (eval-bust
        #                              risk; should be tight)
        #   * broker_above_logical  -> use tolerance_above_*  (MTM lag /
        #                              broker rebate; should be looser
        #                              to avoid false positives)
        #
        # Backwards-compatibility: omitting all of the per-direction
        # parameters preserves the v0.1.65 symmetric behaviour
        # exactly. Existing callers and tests that only set
        # tolerance_usd / tolerance_pct continue to work unchanged.
        if tolerance_usd < 0:
            msg = f"tolerance_usd must be >= 0 (got {tolerance_usd})"
            raise ValueError(msg)
        if tolerance_pct < 0:
            msg = f"tolerance_pct must be >= 0 (got {tolerance_pct})"
            raise ValueError(msg)
        if min_logical_usd < 0:
            msg = f"min_logical_usd must be >= 0 (got {min_logical_usd})"
            raise ValueError(msg)
        # Validate per-direction overrides individually so an early
        # negative does not mask a later one.
        for label, val in (
            ("tolerance_below_usd", tolerance_below_usd),
            ("tolerance_below_pct", tolerance_below_pct),
            ("tolerance_above_usd", tolerance_above_usd),
            ("tolerance_above_pct", tolerance_above_pct),
        ):
            if val is not None and val < 0:
                raise ValueError(f"{label} must be >= 0 (got {val})")
        self._source = broker_equity_source
        self.tolerance_usd = float(tolerance_usd)
        self.tolerance_pct = float(tolerance_pct)
        # Resolve effective per-direction thresholds. When an override
        # is None, fall back to the symmetric default. Stored as
        # public attributes so the boot banner / observability can
        # surface them directly.
        self.tolerance_below_usd = float(
            tolerance_below_usd if tolerance_below_usd is not None else tolerance_usd,
        )
        self.tolerance_below_pct = float(
            tolerance_below_pct if tolerance_below_pct is not None else tolerance_pct,
        )
        self.tolerance_above_usd = float(
            tolerance_above_usd if tolerance_above_usd is not None else tolerance_usd,
        )
        self.tolerance_above_pct = float(
            tolerance_above_pct if tolerance_above_pct is not None else tolerance_pct,
        )
        self.min_logical_usd = float(min_logical_usd)
        # H3 closure (v0.1.66): hysteresis clear band for the below-
        # logical direction. Only the below-direction latches a drift
        # state (the above-direction is informational), so only the
        # below-direction needs hysteresis. Defaults to 70% of the
        # effective below-trigger -- gives a 30% noise margin without
        # making the operator wait too long for the latch to clear on
        # genuine recovery. Pass equal to the trigger to disable
        # hysteresis (latch flips on every threshold cross).
        if clear_tolerance_below_usd is None:
            clear_tolerance_below_usd = self.tolerance_below_usd * 0.7
        if clear_tolerance_below_pct is None:
            clear_tolerance_below_pct = self.tolerance_below_pct * 0.7
        if clear_tolerance_below_usd < 0:
            msg = f"clear_tolerance_below_usd must be >= 0 (got {clear_tolerance_below_usd})"
            raise ValueError(msg)
        if clear_tolerance_below_pct < 0:
            msg = f"clear_tolerance_below_pct must be >= 0 (got {clear_tolerance_below_pct})"
            raise ValueError(msg)
        if clear_tolerance_below_usd > self.tolerance_below_usd:
            msg = (
                f"clear_tolerance_below_usd "
                f"({clear_tolerance_below_usd}) must be <= "
                f"tolerance_below_usd ({self.tolerance_below_usd}); a "
                f"clear band wider than the trigger band would cause "
                f"the latch to clear before drift even crosses the "
                f"trigger"
            )
            raise ValueError(msg)
        if clear_tolerance_below_pct > self.tolerance_below_pct:
            msg = (
                f"clear_tolerance_below_pct "
                f"({clear_tolerance_below_pct}) must be <= "
                f"tolerance_below_pct ({self.tolerance_below_pct})"
            )
            raise ValueError(msg)
        self.clear_tolerance_below_usd = float(clear_tolerance_below_usd)
        self.clear_tolerance_below_pct = float(clear_tolerance_below_pct)
        # L2 closure (v0.1.67): windowed-max drift tracking. The deque
        # holds drift_abs values from the last ``drift_window_size``
        # ticks that produced a real classification (no_broker_data
        # ticks are not added to the window). Default 1000 == ~16 min
        # at the v0.1.65 1s tick cadence -- short enough to age out
        # stale spikes, long enough to retain a meaningful sample for
        # the H1 calibration harness when it lands.
        if drift_window_size < 0:
            msg = f"drift_window_size must be >= 0 (got {drift_window_size})"
            raise ValueError(msg)
        self.drift_window_size = int(drift_window_size)
        self._drift_window: deque[float] = deque(
            maxlen=self.drift_window_size or None,
        )
        self.name = name
        self._stats = ReconcileStats(drift_window_size=self.drift_window_size)
        # H3 closure (v0.1.66): latched drift state. Flips True on
        # entry into a broker_below_logical out-of-tolerance tick;
        # flips back False only when drift falls inside BOTH clear
        # bands (USD and pct). The latch survives no_broker_data
        # ticks -- a transient adapter blink does not clear genuine
        # drift -- and survives broker_above_logical / within_tolerance
        # ticks that are still outside the (tighter) clear band.
        self._in_drift_state: bool = False

    @property
    def stats(self) -> ReconcileStats:
        return self._stats

    def reconcile(self, logical_equity_usd: float) -> ReconcileResult:
        """Perform a single reconcile tick.

        Parameters
        ----------
        logical_equity_usd:
            Current logical (bot-computed) equity in USD.

        Returns
        -------
        ReconcileResult
            Snapshot of the comparison. Always returns a result, even
            when broker data is unavailable (the ``reason`` field
            carries the classification).
        """
        ts = datetime.now(UTC).isoformat()
        self._stats.checks_total += 1

        # H5 closure (v0.1.65): guard against logical equity below the
        # configured floor BEFORE we touch broker data. Below the floor
        # the percentage drift is undefined; producing inf would corrupt
        # the JSON tick log (RFC 8259 violation). Classify as no_data so
        # the runtime path stays uniform.
        if not math.isfinite(logical_equity_usd) or logical_equity_usd < self.min_logical_usd:
            self._stats.checks_no_data += 1
            # H3 closure: a no_broker_data tick is silent on the latch
            # -- the drift state from before this tick carries through.
            # No transition.
            result = ReconcileResult(
                ts=ts,
                logical_equity_usd=(float(logical_equity_usd) if math.isfinite(logical_equity_usd) else 0.0),
                broker_equity_usd=None,
                drift_usd=None,
                drift_pct_of_logical=None,
                in_tolerance=True,
                reason="no_broker_data",
                is_in_drift_state=self._in_drift_state,
                transition="stable",
            )
            self._stats.last_result = result
            return result

        try:
            broker_equity = self._source()
        except Exception as exc:
            log.warning(
                "%s: broker equity source raised %s -- treating as no_data",
                self.name,
                exc,
                exc_info=True,
            )
            broker_equity = None

        if broker_equity is None:
            self._stats.checks_no_data += 1
            result = ReconcileResult(
                ts=ts,
                logical_equity_usd=logical_equity_usd,
                broker_equity_usd=None,
                drift_usd=None,
                drift_pct_of_logical=None,
                in_tolerance=True,
                reason="no_broker_data",
                is_in_drift_state=self._in_drift_state,
                transition="stable",
            )
            self._stats.last_result = result
            return result

        drift_usd = float(logical_equity_usd) - float(broker_equity)
        drift_abs = abs(drift_usd)
        # logical_equity_usd >= min_logical_usd >= 0 here, so the
        # division below is safe.
        drift_pct = drift_abs / abs(float(logical_equity_usd))

        # H2 closure: pick threshold pair by sign of drift. Below =
        # broker is below logical (eval-bust risk, dangerous, tight);
        # above = broker is above logical (MTM lag / rebate, harmless,
        # looser to avoid false positives). drift_usd == 0 case picks
        # below by convention but is a no-op since drift_abs == 0.
        if drift_usd >= 0:
            tol_usd_eff = self.tolerance_below_usd
            tol_pct_eff = self.tolerance_below_pct
        else:
            tol_usd_eff = self.tolerance_above_usd
            tol_pct_eff = self.tolerance_above_pct

        exceeds_usd = drift_abs > tol_usd_eff
        exceeds_pct = drift_pct > tol_pct_eff
        in_tolerance = not (exceeds_usd or exceeds_pct)

        if in_tolerance:
            reason = "within_tolerance"
            self._stats.checks_in_tolerance += 1
        elif drift_usd > 0:
            reason = "broker_below_logical"
            self._stats.checks_out_of_tolerance += 1
            log.warning(
                "%s: DRIFT broker_below_logical: logical=%.2f broker=%.2f "
                "drift=%.2f (%.4f%%) tol_below_usd=%.2f tol_below_pct=%.4f",
                self.name,
                logical_equity_usd,
                broker_equity,
                drift_usd,
                drift_pct * 100.0,
                self.tolerance_below_usd,
                self.tolerance_below_pct,
            )
        else:
            reason = "broker_above_logical"
            self._stats.checks_out_of_tolerance += 1
            log.info(
                "%s: drift broker_above_logical: logical=%.2f broker=%.2f "
                "drift=%.2f (%.4f%%) tol_above_usd=%.2f tol_above_pct=%.4f",
                self.name,
                logical_equity_usd,
                broker_equity,
                drift_usd,
                drift_pct * 100.0,
                self.tolerance_above_usd,
                self.tolerance_above_pct,
            )

        self._stats.max_drift_usd_abs = max(
            self._stats.max_drift_usd_abs,
            drift_abs,
        )
        # L2 closure (v0.1.67): track windowed max drift over the last
        # ``drift_window_size`` real (non no_broker_data) ticks. The
        # deque appends with bounded maxlen so old values age out.
        # Re-computing max() per tick is cheap at the default 1000-
        # element window and avoids the complexity of a heap-based
        # sliding-window max. drift_window_size==0 disables the window.
        if self.drift_window_size > 0:
            self._drift_window.append(drift_abs)
            self._stats.windowed_max_drift_usd_abs = max(self._drift_window)

        # H3 closure (v0.1.66): hysteresis-driven drift-state machine.
        #
        # State transitions:
        #   ARMED + reason=="broker_below_logical" -> DRIFTING
        #     (transition="entered_drift")
        #   DRIFTING + drift inside clear band     -> ARMED
        #     (transition="exited_drift")
        #   anything else                          -> stay
        #     (transition="stable")
        #
        # The clear-band test is "drift_usd >= 0 (still below or zero)
        # AND drift_abs <= clear_tolerance_below_usd AND drift_pct
        # <= clear_tolerance_below_pct". If broker overshoots logical
        # (drift_usd < 0), that is broker_above_logical -- not a
        # cushion-overstated condition any more, so we treat it as
        # a clean exit from the below-direction drift state.
        was_in_drift = self._in_drift_state
        if not was_in_drift:
            if reason == "broker_below_logical":
                self._in_drift_state = True
                self._stats.drift_state_entries += 1
                transition = "entered_drift"
            else:
                transition = "stable"
        else:
            # Currently DRIFTING. Check if drift has fallen inside the
            # clear band. drift_usd < 0 (broker_above_logical) also
            # clears the latch -- we are no longer in the dangerous
            # cushion-overstated direction.
            cleared = drift_usd < 0 or (
                drift_abs <= self.clear_tolerance_below_usd and drift_pct <= self.clear_tolerance_below_pct
            )
            if cleared:
                self._in_drift_state = False
                self._stats.drift_state_exits += 1
                transition = "exited_drift"
            else:
                transition = "stable"

        result = ReconcileResult(
            ts=ts,
            logical_equity_usd=float(logical_equity_usd),
            broker_equity_usd=float(broker_equity),
            drift_usd=drift_usd,
            drift_pct_of_logical=drift_pct,
            in_tolerance=in_tolerance,
            reason=reason,
            is_in_drift_state=self._in_drift_state,
            transition=transition,
        )
        self._stats.last_result = result
        return result


__all__ = [
    "BrokerEquityReconciler",
    "ReconcileResult",
    "ReconcileStats",
]
