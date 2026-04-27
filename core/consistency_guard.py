"""
EVOLUTIONARY TRADING ALGO  //  core.consistency_guard
=========================================
Apex Trader Funding consistency-rule tracker.

Apex rule (``configs/tradovate.yaml :: consistency_rule_pct = 30``):

    Your largest single winning trading day cannot exceed 30% of your
    total net profit. If it does at the moment of eval completion, the
    eval is invalidated even if the profit target was hit.

Concretely, if total_net_profit = $2,500 then no single day may have
more than $750 of profit. A $1,500 day is a violation.

Strategy
--------
This module is a deterministic function of the per-day realized PnL
stream. It does not reach into the venue. Feed it end-of-day closes or
a rolling intraday mark via ``record_intraday(date, pnl_usd)`` and it
returns a ``ConsistencyVerdict`` describing:

  * ``status``           -- OK / WARNING / VIOLATION / INSUFFICIENT_DATA
  * ``largest_day_*``    -- which day is the concentration risk
  * ``total_net_profit`` -- across all days to date (incl. losing)
  * ``max_allowed_day``  -- threshold * total_net_profit
  * ``headroom_today``   -- additional profit today can absorb before
                             a VIOLATION fires (0.0 when already tripped)

The guard is **advisory**: it does not force-flatten. The operator /
runtime layer decides whether to halt new entries, reduce size, or
flatten when the guard returns WARNING or VIOLATION. This keeps the
policy layer and the enforcement layer cleanly separated.

Durable state
-------------
Per-day PnL is persisted to a JSON file alongside the threshold config.
Intraday updates for the *current* day rewrite that day's entry; a new
date entry appends. Atomic write pattern (``tmp + os.replace``) matches
``KillSwitchLatch`` and ``TrailingDDTracker`` for consistency.

Fail-closed on corrupt file -- an unparseable history is more dangerous
than an empty one (the eval could already be in violation and a silent
reset would mask it). Raises ``ConsistencyCorruptError`` instead.

Headroom math
-------------
Let:
    P = sum of all day PnLs (includes losing days; can be <= 0)
    M = max winning day PnL (0.0 if no winning days yet)
    d = today's PnL (already reflected in both P and M above)
    T = threshold fraction (0.30 for Apex 30% rule)

For the *current day* ``today_date`` we consider two regimes:

  * Regime A: today is not yet the largest day (d <= M_prior where
    M_prior is the max over days != today_date).
    Constraint: M_prior <= T * P. So `d` has a *lower bound*:
        d_min = M_prior / T - P_prior
    If d >= d_min the rule holds. Any extra profit today only helps.

  * Regime B: today would be the largest day (d > M_prior).
    Constraint: d <= T * P = T * (P_prior + d)
             => d <= (T / (1 - T)) * P_prior
    That gives a hard upper bound on `d` above which today itself
    becomes the violating concentration.

The **headroom** we report is the minimum of:
    * the distance from d to the Regime-B cap, and
    * +inf if M_prior already satisfies the rule at any d

i.e. ``headroom = max(0.0, d_cap - d)`` where ``d_cap`` is the
Regime-B upper bound computed at current P_prior, M_prior. If
M_prior > d_cap the cap is raised to M_prior (you cannot be required
to take less profit than an already-fixed prior day).
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover — platform fallback
    ZoneInfo = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConsistencyCorruptError(RuntimeError):
    """Raised when the persisted consistency-history file is unparseable."""


# ---------------------------------------------------------------------------
# Public enums + dataclasses
# ---------------------------------------------------------------------------


class ConsistencyStatus(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # total_net_profit <= 0
    OK = "OK"  # ratio < warning
    WARNING = "WARNING"  # warning <= ratio < violation
    VIOLATION = "VIOLATION"  # ratio >= violation


@dataclass
class ConsistencyVerdict:
    """Structured result of a consistency check at a single point in time."""

    status: ConsistencyStatus
    threshold_pct: float  # violation threshold (e.g. 0.30)
    warning_pct: float  # warning threshold (e.g. 0.25)
    largest_day_usd: float
    largest_day_date: str | None
    total_net_profit_usd: float
    total_winning_profit_usd: float
    largest_day_ratio: float  # largest_day / total_net (0..inf)
    max_allowed_day_usd: float  # threshold * total_net_profit
    headroom_today_usd: float  # extra today-$ still safe
    today_pnl_usd: float
    today_date: str | None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class ConsistencyRuleState:
    """Persisted state: threshold config + per-day PnL history."""

    threshold_pct: float
    warning_pct: float
    days: dict[str, float] = field(default_factory=dict)
    # ``days[date_iso]`` is the DAY-END pnl_usd (or current intraday mark
    # for today). Dates are "YYYY-MM-DD" ISO strings.

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


class ConsistencyGuard:
    """Apex 30%-rule tracker with durable per-day history."""

    def __init__(self, path: Path, state: ConsistencyRuleState) -> None:
        self._validate_thresholds(state.threshold_pct, state.warning_pct)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = state

    @staticmethod
    def _validate_thresholds(threshold_pct: float, warning_pct: float) -> None:
        if not (0.0 < threshold_pct < 1.0):
            msg = f"threshold_pct must be in (0, 1), got {threshold_pct}"
            raise ValueError(msg)
        if not (0.0 < warning_pct <= threshold_pct):
            msg = f"warning_pct must be in (0, threshold_pct], got warning={warning_pct} vs threshold={threshold_pct}"
            raise ValueError(msg)

    @classmethod
    def load_or_init(
        cls,
        path: Path,
        threshold_pct: float = 0.30,
        warning_pct: float = 0.25,
    ) -> ConsistencyGuard:
        """Load state from disk or initialize empty history."""
        p = Path(path)
        if not p.exists():
            state = ConsistencyRuleState(
                threshold_pct=threshold_pct,
                warning_pct=warning_pct,
                days={},
            )
            guard = cls(p, state)
            guard._write_atomic()
            return guard

        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            msg = (
                f"consistency-history file corrupt at {p}: {exc}. "
                f"Manual operator review required -- do NOT delete this "
                f"file without verifying the eval history is preserved."
            )
            raise ConsistencyCorruptError(msg) from exc

        loaded_thr = float(raw.get("threshold_pct", threshold_pct))
        loaded_warn = float(raw.get("warning_pct", warning_pct))
        days_raw = raw.get("days") or {}
        days: dict[str, float] = {}
        for k, v in days_raw.items():
            if not isinstance(k, str):
                msg = f"consistency-history has invalid date key: {k!r}"
                raise ConsistencyCorruptError(msg)
            try:
                date.fromisoformat(k)
            except (TypeError, ValueError) as exc:
                msg = f"consistency-history has invalid date key {k!r}: {exc}"
                raise ConsistencyCorruptError(msg) from exc
            days[k] = float(v)
        state = ConsistencyRuleState(
            threshold_pct=loaded_thr,
            warning_pct=loaded_warn,
            days=days,
        )
        cls._validate_thresholds(state.threshold_pct, state.warning_pct)
        return cls(p, state)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _write_atomic(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._state.as_dict(), indent=2, sort_keys=True)
        tmp.write_text(payload, encoding="utf-8")
        try:
            with tmp.open("rb") as fh:
                os.fsync(fh.fileno())
        except OSError:  # pragma: no cover - platform-dependent
            log.debug("fsync failed for %s (continuing)", tmp, exc_info=True)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    # Read accessors
    # ------------------------------------------------------------------ #
    def state(self) -> ConsistencyRuleState:
        return self._state

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def record_eod(
        self,
        date_iso: str,
        realized_pnl_usd: float,
    ) -> ConsistencyVerdict:
        """Record an end-of-day PnL snapshot and return a fresh verdict.

        Overwrites any prior entry for ``date_iso`` (idempotent for
        rebroadcasts of the same day close).
        """
        self._assert_iso_date(date_iso)
        self._state.days[date_iso] = float(realized_pnl_usd)
        self._write_atomic()
        return self.evaluate(today_date=date_iso, today_pnl_usd=realized_pnl_usd)

    def record_intraday(
        self,
        date_iso: str,
        today_realized_pnl_usd: float,
    ) -> ConsistencyVerdict:
        """Record today's in-progress realized PnL.

        Overwrites today's entry on each call. End-of-day, the same
        method freezes the final value (call once more with the close).
        """
        return self.record_eod(date_iso, today_realized_pnl_usd)

    def reset(self) -> None:
        """Wipe per-day history. Thresholds preserved."""
        self._state = ConsistencyRuleState(
            threshold_pct=self._state.threshold_pct,
            warning_pct=self._state.warning_pct,
            days={},
        )
        self._write_atomic()
        log.warning(
            "consistency guard RESET: thresholds preserved (threshold=%.2f warning=%.2f)",
            self._state.threshold_pct,
            self._state.warning_pct,
        )

    # ------------------------------------------------------------------ #
    # Evaluation -- the pure policy math
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        today_date: str | None = None,
        today_pnl_usd: float | None = None,
    ) -> ConsistencyVerdict:
        """Compute the current consistency verdict.

        When ``today_date`` is provided and not yet in the history, it
        is treated as a zero-PnL day for the headroom math. When
        ``today_pnl_usd`` is provided it is taken as the current mark
        for today (does NOT mutate state -- use ``record_intraday`` for
        that).
        """
        days = dict(self._state.days)
        if today_date is not None:
            self._assert_iso_date(today_date)
            if today_pnl_usd is not None:
                days[today_date] = float(today_pnl_usd)
            elif today_date not in days:
                days[today_date] = 0.0

        total_net = sum(days.values())
        winning = {d: p for d, p in days.items() if p > 0}
        total_winning = sum(winning.values())
        if winning:
            largest_day_date, largest_day = max(
                winning.items(),
                key=lambda kv: kv[1],
            )
        else:
            largest_day_date, largest_day = None, 0.0

        threshold = self._state.threshold_pct
        warning = self._state.warning_pct

        max_allowed = threshold * total_net if total_net > 0 else 0.0
        ratio = (largest_day / total_net) if total_net > 0 else 0.0

        if total_net <= 0 or largest_day <= 0:
            status = ConsistencyStatus.INSUFFICIENT_DATA
        elif ratio >= threshold:
            status = ConsistencyStatus.VIOLATION
        elif ratio >= warning:
            status = ConsistencyStatus.WARNING
        else:
            status = ConsistencyStatus.OK

        today_pnl = (
            float(today_pnl_usd)
            if today_pnl_usd is not None
            else days.get(today_date, 0.0)
            if today_date is not None
            else 0.0
        )
        headroom = self._headroom(
            days=days,
            today_date=today_date,
            threshold=threshold,
        )

        return ConsistencyVerdict(
            status=status,
            threshold_pct=threshold,
            warning_pct=warning,
            largest_day_usd=float(largest_day),
            largest_day_date=largest_day_date,
            total_net_profit_usd=float(total_net),
            total_winning_profit_usd=float(total_winning),
            largest_day_ratio=float(ratio),
            max_allowed_day_usd=float(max_allowed),
            headroom_today_usd=float(headroom),
            today_pnl_usd=float(today_pnl),
            today_date=today_date,
        )

    # ------------------------------------------------------------------ #
    # Internal math helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _headroom(
        days: dict[str, float],
        today_date: str | None,
        threshold: float,
    ) -> float:
        """Compute max extra profit today can absorb before a VIOLATION.

        Returns 0.0 when no headroom exists, and a large finite number
        when the constraint is already satisfied with room to grow.
        """
        if today_date is None:
            return 0.0

        today_pnl = float(days.get(today_date, 0.0))
        prior_days = {d: p for d, p in days.items() if d != today_date}
        prior_total = sum(prior_days.values())
        prior_winners = [p for p in prior_days.values() if p > 0]
        prior_max_win = max(prior_winners) if prior_winners else 0.0

        # Regime-B cap: the max today-PnL that keeps today itself under
        # the threshold when today IS the largest day.
        #   today <= threshold * (prior_total + today)
        #   today * (1 - threshold) <= threshold * prior_total
        #   today_cap = threshold * prior_total / (1 - threshold)
        #
        # If prior_total is <= 0, the denominator stays (1 - threshold)
        # but the numerator pulls the cap to <= 0. Headroom is then 0
        # (no winning budget until prior_total grows).
        if threshold >= 1.0:  # pragma: no cover - validated upstream
            return math.inf
        regime_b_cap = threshold * prior_total / (1.0 - threshold)

        # The effective cap is raised to prior_max_win when prior_max_win
        # exceeds regime_b_cap AND prior_max_win <= threshold*total is
        # still satisfiable. Specifically: prior_max_win <= threshold *
        # (prior_total + today). Solve for today:
        #   today >= prior_max_win / threshold - prior_total
        # So as long as today >= that floor, a value of today between
        # floor and any value where today itself doesn't overtake
        # prior_max_win stays OK.
        if prior_max_win > 0 and prior_max_win > regime_b_cap:
            # Prior history alone pins the effective cap at prior_max_win
            # because any today_pnl <= prior_max_win keeps today off the
            # top spot and the rule reduces to the prior_max_win bound.
            floor_needed = prior_max_win / threshold - prior_total
            # Only valid if prior_max_win <= threshold * (prior_total + today)
            # can be satisfied with today <= prior_max_win. That collapses
            # to today >= floor_needed AND today <= prior_max_win.
            if floor_needed <= prior_max_win:
                # Headroom is the distance from current today_pnl to
                # prior_max_win (where today would overtake). Beyond that
                # we'd slip into Regime B, and the regime_b_cap is lower
                # than prior_max_win, so that's the new ceiling.
                effective_cap = prior_max_win
            else:
                # History is already in violation (prior_max_win > threshold
                # * prior_total even if today is infinite) -- no safe today.
                return 0.0
        else:
            effective_cap = regime_b_cap

        headroom = effective_cap - today_pnl
        return max(0.0, headroom)

    # ------------------------------------------------------------------ #
    # Input validation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _assert_iso_date(s: str) -> None:
        try:
            date.fromisoformat(s)
        except (TypeError, ValueError) as exc:
            msg = f"date must be ISO YYYY-MM-DD, got {s!r}: {exc}"
            raise ValueError(msg) from exc


# ---------------------------------------------------------------------------
# Convenience constructor when only a threshold is known
# ---------------------------------------------------------------------------


def default_apex_50k_guard(path: Path) -> ConsistencyGuard:
    """Standard Apex 50K eval guard: 30% threshold, 25% warning band."""
    return ConsistencyGuard.load_or_init(
        path=path,
        threshold_pct=0.30,
        warning_pct=0.25,
    )


def utc_today_iso() -> str:
    """Return today's date in ISO form (UTC). Tests can freeze this.

    .. deprecated::
        Prefer :func:`apex_trading_day_iso` for Apex eval accounting.
        UTC midnight splits a US equity-futures session day in half --
        overnight sessions cross the UTC boundary and get charged to
        two different Apex day buckets, which understates the real
        "largest day" and inflates the denominator.
    """
    return datetime.now(UTC).date().isoformat()


# Apex Trader Funding's trading-day rollover is 17:00 US/Central (5pm CT,
# the CME "globex close" convention). DST-aware. In practice:
#   * UTC-5 (CST / winter) → rollover at 22:00 UTC
#   * UTC-6 (CDT / summer, wait no -- CDT is UTC-5, CST is UTC-6) →
# Correction: Chicago observes CST (UTC-6) in winter and CDT (UTC-5) in
# summer. Rollover at 17:00 local →
#   * summer (CDT, UTC-5): 22:00 UTC
#   * winter (CST, UTC-6): 23:00 UTC
#
# We use zoneinfo to let the stdlib handle the offset. No hard-coded
# hour constant — that would silently drift twice a year at DST.
_APEX_TZ_NAME = "America/Chicago"
_APEX_ROLLOVER_HOUR_LOCAL = 17  # 5pm CT


def apex_trading_day_iso(now_utc: datetime | None = None) -> str:
    """Return the Apex trading-day key for ``now_utc`` (defaults to now).

    Apex defines a trading day as the 24-hour window that ends at
    17:00 US/Central. A UTC timestamp at 22:30 UTC in July (17:30
    CDT) is therefore on the FOLLOWING Apex trading day -- the
    overnight session has already started.

    If the ``zoneinfo`` module is unavailable (very old Python /
    exotic Windows install), falls back to a safe 23:00-UTC rollover
    that is wrong by at most one hour during summer but never splits
    an RTH session. Logs a warning in that case.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)

    if ZoneInfo is not None:
        local = now_utc.astimezone(ZoneInfo(_APEX_TZ_NAME))
        # If we've crossed the 17:00 local rollover, we're already
        # in the NEXT trading day.
        trading_day = local.date() + timedelta(days=1) if local.hour >= _APEX_ROLLOVER_HOUR_LOCAL else local.date()
        return trading_day.isoformat()

    # zoneinfo missing -- degraded fallback. Pick 23:00 UTC (CST
    # winter rollover); wrong by 1h in summer but never mid-RTH.
    log.warning(
        "zoneinfo unavailable; using fixed 23:00-UTC Apex day rollover. "
        "Install tzdata or upgrade Python for DST-accurate accounting.",
    )
    rollover_utc_hour = 23
    trading_day = now_utc.date() + timedelta(days=1) if now_utc.hour >= rollover_utc_hour else now_utc.date()
    return trading_day.isoformat()


