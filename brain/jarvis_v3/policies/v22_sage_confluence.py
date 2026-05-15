"""Candidate policy v22 -- sage confluence modulation (2026-04-27).

Hypothesis
----------
JARVIS's verdict is informed by stress, session, and binding-constraint
heuristics, but doesn't directly factor in multi-school market-theory
confluence. v22 adds the sage report as a modulator on top of v17:

  * If sage CONVICTION is high (>=0.65) AND sage AGREES with the entry
    direction (alignment_score >= 0.7), LOOSEN the cap by 1.2x (allow
    full size when v17 said CONDITIONAL).

  * If sage CONVICTION is high AND sage DISAGREES with the entry
    direction (alignment_score <= 0.3), TIGHTEN the cap to 0.30 OR
    DEFER if v17 already said CONDITIONAL.

  * If sage conviction is LOW (<0.35), DON'T modulate -- the sage
    couldn't reach a consensus, so trust v17.

This is the bridge between v17's risk gating and the sage's
fundamental-school read of the tape. The sage is consulted ONLY when
the bot supplies bars in the request payload (key: ``payload['sage_bars']``)
-- otherwise v22 is identical to v17.

Thresholds are dynamically tuned from EdgeTracker realized-R data when
enough observations exist; fall back to env-var overrides, then to
hardcoded defaults.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    Verdict,
    evaluate_request,
)
from eta_engine.brain.jarvis_v3.candidate_policy import register_candidate

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_context import JarvisContext

logger = logging.getLogger(__name__)

#: Hardcoded defaults (used when no edge-tracker data available)
_SAGE_CONVICTION_FLOOR_DEFAULT: float = 0.35
_SAGE_CONVICTION_HIGH_DEFAULT: float = 0.65
_SAGE_AGREE_THRESHOLD_DEFAULT: float = 0.70
_SAGE_DISAGREE_THRESHOLD_DEFAULT: float = 0.30
_SAGE_DISAGREE_TIGHTEN_CAP_DEFAULT: float = 0.30


def _load_threshold(name: str, default: float) -> float:
    """Load a threshold from env var override, with explicit default."""
    env_key = f"ETA_V22_{name.upper()}"
    if env_key in os.environ:
        try:
            return float(os.environ[env_key])
        except ValueError:
            logger.warning("invalid env var %s, using default %s", env_key, default)
    return default


def get_sage_thresholds() -> dict[str, float]:
    """Return the active v22 sage modulation thresholds.

    Loads from ETA_V22_* env vars with defaults, then applies light
    self-tuning from EdgeTracker aggregate edge data when available.
    """
    c_floor = _load_threshold("sage_conviction_floor", _SAGE_CONVICTION_FLOOR_DEFAULT)
    c_high = _load_threshold("sage_conviction_high", _SAGE_CONVICTION_HIGH_DEFAULT)
    agree = _load_threshold("sage_agree_threshold", _SAGE_AGREE_THRESHOLD_DEFAULT)
    disagree = _load_threshold("sage_disagree_threshold", _SAGE_DISAGREE_THRESHOLD_DEFAULT)
    tighten = _load_threshold("sage_disagree_tighten_cap", _SAGE_DISAGREE_TIGHTEN_CAP_DEFAULT)

    # Self-tune: if edge tracker has >=50 total observations across
    # all schools, nudge thresholds based on aggregate edge.
    try:
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker

        tracker = default_tracker()
        edges = tracker.all_weight_modifiers()
        if edges:
            avg_mod = sum(edges.values()) / len(edges)
            # avg_mod > 1.0 means schools are net positive -> loosen thresholds
            # avg_mod < 1.0 means schools are net negative -> tighten thresholds
            if avg_mod > 1.1:
                c_floor = max(0.20, c_floor - 0.05)
                c_high = max(0.50, c_high - 0.05)
            elif avg_mod < 0.9:
                c_floor = min(0.50, c_floor + 0.05)
                c_high = min(0.80, c_high + 0.05)
    except Exception:  # noqa: BLE001
        pass

    return {
        "conviction_floor": round(c_floor, 3),
        "conviction_high": round(c_high, 3),
        "agree_threshold": round(agree, 3),
        "disagree_threshold": round(disagree, 3),
        "disagree_tighten_cap": round(tighten, 3),
    }


#: Symbol-prefix map for instrument-class auto-detection. Bots can
#: always override by passing ``instrument_class`` in the payload.
_CRYPTO_PREFIXES: tuple[str, ...] = (
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "MATIC",
    "AVAX",
    "LTC",
    "BNB",
    "ATOM",
    "NEAR",
    "DOT",
    "LINK",
    "UNI",
    "MBT",
    "MET",
)
_FUTURES_PREFIXES: tuple[str, ...] = (
    "MNQ",
    "NQ",
    "MES",
    "ES",
    "MGC",
    "GC",
    "MCL",
    "CL",
    "MYM",
    "YM",
    "M2K",
    "RTY",
    "MBT",
    "MET",  # MBT/MET are CME micro crypto futures
)


def _infer_instrument_class(symbol: str) -> str | None:
    """Best-effort instrument class from symbol prefix.

    Returns one of {"crypto", "futures", None}. None when the symbol
    doesn't match any known prefix -- sage schools that gate on
    instrument_class will then run for every class (default behavior).
    """
    if not symbol:
        return None
    s = symbol.upper()
    # Spot/perp crypto first because BTC futures share the BTC prefix
    if (s.endswith("USDT") or s.endswith("USD") or s.endswith("PERP")) and any(
        s.startswith(p) for p in _CRYPTO_PREFIXES
    ):
        return "crypto"
    # CME-style micro / standard futures (including continuous/suffixed roots like MNQ1).
    root = s.rstrip("0123456789")
    if any(
        s == p
        or root == p
        or s.startswith(p + "!")
        or s.startswith(p + "Z")
        or s.startswith(p + "H")
        or s.startswith(p + "M")
        or s.startswith(p + "U")
        for p in _FUTURES_PREFIXES
    ):
        return "futures"
    # Bare spot-ish crypto tickers remain crypto, but MBT/MET are CME micros.
    if s in _CRYPTO_PREFIXES and s not in _FUTURES_PREFIXES:
        return "crypto"
    return None


def evaluate_v22(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """v22: modulate v17 verdicts using multi-school sage confluence."""
    t = get_sage_thresholds()
    base = evaluate_request(req, ctx)
    if base.verdict not in (Verdict.APPROVED, Verdict.CONDITIONAL):
        return base

    # Sage requires bars; bot must supply them in payload['sage_bars']
    sage_bars = req.payload.get("sage_bars") if isinstance(req.payload, dict) else None
    if not sage_bars or not isinstance(sage_bars, list) or len(sage_bars) < 30:
        return base

    side = req.payload.get("side", "long")
    entry_price = float(req.payload.get("entry_price", 0))
    symbol = req.payload.get("symbol", "")

    # Wave-6 pre-live (2026-04-27): infer instrument class from symbol
    # so OnChainSchool / FundingBasisSchool / OptionsGreeksSchool gate
    # correctly. Bots can override by passing instrument_class explicitly.
    instrument_class = req.payload.get("instrument_class") or _infer_instrument_class(symbol)

    # Wave-6 pre-live: pull warm on-chain metrics for BTC + ETH crypto
    # bots so OnChainSchool sees real data instead of returning NEUTRAL.
    # The fetcher caches for 5 min so this is a cheap dict lookup once
    # the warmer task has run. Falls back silently to {} on failure.
    onchain = req.payload.get("onchain") or {}
    funding = req.payload.get("funding") or req.payload.get("funding_basis")
    options = req.payload.get("options") or req.payload.get("options_greeks")
    if instrument_class == "crypto" and not onchain:
        # Check local cache first to avoid HTTP on the hot path.
        # The warm task pre-populates this every 5 min; this is a
        # safety net that only fires when the cache is cold.
        try:
            from eta_engine.brain.jarvis_v3.sage.onchain_fetcher import _CACHE

            cache_key = symbol.upper()[:3]
            entry = _CACHE.get(cache_key)
            if entry is not None and hasattr(entry, "value"):
                onchain = entry.value
        except Exception:  # noqa: BLE001
            pass
        # Fall through to fetch only if cache is truly empty
        if not onchain:
            try:
                from eta_engine.brain.jarvis_v3.sage.onchain_fetcher import fetch_onchain

                onchain = fetch_onchain(symbol) or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("fetch_onchain raised %s (non-fatal)", exc)

    try:
        from eta_engine.brain.jarvis_v3.sage import MarketContext, consult_sage

        m_ctx = MarketContext(
            bars=sage_bars,
            side=side,
            entry_price=entry_price,
            symbol=symbol,
            instrument_class=instrument_class,
            order_book_imbalance=req.payload.get("order_book_imbalance"),
            cumulative_delta=req.payload.get("cumulative_delta"),
            realized_vol=req.payload.get("realized_vol"),
            session_phase=str(base.session_phase) if base.session_phase else None,
            account_equity_usd=req.payload.get("account_equity_usd"),
            risk_per_trade_pct=req.payload.get("risk_per_trade_pct"),
            stop_distance_pct=req.payload.get("stop_distance_pct"),
            onchain=onchain or None,
            funding=funding,
            options=options,
        )
        report = consult_sage(m_ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sage consultation failed (non-fatal): %s", exc)
        return base

    # Wave-6 (2026-04-27): stash the report so the bot's record_fill_outcome
    # can attribute the realized R back to each school via EdgeTracker.
    # Last-write-wins per (symbol, side); read-once on pop.
    try:
        from eta_engine.brain.jarvis_v3.sage.last_report_cache import set_last

        set_last(symbol, side, report)
    except Exception as exc:  # noqa: BLE001
        logger.debug("last_report_cache.set_last raised %s (non-fatal)", exc)

    # Detect named school-vs-school clash patterns for richer reasoning
    clash_modifier = 1.0
    clash_names: list[str] = []
    clash_verdict = None
    try:
        from eta_engine.brain.jarvis_v3.sage.disagreement import (
            detect_clashes,
            strongest_clash_modifier,
        )

        clashes = detect_clashes(report)
        clash_verdict, clash_modifier = strongest_clash_modifier(clashes)
        if clashes:
            clash_names = [c.name for c in clashes]
    except Exception as exc:  # noqa: BLE001
        logger.debug("clash detection failed (non-fatal): %s", exc)

    # Below conviction floor -> don't modulate
    if report.conviction < t["conviction_floor"]:
        return base

    # High-conviction agreement -> loosen
    if (
        report.conviction >= t["conviction_high"]
        and report.alignment_score >= t["agree_threshold"]
        and base.verdict == Verdict.CONDITIONAL
    ):
        # Loosen the cap (boost up to 1.0)
        new_cap = min(1.0, (base.size_cap_mult or 0.5) * 1.2)
        return base.model_copy(
            update={
                "size_cap_mult": new_cap,
                "verdict": Verdict.APPROVED if new_cap >= 1.0 else base.verdict,
                "reason": f"{base.reason} [v22 sage agrees ({report.summary_line()}) -> loosen]",
                "conditions": [*base.conditions, "v22_sage_loosened"],
            }
        )

    # High-conviction disagreement -> tighten or defer, enriched by clash patterns
    if report.conviction >= t["conviction_high"] and report.alignment_score <= t["disagree_threshold"]:
        cap = t["disagree_tighten_cap"] * clash_modifier
        clash_note = f", clash={','.join(clash_names)}" if clash_names else ""
        if base.verdict == Verdict.APPROVED:
            return base.model_copy(
                update={
                    "verdict": Verdict.CONDITIONAL,
                    "size_cap_mult": cap,
                    "reason": (
                        f"{base.reason} [v22 sage disagrees strongly "
                        f"({report.summary_line()}) -> downgrade APPROVED to CONDITIONAL@{cap:.2f}{clash_note}]"
                    ),
                    "conditions": [*base.conditions, "v22_sage_disagree_tighten"],
                }
            )
        else:  # CONDITIONAL
            return base.model_copy(
                update={
                    "verdict": Verdict.DEFERRED,
                    "size_cap_mult": 0.0,
                    "reason": (
                        f"{base.reason} [v22 sage disagrees strongly + already CONDITIONAL "
                        f"-> DEFER ({report.summary_line()}){clash_note}]"
                    ),
                    "conditions": [*base.conditions, "v22_sage_disagree_defer"],
                }
            )

    # Mid-range or split -> no modulation
    return base


register_candidate(
    "v22",
    evaluate_v22,
    parent_version=17,
    rationale=(
        "modulate v17 verdicts using multi-school sage confluence "
        "(Dow + Wyckoff + Elliott + Fib + S/R + trend + VPA + MP + SMC + "
        "order flow + risk + Gann + NEoWave + Weis-Wyckoff)"
    ),
    metadata={
        **get_sage_thresholds(),
        "kaizen_ticket": "KZN-2026-04-27-sage-confluence-modulation",
        "thresholds_self_tune": True,
    },
    overwrite=True,
)
