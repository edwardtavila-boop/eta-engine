"""ATR-based bracket sizing + per-class capital budgets.

The supervisor used fixed-% brackets (1.5% stop / 2.0% target) and a
simple ``bot.cash * 0.10`` risk-unit. Both are wrong for live crypto:

  - Fixed % stops get whipsawed on high-vol BTC/ETH and leave too much
    room on quiet days. Average True Range (ATR) is volatility-aware
    and adapts.
  - bot.cash is paper currency; live crypto starts at $500-$2000 and
    must respect the operator's actual budget, not the simulator's.

This module is pure functions over a bar deque + env knobs so the
supervisor can swap in ATR brackets without restructuring tick state.

Env knobs for live capital management:

  ETA_LIVE_CRYPTO_BUDGET_PER_BOT_USD   default 100.0
  ETA_LIVE_CRYPTO_FLEET_BUDGET_USD     default 1500.0
  ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD  default 500.0   (paper futures)
  ETA_LIVE_FUTURES_FLEET_BUDGET_USD    default 5000.0  (paper futures)
  ETA_BRACKET_ATR_PERIOD               default 14
  ETA_BRACKET_ATR_STOP_MULT            default 2.0
  ETA_BRACKET_ATR_TARGET_MULT          default 3.0
  ETA_BRACKET_FALLBACK_STOP_PCT        default 0.015  (used if no ATR)
  ETA_BRACKET_FALLBACK_TARGET_PCT      default 0.020
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)


_CRYPTO_ROOTS = {"BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "MBT", "MET"}
_FUTURES_ROOTS = {
    "MNQ",
    "NQ",
    "ES",
    "MES",
    "NG",
    "CL",
    "GC",
    "ZN",
    "ZB",
    "6E",
    "M6E",
    "MGC",
    "MCL",
    "RTY",
    "M2K",
    # Equity-index Dow (added 2026-05-07): YM and MYM were missing from
    # the set, which silently fell through to the "other" asset class
    # at $100 default budget instead of $500/$10000 futures budget.
    # YM bots were thus capped at $100 cap which kills every entry.
    "YM",
    "MYM",
    # Rates micros (added 2026-05-07 as part of futures-roots audit):
    "ZF",
    "ZT",
    # Currency micros (M6E already there, M6B/M6A also exist):
    "M6B",
    "M6A",
    "6B",
    "6A",
    "6J",
    "M6J",
}


def _root(symbol: str) -> str:
    s = symbol.upper().lstrip("/").rstrip("0123456789")
    for suffix in ("USDT", "USD"):
        if s.endswith(suffix):
            s = s[: -len(suffix)] or s
            break
    return s


def _is_crypto(symbol: str) -> bool:
    return _root(symbol) in _CRYPTO_ROOTS


def _is_futures(symbol: str) -> bool:
    r = _root(symbol)
    if r in {"MBT", "MET"}:
        return False
    return r in _FUTURES_ROOTS


# ─── ATR ─────────────────────────────────────────────────────────


def compute_atr(bars: Sequence[dict[str, Any]], period: int = 14) -> float | None:
    """True-Range simple moving average over the last ``period`` bars.

    Returns None when fewer than ``period + 1`` bars are available
    (need a previous close for each TR). Bars must have ``high``,
    ``low``, ``close`` keys.
    """
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_close = float(bars[i - 1]["close"])
        high = float(bars[i]["high"])
        low = float(bars[i]["low"])
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window)


# ─── Bracket sizing ──────────────────────────────────────────────


def compute_bracket(
    *,
    side: str,
    entry_price: float,
    bars: Iterable[dict[str, Any]] | None = None,
    stop_mult_override: float | None = None,
    target_mult_override: float | None = None,
) -> tuple[float, float, str]:
    """Return ``(stop_price, target_price, source)`` for a new entry.

    Uses ATR(period) when there are enough bars; otherwise falls back
    to fixed-percent stops. ``source`` is "atr" or "fixed_pct" so the
    caller can log which path was taken.

    Per-bot overrides (``stop_mult_override``, ``target_mult_override``)
    take precedence over the global env defaults — the supervisor reads
    each bot's ``atr_stop_mult`` / ``rr_target`` from per_bot_registry
    so the live bracket geometry matches the lab's, keeping v27
    sharpe-drift comparing strategy quality, not bracket variance.
    """
    period = int(os.getenv("ETA_BRACKET_ATR_PERIOD", "14"))
    stop_mult = (
        float(stop_mult_override)
        if stop_mult_override is not None
        else float(os.getenv("ETA_BRACKET_ATR_STOP_MULT", "2.0"))
    )
    target_mult = (
        float(target_mult_override)
        if target_mult_override is not None
        else float(os.getenv("ETA_BRACKET_ATR_TARGET_MULT", "3.0"))
    )
    fallback_stop_pct = float(os.getenv("ETA_BRACKET_FALLBACK_STOP_PCT", "0.015"))
    fallback_target_pct = float(os.getenv("ETA_BRACKET_FALLBACK_TARGET_PCT", "0.020"))

    side_u = side.upper()
    bar_list = list(bars) if bars is not None else []
    atr = compute_atr(bar_list, period=period) if bar_list else None

    # Per-asset precision: FX prices live at 1.xxxx so 4-decimal rounding
    # collapses tight ATR stops to entry price (e.g. round(1.17082, 4) =
    # 1.1708 = entry → zero-distance stop → realized_r explodes when
    # divided by ~0). Use 5 decimals for FX, 2 for high-priced futures
    # like ZN/ZB (110.xx prices), 4 for everything else (default).
    decimals = _round_decimals_for(entry_price)

    if atr is not None and atr > 0:
        if side_u == "BUY":
            stop = entry_price - stop_mult * atr
            target = entry_price + target_mult * atr
        else:
            stop = entry_price + stop_mult * atr
            target = entry_price - target_mult * atr
        # Minimum stop distance guard: refuse a no-distance bracket
        # (rounding artifact). Caller (supervisor) treats stop==entry
        # as a refusal and skips the entry.
        _stop_r = round(stop, decimals)
        _target_r = round(target, decimals)
        if abs(_stop_r - entry_price) < 1e-9 or abs(_target_r - entry_price) < 1e-9:
            # Fall through to fixed_pct (which uses fractions of price,
            # always producing a meaningful distance).
            pass
        else:
            return _stop_r, _target_r, "atr"

    # Fallback: fixed percent
    if side_u == "BUY":
        stop = entry_price * (1.0 - fallback_stop_pct)
        target = entry_price * (1.0 + fallback_target_pct)
    else:
        stop = entry_price * (1.0 + fallback_stop_pct)
        target = entry_price * (1.0 - fallback_target_pct)
    return round(stop, decimals), round(target, decimals), "fixed_pct"


def _round_decimals_for(price: float) -> int:
    """Pick rounding precision based on the price's order of magnitude.

    FX (price ~1) → 5 decimals (1 pip resolution)
    BTC/ETH (10-100k) → 2 decimals
    Equity-index futures (1k-50k) → 2 decimals
    Tiny prices (HG copper 5.x, NG natgas 3.x) → 4 decimals
    Default → 4 decimals
    """
    if price <= 0:
        return 4
    if price < 5:
        return 5  # FX, NG sometimes
    if price < 100:
        return 4  # HG, ZN sometimes
    return 2  # crypto, equity-index, gold, etc.


# ─── Per-class capital budgets ──────────────────────────────────


def _budget_per_bot_usd(symbol: str, *, bot_id: str | None = None) -> float:
    """Return the per-bot capital cap in USD.

    Lookup order:
      1. Per-bot override in ``per_bot_registry.ASSIGNMENTS[bot].extras
         ["per_bot_budget_usd"]`` -- lets high-notional contracts (YM,
         GC, ES) declare a larger cap matching their margin requirement
         WITHOUT lifting the cap for every bot trading the same asset
         class. Falls through to env-var defaults if absent.
      2. Asset-class env-var:
            crypto  -> ETA_LIVE_CRYPTO_BUDGET_PER_BOT_USD  (default 100)
            futures -> ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD (default 500)
            other   -> ETA_LIVE_OTHER_BUDGET_PER_BOT_USD   (default 100)

    The bot_id argument is optional so existing callers (which only
    have ``symbol``) keep working; the per-bot override only fires
    when the caller passes its bot_id explicitly.
    """
    if bot_id:
        try:
            from eta_engine.strategies.per_bot_registry import ASSIGNMENTS

            for a in ASSIGNMENTS:
                if a.bot_id == bot_id:
                    override = (a.extras or {}).get("per_bot_budget_usd")
                    if override is not None:
                        try:
                            parsed = float(override)
                            if parsed > 0:
                                return parsed
                            logger.warning(
                                "bot %s has non-positive per_bot_budget_usd=%r; falling back to asset-class default",
                                bot_id,
                                override,
                            )
                        except (TypeError, ValueError):
                            logger.warning(
                                "bot %s has malformed per_bot_budget_usd=%r; falling back to asset-class default",
                                bot_id,
                                override,
                            )
                    break
        except Exception:  # noqa: BLE001 -- never let registry lookup fail sizing
            pass

    if _is_crypto(symbol):
        return float(os.getenv("ETA_LIVE_CRYPTO_BUDGET_PER_BOT_USD", "100.0"))
    if _is_futures(symbol):
        return float(os.getenv("ETA_LIVE_FUTURES_BUDGET_PER_BOT_USD", "500.0"))
    return float(os.getenv("ETA_LIVE_OTHER_BUDGET_PER_BOT_USD", "100.0"))


def _fleet_budget_usd(symbol: str) -> float:
    if _is_crypto(symbol):
        return float(os.getenv("ETA_LIVE_CRYPTO_FLEET_BUDGET_USD", "1500.0"))
    if _is_futures(symbol):
        return float(os.getenv("ETA_LIVE_FUTURES_FLEET_BUDGET_USD", "5000.0"))
    return float(os.getenv("ETA_LIVE_OTHER_FLEET_BUDGET_USD", "1500.0"))


def lookup_bot_bracket_params(bot_id: str) -> tuple[float | None, float | None]:
    """Read ``atr_stop_mult`` / ``rr_target`` from the bot's
    per_bot_registry assignment (nested ``*_config`` dicts in extras).

    Returns ``(stop_mult, target_mult)`` — either may be None when the
    registry entry lacks per-bot tuning. The supervisor passes these
    into compute_bracket so live and lab geometry match per-bot.

    Lookup order (first match wins):
      1. extras["bracket_params"] = {"stop_mult": .., "target_mult": ..}
      2. Any extras key ending in "_config" containing ``atr_stop_mult``
         and/or ``rr_target``.
      3. Top-level ``atr_stop_mult`` / ``rr_target`` in extras.
    """
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS
    except ImportError:
        return None, None

    for a in ASSIGNMENTS:
        if a.bot_id != bot_id:
            continue
        extras = getattr(a, "extras", {}) or {}

        bp = extras.get("bracket_params")
        if isinstance(bp, dict):
            sm = bp.get("stop_mult")
            tm = bp.get("target_mult")
            if sm is not None or tm is not None:
                return _safe_float(sm), _safe_float(tm)

        for k, v in extras.items():
            if not (isinstance(k, str) and k.endswith("_config") and isinstance(v, dict)):
                continue
            sm = v.get("atr_stop_mult")
            tm = v.get("rr_target") or v.get("target_atr") or v.get("atr_target_mult")
            if sm is not None or tm is not None:
                return _safe_float(sm), _safe_float(tm)

        sm = extras.get("atr_stop_mult")
        tm = extras.get("rr_target") or extras.get("target_atr")
        if sm is not None or tm is not None:
            return _safe_float(sm), _safe_float(tm)

        break  # found bot_id but no params → don't keep scanning
    return None, None


def _safe_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _point_value(symbol: str) -> float:
    """Return the contract multiplier (USD value per 1.0 of price).

    Thin wrapper over ``feeds.instrument_specs.effective_point_value``
    -- kept here so callers don't have to import from feeds. The shared
    helper handles the multi-venue ambiguity (BTC=5.0 in CME futures
    spec vs 1.0 on Alpaca spot, etc.) so all PnL / sizing call sites
    use the same resolution rule.
    """
    try:
        from eta_engine.feeds.instrument_specs import effective_point_value

        return effective_point_value(symbol, route="auto")
    except Exception:  # noqa: BLE001 -- conservative fallback
        return 1.0


def _is_live_money_mode() -> bool:
    """True when the supervisor is running with real money, not paper.

    The paper-futures-floor logic below disables itself in live mode
    so a sizing-cap clamp can't accidentally over-trade on a live
    account. The check looks at:
      1. ``ETA_SUPERVISOR_LIVE_MONEY=1`` (explicit live-money flag)
      2. ``ETA_SUPERVISOR_MODE`` set to ``live`` / ``live_money``
    Defaults to FALSE (paper) when neither is set -- safer to keep
    the floor on by default than to silently strip it.
    """
    if os.getenv("ETA_SUPERVISOR_LIVE_MONEY", "").strip() in ("1", "true", "yes"):
        return True
    mode = os.getenv("ETA_SUPERVISOR_MODE", "").strip().lower()
    return mode in {"live", "live_money", "real"}


def _paper_floor_enabled() -> bool:
    """True when the paper-futures min-1-lot floor should kick in.

    Live money: floor OFF, hard. The bot drops to 0 contracts when
    the cap rounds it below 1.

    Paper / sim: floor ON unless explicitly disabled via
    ``ETA_PAPER_FUTURES_FLOOR=0``. Lifts qty to 1 so paper-soak
    actually trades full contracts even when the cap rounds below 1.
    """
    if _is_live_money_mode():
        return False
    return float(os.getenv("ETA_PAPER_FUTURES_FLOOR", "1")) > 0


def cap_qty_to_budget(
    *,
    symbol: str,
    entry_price: float,
    requested_qty: float,
    fleet_open_notional_usd: float = 0.0,
    bot_id: str | None = None,
) -> tuple[float, str]:
    """Return ``(capped_qty, reason)``.

    Caps the requested qty so neither the per-bot nor the fleet budget
    is exceeded. ``reason`` is one of "ok", "per_bot_capped",
    "fleet_capped", "fleet_exhausted", "paper_futures_floor". The
    supervisor logs the reason so the operator can see when budgets
    are clamping signals.

    Notional math: ``qty * entry_price * point_value``. The point_value
    (contract multiplier) is the difference between "Dow at 49639" and
    "1 YM contract has $248,195 of economic exposure." Earlier
    iterations of this function omitted ``point_value`` and reported
    notional in INDEX-POINT terms, which silently under-counted by 5x
    to 100x for full-sized futures (caught 2026-05-07).

    ``bot_id`` (optional) lets the per-bot budget override fire. Without
    it, only the asset-class env-var defaults apply -- which is fine
    for crypto and the micro futures, but YM/GC/ES (whose 1-contract
    notional dwarfs the $10k default) need their own bigger cap.

    Live-money safety: ``ETA_SUPERVISOR_MODE=live`` or
    ``ETA_SUPERVISOR_LIVE_MONEY=1`` disables the
    ``paper_futures_floor`` automatic round-up. In live mode a bot
    that wants 1 contract but only has $10k cap will return 0 and
    skip the entry, NOT silently lift to 1 (50x over-risk).
    """
    if entry_price <= 0:
        return requested_qty, "ok"

    per_bot_cap_usd = _budget_per_bot_usd(symbol, bot_id=bot_id)
    fleet_cap_usd = _fleet_budget_usd(symbol)

    point_value = _point_value(symbol)
    requested_notional = abs(requested_qty) * entry_price * point_value

    # Fleet budget — what's left after existing open exposure?
    fleet_remaining = max(0.0, fleet_cap_usd - max(0.0, fleet_open_notional_usd))
    if fleet_remaining <= 0:
        # Paper-mode futures floor: same rationale as the per-bot floor
        # below — the fleet budget is a sanity guardrail in paper mode,
        # not a real fund constraint. If a single MNQ contract ($20-40k
        # notional) flips fleet_remaining negative, every other futures
        # bot would be locked out for the rest of the day. Floor to 1
        # contract per request to keep the fleet trading. Live mode
        # auto-disables this via _paper_floor_enabled() so a fleet-cap
        # clamp can't accidentally over-trade on a live account.
        if not _is_crypto(symbol) and abs(requested_qty) >= 1.0 and _paper_floor_enabled():
            return 1.0, "paper_futures_floor"
        return 0.0, "fleet_exhausted"

    notional_cap = min(per_bot_cap_usd, fleet_remaining)
    if requested_notional <= notional_cap:
        return requested_qty, "ok"

    # capped_qty * entry_price * point_value == notional_cap, so divide
    # back through the multiplier. Earlier code did
    # ``notional_cap / entry_price`` which was missing point_value -- the
    # capped qty came out in index-point units instead of contract units,
    # over-stating the allowed contract count by the multiplier.
    capped_qty = notional_cap / (entry_price * point_value)
    # Round consistently with the supervisor's existing precision.
    if _is_crypto(symbol):
        capped_qty = round(capped_qty, 6)
    else:
        capped_qty = float(int(capped_qty))
        capped_qty = max(capped_qty, 0.0)

    reason = "per_bot_capped" if per_bot_cap_usd <= fleet_remaining else "fleet_capped"

    # Paper-mode minimum-quantity floor for futures contracts.
    # In paper mode the cap is a sanity guardrail, not a real fund
    # constraint. ATR sizing on a $10k cap vs MNQ $14k notional rounds
    # to 0 contracts, killing every entry; symptom in production was
    # 82 APPROVED verdicts on bot.mnq with zero n_entries. Floor lifts
    # capped_qty to 1.0 when the strategy asked for at least 1 contract
    # AND we are in paper mode. ``_paper_floor_enabled()`` short-circuits
    # to False on live money so the cap stays hard there -- no silent
    # 50x over-risk if a live deploy forgets to flip an env var.
    if not _is_crypto(symbol) and abs(requested_qty) >= 1.0 and capped_qty < 1.0 and _paper_floor_enabled():
        return 1.0, "paper_futures_floor"

    return capped_qty, reason
