"""
EVOLUTIONARY TRADING ALGO  //  core.eta_account_invariant
================================================
B3 closure (v0.1.69) -- explicit documentation + validation of the
tier-A aggregate-equity invariant that the R1 broker-equity drift
detector relies on.

Why this exists
---------------
The drift detector compares the runtime's *logical* equity --
``sum(bot.state.equity_usd for bot in tier_a_bots)`` -- against the
single broker account's reported net-liquidation. The two are only
comparable if all tier-A bots are slicing one shared Apex account.

The implicit invariant
----------------------

  **All tier-A bots in a given runtime trade in ONE Apex account.
  Each bot's ``equity_usd`` is a SLICE of that shared account, not
  an independent number. The sum across tier-A bots equals the
  broker's net-liquidation for that account, modulo realized
  commissions / slippage / funding / carry that the bots do not
  perfectly model.**

What the Red Team review (B3) called out
----------------------------------------

A misconfigured fleet violates this invariant in two predictable
ways:

  1. **Two tier-A bots, both initialised at the full account size.**
     Operator copies the MNQ bot config to start an NQ bot, forgets
     to halve the per-bot allocation. Now ``sum_logical = 2 * 50K``
     vs ``broker_net_liq = 50K`` -- the reconciler classifies this
     as a $50K ``broker_below_logical`` drift and fires
     FLATTEN_TIER_A_PREEMPTIVE on what is actually a config bug.

  2. **A tier-A bot whose equity goes negative.**
     A simulation crash leaves ``equity_usd = -10_000``. The sum
     comes out absurdly low; the reconciler classifies as huge
     ``broker_above_logical`` (cushion under-stated) -- not
     dangerous, but the boot-time sanity check should catch it.

What this module does NOT do
----------------------------
* Does NOT require multiple tier-A bots to share an explicit
  ``account_id`` field. ``BotSnapshot`` does not carry one today,
  and adding it would be a wider surface change. The invariant is
  documented and validated *aggregate-side* (against the broker
  net-liq + an operator-supplied expected account size).

* Does NOT promote a violation to a KillVerdict. The validator is
  ADVISORY -- a violation logs WARN and fires a
  ``tier_a_invariant_violation`` alert. The operator decides
  whether to halt. Promoting to a verdict is v0.2.x scope (gated
  on the same H1 calibration empirics that gate
  KillVerdict-on-drift synthesis -- shares M2's exit criteria
  per docs/red_team_d2_d3_review.md). Lands when the M2 closure
  ships, at which point this validator's WARN gets paired with a
  KillVerdict(PAUSE_NEW_ENTRIES) emitted from the same code
  path that handles broker-drift verdicts.

* Does NOT assert that the broker account size matches what the
  bots think. We do not directly read ``broker_net_liq`` here --
  the BrokerEquityReconciler is the single owner of that
  comparison.  This validator is an upstream sanity-check on
  ``sum_logical`` itself.

Usage
-----
    from eta_engine.core.eta_account_invariant import (
        validate_tier_a_aggregate_equity,
    )

    result = validate_tier_a_aggregate_equity(
        snapshots=tier_a_snapshots,
        expected_account_size_usd=50_000.0,
    )
    if not result.ok:
        log.warning(result.reason)
        dispatcher.send("tier_a_invariant_violation", result.as_dict())
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.core.kill_switch_runtime import BotSnapshot


#: Default upper-bound multiplier on expected account size.
#: ``sum_logical > expected_account_size_usd * THIS`` triggers a
#: violation. Picked at 1.5× because reasonable tier-A drift +
#: paper-trading-overshoot can legitimately push aggregate logical
#: equity ~10-20% above the original deposit; 50% is a comfortable
#: margin that flags the ``2×`` config-bug case immediately.
DEFAULT_OVERSIZE_MULTIPLIER: float = 1.5

#: Default lower-bound multiplier. ``sum_logical < expected_account_size_usd * THIS``
#: triggers a violation. Picked at 0.0 (negative-equity check) -- a
#: tier-A bot bookkeeping a NEGATIVE equity is always a bug, but a
#: drawdown to 30-40% of starting size is a real Apex-eval state we
#: do not want to spam-alert on. Operator can tighten via the kwarg.
DEFAULT_UNDERSIZE_MULTIPLIER: float = 0.0


@dataclass(frozen=True)
class InvariantResult:
    """Verdict from :func:`validate_tier_a_aggregate_equity`.

    Attributes
    ----------
    ok:
        True iff every check passed.
    sum_logical_usd:
        ``sum(s.equity_usd for s in snapshots if s.tier == "A")``.
    n_tier_a:
        Count of tier-A bots in the input snapshot list. Useful for
        logging context.
    expected_account_size_usd:
        Echoed back from the input for serialisation clarity.
    verdict:
        One of:
        * ``"ok"`` -- every check passed
        * ``"no_tier_a_bots"`` -- nothing to validate (drift
          detection irrelevant for this tick)
        * ``"negative_aggregate"`` -- sum < 0, definitely a bug
        * ``"oversize_aggregate"`` -- sum > oversize multiplier,
          most likely a config copy-paste bug (the canonical B3
          finding)
        * ``"undersize_aggregate"`` -- sum < undersize multiplier
          (only fires when the operator has tightened the lower
          bound; default 0.0 means this verdict is reserved for
          the negative-equity case which gets ``negative_aggregate``)
        * ``"non_finite_aggregate"`` -- sum is NaN / inf
    reason:
        Human-readable string, suitable for log lines.
    """

    ok: bool
    sum_logical_usd: float
    n_tier_a: int
    expected_account_size_usd: float | None
    verdict: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sum_logical_usd": self.sum_logical_usd,
            "n_tier_a": self.n_tier_a,
            "expected_account_size_usd": self.expected_account_size_usd,
            "verdict": self.verdict,
            "reason": self.reason,
        }


class ApexAccountInvariantError(RuntimeError):
    """Raised when the operator opts into strict mode and a check fails.

    Strict mode is opt-in via the ``strict`` kwarg of
    :func:`validate_tier_a_aggregate_equity`. The default is
    advisory (returns an :class:`InvariantResult` with ``ok=False``);
    strict promotes the same violation to an exception so a
    boot-time gate can refuse to start.
    """


def validate_tier_a_aggregate_equity(
    *,
    snapshots: list[BotSnapshot],
    expected_account_size_usd: float | None = None,
    oversize_multiplier: float = DEFAULT_OVERSIZE_MULTIPLIER,
    undersize_multiplier: float = DEFAULT_UNDERSIZE_MULTIPLIER,
    strict: bool = False,
) -> InvariantResult:
    """Validate the tier-A aggregate-equity invariant.

    Parameters
    ----------
    snapshots:
        The full bot snapshot list for the tick. Non-tier-A bots are
        ignored -- the validator filters by ``tier == "A"`` before
        computing the aggregate.
    expected_account_size_usd:
        Operator-supplied expected size of the Apex eval account,
        in USD. When set, the validator checks the aggregate
        against [under * size, over * size]. When ``None``, only
        the negative-aggregate / non-finite-aggregate cases fire.
    oversize_multiplier:
        ``sum_logical > expected_account_size_usd * oversize_multiplier``
        triggers ``oversize_aggregate``. Default 1.5×.
    undersize_multiplier:
        ``sum_logical < expected_account_size_usd * undersize_multiplier``
        triggers ``undersize_aggregate``. Default 0.0 (so the
        verdict is reserved for negative-aggregate by convention;
        operator can tighten).
    strict:
        When True and the result is non-ok, raise
        :class:`ApexAccountInvariantError`. When False (default),
        return the structured result and let the caller decide.

    Returns
    -------
    InvariantResult
        Always returns; the caller inspects ``ok`` and ``verdict``.
        When ``strict`` is True, only returns on ``ok=True``.

    Raises
    ------
    ApexAccountInvariantError
        Iff ``strict`` is True and the verdict is non-ok.
    ValueError
        If multipliers are negative.
    """
    if oversize_multiplier < 0:
        msg = f"oversize_multiplier must be >= 0 (got {oversize_multiplier})"
        raise ValueError(msg)
    if undersize_multiplier < 0:
        msg = f"undersize_multiplier must be >= 0 (got {undersize_multiplier})"
        raise ValueError(msg)
    if expected_account_size_usd is not None and oversize_multiplier < undersize_multiplier:
        msg = f"oversize_multiplier ({oversize_multiplier}) must be >= undersize_multiplier ({undersize_multiplier})"
        raise ValueError(msg)

    tier_a = [s for s in snapshots if s.tier == "A"]
    n_tier_a = len(tier_a)

    if n_tier_a == 0:
        return InvariantResult(
            ok=True,
            sum_logical_usd=0.0,
            n_tier_a=0,
            expected_account_size_usd=expected_account_size_usd,
            verdict="no_tier_a_bots",
            reason="no tier-A bots present; invariant trivially satisfied",
        )

    sum_logical = sum(s.equity_usd for s in tier_a)

    if not math.isfinite(sum_logical):
        result = InvariantResult(
            ok=False,
            sum_logical_usd=sum_logical,
            n_tier_a=n_tier_a,
            expected_account_size_usd=expected_account_size_usd,
            verdict="non_finite_aggregate",
            reason=(
                f"tier-A aggregate equity is non-finite "
                f"({sum_logical}) across {n_tier_a} bot(s); a "
                f"snapshot has corrupted state"
            ),
        )
        if strict:
            raise ApexAccountInvariantError(result.reason)
        return result

    if sum_logical < 0:
        result = InvariantResult(
            ok=False,
            sum_logical_usd=sum_logical,
            n_tier_a=n_tier_a,
            expected_account_size_usd=expected_account_size_usd,
            verdict="negative_aggregate",
            reason=(
                f"tier-A aggregate equity is NEGATIVE "
                f"(${sum_logical:.2f}) across {n_tier_a} bot(s); "
                f"a bot is bookkeeping equity below zero, which "
                f"violates the invariant that bot equity slices a "
                f"shared positive-balance Apex account"
            ),
        )
        if strict:
            raise ApexAccountInvariantError(result.reason)
        return result

    if expected_account_size_usd is not None:
        upper = expected_account_size_usd * oversize_multiplier
        lower = expected_account_size_usd * undersize_multiplier
        if sum_logical > upper:
            result = InvariantResult(
                ok=False,
                sum_logical_usd=sum_logical,
                n_tier_a=n_tier_a,
                expected_account_size_usd=expected_account_size_usd,
                verdict="oversize_aggregate",
                reason=(
                    f"tier-A aggregate equity ${sum_logical:.2f} "
                    f"exceeds {oversize_multiplier:.2f}x of "
                    f"expected_account_size_usd "
                    f"${expected_account_size_usd:.2f} "
                    f"(threshold ${upper:.2f}); likely a config "
                    f"copy-paste where two tier-A bots each track "
                    f"the full account size instead of slices"
                ),
            )
            if strict:
                raise ApexAccountInvariantError(result.reason)
            return result
        # Strict undersize check only fires when the operator has
        # tightened the lower bound above 0 (default 0.0 so the
        # negative-aggregate verdict above is the only fire point).
        if undersize_multiplier > 0 and sum_logical < lower:
            result = InvariantResult(
                ok=False,
                sum_logical_usd=sum_logical,
                n_tier_a=n_tier_a,
                expected_account_size_usd=expected_account_size_usd,
                verdict="undersize_aggregate",
                reason=(
                    f"tier-A aggregate equity ${sum_logical:.2f} is "
                    f"below {undersize_multiplier:.2f}x of "
                    f"expected_account_size_usd "
                    f"${expected_account_size_usd:.2f} "
                    f"(threshold ${lower:.2f}); a tier-A bot has "
                    f"drawn the account further than the configured "
                    f"undersize floor"
                ),
            )
            if strict:
                raise ApexAccountInvariantError(result.reason)
            return result

    return InvariantResult(
        ok=True,
        sum_logical_usd=sum_logical,
        n_tier_a=n_tier_a,
        expected_account_size_usd=expected_account_size_usd,
        verdict="ok",
        reason=(f"tier-A aggregate ${sum_logical:.2f} across {n_tier_a} bot(s) within sanity bounds"),
    )


__all__ = [
    "DEFAULT_OVERSIZE_MULTIPLIER",
    "DEFAULT_UNDERSIZE_MULTIPLIER",
    "ApexAccountInvariantError",
    "InvariantResult",
    "validate_tier_a_aggregate_equity",
]