# ---------------------------------------------------------------------------
# R4 closure -- CME-calendar-aware session-day rollover
# ---------------------------------------------------------------------------
# Background: ``apex_trading_day_iso`` correctly handles the 17:00 CT rollover
# but can still return a Saturday, Sunday, or US federal-holiday key when the
# input timestamp falls in one of those windows. Apex has no activity on
# those dates (CME Globex closed) so the bucket is effectively orphaned --
# the 30%-rule denominator gets a zero-PnL entry that doesn't match Apex's
# own accounting. Fix: roll forward to the next real trading day.

# CME observes every US federal "closed" holiday. List matches the
# CME Globex calendar under "full closure" events as of 2026.
_CME_FIXED_CLOSURES: frozenset[tuple[int, int]] = frozenset(
    {
        (1, 1),  # New Year's Day
        (6, 19),  # Juneteenth (recognized by CME since 2022)
        (7, 4),  # Independence Day
        (12, 25),  # Christmas Day
    }
)


def _is_cme_holiday(d: date) -> bool:
    """Return True if ``d`` is a CME Globex full-closure holiday.

    Fixed-date holidays: New Year's Day, Juneteenth, Independence Day,
    Christmas Day. Moveable holidays: MLK Day (3rd Mon Jan),
    Presidents' Day (3rd Mon Feb), Good Friday (Friday before Easter),
    Memorial Day (last Mon May), Labor Day (1st Mon Sep),
    Thanksgiving (4th Thu Nov).
    """
    if (d.month, d.day) in _CME_FIXED_CLOSURES:
        return True
    # 3rd Monday of January -- MLK
    if d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True
    # 3rd Monday of February -- Presidents' Day
    if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
        return True
    # Last Monday of May -- Memorial Day
    if d.month == 5 and d.weekday() == 0 and d.day >= 25:
        return True
    # 1st Monday of September -- Labor Day
    if d.month == 9 and d.weekday() == 0 and d.day <= 7:
        return True
    # 4th Thursday of November -- Thanksgiving
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
        return True
    # Good Friday -- 2 days before Easter Sunday.
    # dateutil.easter is the canonical stdlib-adjacent helper.
    try:
        from dateutil.easter import easter as _easter

        if d == _easter(d.year) - timedelta(days=2):
            return True
    except ImportError:
        # No dateutil -- conservatively skip Good Friday check. The
        # consistency-guard bucket would just include Good Friday as a
        # normal trading day, which is slightly wrong but strictly safer
        # than crashing the runtime on a live-trading Friday.
        log.warning(
            "dateutil.easter unavailable; Good Friday will not be treated "
            "as a CME holiday. Install python-dateutil for full accuracy.",
        )
    return False


