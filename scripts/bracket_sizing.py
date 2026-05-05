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
    "MNQ", "NQ", "ES", "MES", "NG", "CL", "GC", "ZN", "ZB",
    "6E", "M6E", "MGC", "MCL", "RTY", "M2K",
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
        float(stop_mult_override) if stop_mult_override is not None
        else float(os.getenv("ETA_BRACKET_ATR_STOP_MULT", "2.0"))
    )
    target_mult = (
        float(target_mult_override) if target_mult_override is not None
        else float(os.getenv("ETA_BRACKET_ATR_TARGET_MULT", "3.0"))
    )
    fallback_stop_pct = float(os.getenv("ETA_BRACKET_FALLBACK_STOP_PCT", "0.015"))
    fallback_target_pct = float(os.getenv("ETA_BRACKET_FALLBACK_TARGET_PCT", "0.020"))

    side_u = side.upper()
    bar_list = list(bars) if bars is not None else []
    atr = compute_atr(bar_list, period=period) if bar_list else None

    if atr is not None and atr > 0:
        if side_u == "BUY":
            stop = entry_price - stop_mult * atr
            target = entry_price + target_mult * atr
        else:
            stop = entry_price + stop_mult * atr
            target = entry_price - target_mult * atr
        return round(stop, 4), round(target, 4), "atr"

    # Fallback: fixed percent
    if side_u == "BUY":
        stop = entry_price * (1.0 - fallback_stop_pct)
        target = entry_price * (1.0 + fallback_target_pct)
    else:
        stop = entry_price * (1.0 + fallback_stop_pct)
        target = entry_price * (1.0 - fallback_target_pct)
    return round(stop, 4), round(target, 4), "fixed_pct"


# ─── Per-class capital budgets ──────────────────────────────────


def _budget_per_bot_usd(symbol: str) -> float:
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


def cap_qty_to_budget(
    *,
    symbol: str,
    entry_price: float,
    requested_qty: float,
    fleet_open_notional_usd: float = 0.0,
) -> tuple[float, str]:
    """Return ``(capped_qty, reason)``.

    Caps the requested qty so neither the per-bot nor the fleet budget
    is exceeded. ``reason`` is one of "ok", "per_bot_capped",
    "fleet_capped", "fleet_exhausted". The supervisor logs the reason
    so the operator can see when budgets are clamping signals.
    """
    if entry_price <= 0:
        return requested_qty, "ok"

    per_bot_cap_usd = _budget_per_bot_usd(symbol)
    fleet_cap_usd = _fleet_budget_usd(symbol)

    requested_notional = abs(requested_qty) * entry_price

    # Fleet budget — what's left after existing open exposure?
    fleet_remaining = max(0.0, fleet_cap_usd - max(0.0, fleet_open_notional_usd))
    if fleet_remaining <= 0:
        return 0.0, "fleet_exhausted"

    notional_cap = min(per_bot_cap_usd, fleet_remaining)
    if requested_notional <= notional_cap:
        return requested_qty, "ok"

    capped_qty = notional_cap / entry_price
    # Round consistently with the supervisor's existing precision.
    if _is_crypto(symbol):
        capped_qty = round(capped_qty, 6)
    else:
        capped_qty = float(int(capped_qty))
        capped_qty = max(capped_qty, 0.0)

    reason = "per_bot_capped" if per_bot_cap_usd <= fleet_remaining else "fleet_capped"

    # Paper-mode minimum-quantity floor for futures contracts.
    # Default per-bot futures budget ($500) divided by MNQ notional
    # ($20000/contract before point_value) rounds to 0 contracts —
    # so EVERY futures entry approved by JARVIS got killed at the cap
    # before any FillRecord could be written. Symptom in production:
    # 82 APPROVED verdicts for bot.mnq, zero n_entries on all 8 MNQ
    # bots. In paper mode the cap is a sanity guard, not a real fund
    # constraint, so floor capped_qty to 1.0 when the operator clearly
    # asked for at least 1 contract. The env var ETA_PAPER_FUTURES_FLOOR
    # (default 1) lets live deployments disable this by setting it to 0.
    if (
        not _is_crypto(symbol)
        and abs(requested_qty) >= 1.0
        and capped_qty < 1.0
        and float(os.getenv("ETA_PAPER_FUTURES_FLOOR", "1")) > 0
    ):
        return 1.0, "paper_futures_floor"

    return capped_qty, reason
