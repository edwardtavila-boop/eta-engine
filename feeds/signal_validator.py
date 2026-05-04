"""Hard signal validation for the realistic-fill simulator.

Rejects malformed strategy signals BEFORE they become positions.  A
malformed signal in paper-soak inflates win-rate (volume_profile shipped
stops on the WRONG side of entry, which presented as 100% WR because
the "stop" was actually above entry on every long).  In live trading,
the same signal would have the broker either reject the bracket or fill
it as a market-on-touch above entry — sometimes profitable, sometimes
catastrophic, never what the strategy intended.

This module is deliberately strict.  When `paper_trade_sim` is run with
real strategies, ANY violation here means the strategy is broken and
must be fixed BEFORE further paper-soak runs add more bad evidence to
the ledger.

Validation rules
----------------
1. side ∈ {LONG, SHORT}
2. entry > 0, stop > 0, target > 0
3. LONG: stop < entry < target
4. SHORT: target < entry < stop
5. 0.1 ≤ RR ≤ 50 (RR = reward/risk)
6. stop_dist / price ≤ 20%  (catches "structural stop on a frozen profile
   that drifted hundreds of points away from current price")
7. computed_qty ≤ MAX_QTY_PCT_OF_EQUITY × equity / price  (prevents a
   degenerate tiny stop_dist from blowing through size limits)

Each violation returns a `ValidationFailure` with a stable code so the
fleet audit tool can aggregate "bot X had N stop_side_inverted failures"
without parsing strings.
"""
from __future__ import annotations

from dataclasses import dataclass


# Hard global notional cap, expressed as a multiple of account equity.
# Calibrated for futures: retail intraday margin on micro contracts is
# typically 50:1, so a 1-contract MNQ trade at $27k price = $54k notional
# on a $10k account is already ~5x leverage and still legitimate.  We
# cap at 50x — anything above that is degenerate sizing (e.g., a
# strategy returned a tiny stop_dist that produced a runaway qty).  For
# crypto spot symbols this cap is effectively unreachable since
# point_value=1.0 makes notional ≈ qty * price which a sane risk %
# never produces beyond 5-10x.
MAX_QTY_NOTIONAL_PCT_OF_EQUITY: float = 50.0  # 50x notional cap (futures-friendly)


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    code: str
    message: str
    detail: dict


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    failures: tuple[ValidationFailure, ...] = ()

    @classmethod
    def passed(cls) -> ValidationResult:
        return cls(ok=True, failures=())

    @classmethod
    def failed(cls, *failures: ValidationFailure) -> ValidationResult:
        return cls(ok=False, failures=tuple(failures))


def validate_signal(
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    qty: float,
    equity: float,
    point_value: float,
    spec_symbol: str = "",
) -> ValidationResult:
    """Apply all hard rules to a signal.  Returns ValidationResult.

    Caller decides what to do with failures (skip the trade, abort the
    whole run, log and continue).  Most paper_trade_sim modes should
    skip the trade and increment a per-bot rejection counter so the
    fleet audit can flag the strategy.
    """
    failures: list[ValidationFailure] = []
    side = side.upper()

    # 1. Side
    if side not in {"LONG", "SHORT", "BUY", "SELL"}:
        failures.append(ValidationFailure(
            code="invalid_side", message=f"side must be LONG/SHORT, got {side!r}",
            detail={"side": side},
        ))
        return ValidationResult.failed(*failures)

    is_long = side in {"LONG", "BUY"}

    # 2. Positive prices
    for name, val in (("entry", entry), ("stop", stop), ("target", target)):
        if val <= 0 or not _is_finite(val):
            failures.append(ValidationFailure(
                code="non_positive_price",
                message=f"{name} must be > 0 and finite, got {val}",
                detail={"field": name, "value": val},
            ))

    if failures:
        return ValidationResult.failed(*failures)

    # 3. Stop on correct side of entry
    if is_long and stop >= entry:
        failures.append(ValidationFailure(
            code="stop_side_inverted",
            message=f"LONG stop ({stop}) must be BELOW entry ({entry})",
            detail={"side": side, "entry": entry, "stop": stop},
        ))
    elif (not is_long) and stop <= entry:
        failures.append(ValidationFailure(
            code="stop_side_inverted",
            message=f"SHORT stop ({stop}) must be ABOVE entry ({entry})",
            detail={"side": side, "entry": entry, "stop": stop},
        ))

    # 4. Target on correct side of entry
    if is_long and target <= entry:
        failures.append(ValidationFailure(
            code="target_side_inverted",
            message=f"LONG target ({target}) must be ABOVE entry ({entry})",
            detail={"side": side, "entry": entry, "target": target},
        ))
    elif (not is_long) and target >= entry:
        failures.append(ValidationFailure(
            code="target_side_inverted",
            message=f"SHORT target ({target}) must be BELOW entry ({entry})",
            detail={"side": side, "entry": entry, "target": target},
        ))

    # If sides are inverted, the rest of the checks are noise — return now.
    if failures:
        return ValidationResult.failed(*failures)

    # 5. RR sanity
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        failures.append(ValidationFailure(
            code="zero_risk",
            message="entry == stop produces zero risk",
            detail={"entry": entry, "stop": stop},
        ))
        return ValidationResult.failed(*failures)
    rr = reward / risk
    if rr < 0.1:
        failures.append(ValidationFailure(
            code="rr_too_small",
            message=f"RR={rr:.3f} below sanity floor 0.1 (target too close)",
            detail={"rr": rr, "risk": risk, "reward": reward},
        ))
    if rr > 50:
        failures.append(ValidationFailure(
            code="rr_absurd",
            message=f"RR={rr:.1f} above sanity ceiling 50 (target too far / stop too tight)",
            detail={"rr": rr, "risk": risk, "reward": reward},
        ))

    # 6. Stop too far in % terms (catches frozen-profile-far-away bugs)
    stop_dist_pct = risk / entry
    if stop_dist_pct > 0.20:
        failures.append(ValidationFailure(
            code="stop_dist_too_wide",
            message=f"stop is {stop_dist_pct*100:.1f}% from entry — likely structural-stop drift bug",
            detail={"stop_dist_pct": stop_dist_pct, "entry": entry, "stop": stop},
        ))

    # 7. Qty / notional cap
    if qty <= 0:
        failures.append(ValidationFailure(
            code="non_positive_qty",
            message=f"qty must be > 0, got {qty}",
            detail={"qty": qty},
        ))
    elif equity > 0 and entry > 0 and point_value > 0:
        notional = abs(qty) * abs(entry) * point_value
        if notional > MAX_QTY_NOTIONAL_PCT_OF_EQUITY * equity:
            failures.append(ValidationFailure(
                code="notional_exceeds_cap",
                message=(
                    f"notional {notional:.0f} > {MAX_QTY_NOTIONAL_PCT_OF_EQUITY:.0f}x equity "
                    f"({equity:.0f}) — degenerate sizing, likely tiny stop_dist"
                ),
                detail={
                    "qty": qty, "entry": entry, "point_value": point_value,
                    "equity": equity, "notional": notional,
                },
            ))

    if failures:
        return ValidationResult.failed(*failures)
    return ValidationResult.passed()


def _is_finite(x: float) -> bool:
    import math
    return math.isfinite(x)


__all__ = [
    "MAX_QTY_NOTIONAL_PCT_OF_EQUITY",
    "ValidationFailure",
    "ValidationResult",
    "validate_signal",
]