def _is_trading_day(d: date) -> bool:
    """CME trading day: not weekend, not a full-closure holiday."""
    return d.weekday() < 5 and not _is_cme_holiday(d)


def _next_trading_day(d: date) -> date:
    """Roll forward to the next trading day (inclusive of ``d``)."""
    while not _is_trading_day(d):
        d += timedelta(days=1)
    return d


def apex_trading_day_iso_cme(now_utc: datetime | None = None) -> str:
    """CME-calendar-aware variant of ``apex_trading_day_iso``.

    Computes the base 17:00-CT session-day key, then rolls forward to
    the next actual trading day if that key lands on a weekend or
    US federal holiday. Use this for Apex consistency-guard bucketing
    so the 30%-rule denominator never includes orphan weekend/holiday
    entries that Apex itself doesn't count.

    Edge-case map (CDT, summer timestamps):
      * Fri 17:30 CT (Fri 22:30 UTC): base=Sat -> rolls to Mon.
      * Sat 10:00 CT (Sat 15:00 UTC): base=Sat -> rolls to Mon.
      * Sun 16:00 CT (Sun 21:00 UTC): base=Sun -> rolls to Mon.
      * Sun 17:30 CT (Sun 22:30 UTC): base=Mon -> unchanged.
      * Dec 25 anytime: base=Dec 25 -> rolls to Dec 26 (or further if 26
        is weekend).
      * Good Friday anytime: base=Good Fri -> rolls to Sat -> rolls to
        Mon (Easter Mon is a trading day at CME).

    Weekday timestamps outside holidays return identical output to the
    base helper -- this is a strict superset.
    """
    base_iso = apex_trading_day_iso(now_utc=now_utc)
    base = date.fromisoformat(base_iso)
    if _is_trading_day(base):
        return base_iso
    return _next_trading_day(base).isoformat()


__all__ = [
    "ConsistencyCorruptError",
    "ConsistencyGuard",
    "ConsistencyRuleState",
    "ConsistencyStatus",
    "ConsistencyVerdict",
    "apex_trading_day_iso",
    "apex_trading_day_iso_cme",
    "default_apex_50k_guard",
    "utc_today_iso",
]
