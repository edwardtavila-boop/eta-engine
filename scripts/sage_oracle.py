"""Sage oracle — consult every school for each bot and surface the truth.

For each bot in the registry (or a subset), load recent bars from the
data cache, build a MarketContext for the bot's planned direction, and
consult all 24 Sage schools. Output:

  * Composite bias + conviction + alignment_score
  * Per-school verdict (bias, conviction, 1-line rationale)
  * Schools ranked by conviction (high → low)
  * Disagreement signal (which schools dissent on direction)
  * Edge-tracker history (hit_rate, expectancy) per school if available

Use this BEFORE applying parameter retunes — if Sage's high-conviction
schools all disagree with the bot's planned long bias, that's a signal
the bot's direction itself is wrong, not just its stop multiplier.

Usage:
    python -m eta_engine.scripts.sage_oracle --bot btc_optimized
    python -m eta_engine.scripts.sage_oracle --diamonds
    python -m eta_engine.scripts.sage_oracle --all --json
    python -m eta_engine.scripts.sage_oracle --bot btc_hybrid --schools wyckoff,smc_ict,trend_following
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Bar data lookup. Falls back gracefully when a symbol's data is missing.
_BAR_PATHS = {
    # Crypto spot (Coinbase / IBKR history)
    ("BTC", "1h"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\BTC_1h.csv"),
    ("BTC", "5m"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\BTC_5m.csv"),
    ("BTC", "1m"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\BTC_1m.csv"),
    ("BTC", "1d"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\BTC_D.csv"),
    ("ETH", "1h"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\ETH_1h.csv"),
    ("ETH", "5m"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\ETH_5m.csv"),
    ("ETH", "1d"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\ETH_D.csv"),
    ("SOL", "1h"): Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\SOL_1h.csv"),
    # Equity-index futures
    ("MNQ", "5m"): Path(r"C:\EvolutionaryTradingAlgo\data\MNQ_5m.csv"),
    ("MNQ1", "5m"): Path(r"C:\EvolutionaryTradingAlgo\data\MNQ_5m.csv"),
    ("NQ", "5m"): Path(r"C:\EvolutionaryTradingAlgo\data\NQ_5m.csv"),
    ("NQ1", "5m"): Path(r"C:\EvolutionaryTradingAlgo\data\NQ_5m.csv"),
}


def _resolve_root() -> Path:
    """Project data root."""
    return Path(r"C:\EvolutionaryTradingAlgo\data")


def _resolve_bar_path(symbol: str, timeframe: str) -> Path | None:
    """Find a bar file for (symbol, timeframe). Tries exact match, then
    common alternates (BTC1h ↔ BTC/1h, MNQ1 ↔ MNQ).

    For commodities (GC, CL, NG, ZN, ZB, 6E, etc.) the explicit
    _BAR_PATHS dict doesn't have entries, so we fall through to the
    glob search across both /data and /data/crypto/ibkr/history. Any
    file matching SYMBOL_TF.csv (case-insensitive symbol root) wins.
    """
    for sym_key in (symbol.upper(), symbol.upper().rstrip("0123456789")):
        for tf_key in (timeframe, timeframe.replace("h", "h"), timeframe):
            p = _BAR_PATHS.get((sym_key, tf_key))
            if p and p.exists():
                return p
    # last-ditch: any csv matching SYMBOL_TF in standard data dirs
    _roots = (
        Path(r"C:\EvolutionaryTradingAlgo\data"),
        Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history"),
        Path(r"C:\EvolutionaryTradingAlgo\data\futures"),
        Path(r"C:\EvolutionaryTradingAlgo\data\futures\ibkr\history"),
    )
    sym_root = symbol.upper().rstrip("0123456789")
    for root in _roots:
        if not root.exists():
            continue
        candidates = list(root.glob(f"{sym_root}_{timeframe}.csv"))
        if candidates:
            return candidates[0]
        # Also try MNQ1, NQ1 etc. variants as filename
        candidates = list(root.glob(f"{symbol.upper()}_{timeframe}.csv"))
        if candidates:
            return candidates[0]
    return None


def _load_bars(symbol: str, timeframe: str, limit: int = 300) -> list[dict[str, Any]]:
    """Load the last ``limit`` bars from a CSV. Returns plain dicts with
    ``open/high/low/close/volume`` so Sage can consume them directly."""
    path = _resolve_bar_path(symbol, timeframe)
    if path is None:
        return []
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    except OSError:
        return []
    bars: list[dict[str, Any]] = []
    for r in rows[-limit:]:
        with contextlib.suppress(KeyError, ValueError, TypeError):
            # Cover the three timestamp-column conventions seen in this
            # repo's CSVs: ts (ISO string), datetime/timestamp (ISO string),
            # and time (Unix epoch seconds). Convert pure-digit strings
            # to int so seasonality's _bar_timestamp_utc takes the epoch
            # branch (datetime.fromtimestamp) instead of failing on
            # fromisoformat("1715403600").
            ts_raw: Any = (
                r.get("ts") or r.get("timestamp")
                or r.get("datetime") or r.get("time") or ""
            )
            if isinstance(ts_raw, str) and ts_raw.isdigit():
                ts_raw = int(ts_raw)
            bars.append({
                "ts": ts_raw,
                "open": float(r.get("open") or r.get("Open") or 0),
                "high": float(r.get("high") or r.get("High") or 0),
                "low": float(r.get("low") or r.get("Low") or 0),
                "close": float(r.get("close") or r.get("Close") or 0),
                "volume": float(r.get("volume") or r.get("Volume") or 0),
            })
    return bars


_CRYPTO_ROOTS_LOCAL = frozenset({
    "BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "XRP", "MBT", "MET",
})
_FUTURES_ROOTS_LOCAL = frozenset({
    # Equity-index
    "MNQ", "NQ", "ES", "MES", "RTY", "M2K", "YM", "MYM",
    # Energies
    "CL", "MCL", "NG", "RB", "HO", "BZ",
    # Metals
    "GC", "MGC", "SI", "SIL", "HG", "PA", "PL",
    # Rates
    "ZN", "ZB", "ZF", "ZT",
    # FX (full + micros)
    "6E", "M6E", "6A", "M6A", "6B", "M6B", "6J", "M6J", "6C", "6N", "6S",
    # Grains
    "ZC", "ZS", "ZW", "ZL", "ZM",
    # Softs
    "KC", "SB", "CC", "CT",
    # Livestock
    "LE", "HE",
})


def _instrument_class(symbol: str) -> str:
    s = symbol.upper().rstrip("0123456789")
    if s in _CRYPTO_ROOTS_LOCAL:
        return "crypto"
    if s in _FUTURES_ROOTS_LOCAL:
        return "futures"
    return "other"


def consult_for_bot(
    bot_id: str,
    side: str = "long",
    enabled: set[str] | None = None,
) -> dict[str, Any]:
    """Build a MarketContext for ``bot_id`` and run consult_sage()."""
    try:
        from eta_engine.brain.jarvis_v3.sage.base import MarketContext
        from eta_engine.brain.jarvis_v3.sage.consultation import consult_sage
        from eta_engine.brain.jarvis_v3.sage.edge_tracker import default_tracker
        from eta_engine.strategies.per_bot_registry import get_for_bot
    except ImportError as exc:
        return {"error": f"sage import failed: {exc}"}

    assignment = get_for_bot(bot_id)
    if assignment is None:
        return {"error": f"bot_id {bot_id} not found in registry"}

    bars = _load_bars(assignment.symbol, assignment.timeframe, limit=300)
    if not bars:
        return {
            "bot_id": bot_id,
            "symbol": assignment.symbol,
            "timeframe": assignment.timeframe,
            "error": f"no bars at {assignment.symbol}/{assignment.timeframe}",
        }

    # Build peer_returns from same-class siblings so cross_asset_correlation
    # school can produce meaningful alignment instead of returning neutral.
    peer_returns: dict[str, list[float]] = {}
    self_class = _instrument_class(assignment.symbol)
    self_root = assignment.symbol.upper().rstrip("0123456789")
    peer_pool = ("BTC", "ETH", "SOL") if self_class == "crypto" else (
        ("MNQ", "NQ", "ES") if self_class == "futures" else ()
    )
    for peer_sym in peer_pool:
        if peer_sym == self_root:
            continue
        peer_bars = _load_bars(peer_sym, assignment.timeframe, limit=60)
        if len(peer_bars) < 5:
            continue
        closes = [b["close"] for b in peer_bars if b.get("close")]
        rets = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        if rets:
            peer_returns[peer_sym] = rets

    last_close = bars[-1]["close"]
    ctx = MarketContext(
        bars=bars,
        side=side,
        entry_price=last_close,
        symbol=assignment.symbol,
        instrument_class=_instrument_class(assignment.symbol),
        peer_returns=peer_returns or None,
    )

    try:
        report = consult_sage(ctx, enabled=enabled, parallel=False, use_cache=False)
    except Exception as exc:  # noqa: BLE001
        return {
            "bot_id": bot_id,
            "symbol": assignment.symbol,
            "error": f"consult_sage raised: {exc}",
        }

    # Surface curated disagreement patterns (e.g. dow_long+wyckoff_short
    # = structural uptrend topping → defer). The catalog lives in
    # sage/disagreement.py and is wired into jarvis_full as of this
    # session — exposing it here lets the operator see which clashes are
    # firing live without diffing the verdict log.
    clashes_data: list[dict[str, Any]] = []
    clash_modifier = "no_change"
    clash_cap_mult = 1.0
    try:
        from eta_engine.brain.jarvis_v3.sage.disagreement import (
            detect_clashes,
            strongest_clash_modifier,
        )
        matches = detect_clashes(report)
        for c in matches:
            clashes_data.append({
                "name": c.name,
                "school_a": c.school_a,
                "bias_a": c.bias_a.value,
                "school_b": c.school_b,
                "bias_b": c.bias_b.value,
                "interpretation": c.interpretation,
                "verdict_modifier": c.verdict_modifier,
                "cap_mult": c.cap_mult,
            })
        if matches:
            clash_modifier, clash_cap_mult = strongest_clash_modifier(matches)
    except Exception:  # noqa: BLE001
        pass

    # Pull edge-tracker stats for every school the report mentions.
    try:
        tracker = default_tracker()
        edge_snapshot = tracker.snapshot()
    except Exception:  # noqa: BLE001
        edge_snapshot = {}

    schools = []
    for name, v in report.per_school.items():
        edge = edge_snapshot.get(name, {})
        schools.append({
            "school": name,
            "bias": v.bias.value,
            "conviction": round(v.conviction, 4),
            "aligned_with_entry": bool(v.aligned_with_entry),
            "rationale": (v.rationale or "")[:200],
            "edge_n_obs": int(edge.get("n_obs", 0)),
            "edge_hit_rate": float(edge.get("hit_rate", 0.5)),
            "edge_expectancy_r": float(edge.get("expectancy", 0.0)),
        })
    schools.sort(key=lambda s: s["conviction"], reverse=True)

    return {
        "bot_id": bot_id,
        "symbol": assignment.symbol,
        "timeframe": assignment.timeframe,
        "n_bars": len(bars),
        "last_close": last_close,
        "side": side,
        "composite_bias": report.composite_bias.value,
        "conviction": round(report.conviction, 4),
        "alignment_score": round(report.alignment_score, 4),
        "consensus_pct": round(report.consensus_pct, 4),
        "schools_consulted": report.schools_consulted,
        "schools_aligned": report.schools_aligned_with_entry,
        "schools_disagreeing": report.schools_disagreeing_with_entry,
        "schools_neutral": report.schools_neutral,
        "schools": schools,
        "clashes": clashes_data,
        "clash_modifier": clash_modifier,
        "clash_cap_mult": clash_cap_mult,
    }


def _print_text(r: dict[str, Any]) -> None:
    if "error" in r:
        print(f"\n!! {r.get('bot_id', '?')}: {r['error']}")
        return

    print("=" * 102)
    print(f" SAGE ORACLE  {r['bot_id']} ({r['symbol']}/{r['timeframe']}) "
          f"side={r['side']}  bars={r['n_bars']}  last={r['last_close']:.4f}")
    print("=" * 102)
    print(f"  composite_bias={r['composite_bias']:<8} "
          f"conviction={r['conviction']:.3f}  "
          f"alignment={r['alignment_score']:.2f}  "
          f"consensus={r['consensus_pct']:.2f}")
    print(f"  schools  consulted={r['schools_consulted']}  "
          f"aligned={r['schools_aligned']}  "
          f"disagreeing={r['schools_disagreeing']}  "
          f"neutral={r['schools_neutral']}")
    print("-" * 102)
    print(f"  {'school':<28} {'bias':<8} {'conv':>5} {'algn':>4} "
          f"{'edge_n':>6} {'hit%':>5} {'exp_R':>6}  rationale")
    print("-" * 102)
    for s in r["schools"]:
        print(
            f"  {s['school']:<28} "
            f"{s['bias']:<8} "
            f"{s['conviction']:>5.2f} "
            f"{('Y' if s['aligned_with_entry'] else 'n'):>4} "
            f"{s['edge_n_obs']:>6} "
            f"{s['edge_hit_rate']*100:>5.1f} "
            f"{s['edge_expectancy_r']:>+6.2f}  "
            f"{(s['rationale'] or '').replace(chr(10), ' ')[:50]}"
        )
    # Top + bottom-confidence dissenters
    high_dissent = [
        s for s in r["schools"]
        if s["bias"] != r["composite_bias"]
        and s["bias"] != "neutral"
        and s["conviction"] >= 0.50
    ]
    if high_dissent:
        print("-" * 102)
        print("  HIGH-CONVICTION DISSENTERS:")
        for s in high_dissent:
            print(f"    [{s['school']}] {s['bias']} (conv={s['conviction']:.2f})  "
                  f"{(s['rationale'] or '')[:70]}")
    # Curated clash patterns (defer/tighten/loosen verdicts)
    clashes = r.get("clashes") or []
    if clashes:
        print("-" * 102)
        print(f"  CLASH PATTERNS ({len(clashes)}) — modifier="
              f"{r.get('clash_modifier')} cap_mult={r.get('clash_cap_mult')}:")
        for c in clashes:
            print(f"    [{c['name']}] {c['school_a']}={c['bias_a']} vs "
                  f"{c['school_b']}={c['bias_b']} → {c['verdict_modifier']}")
            print(f"      {(c['interpretation'] or '')[:90]}")
    print("=" * 102)


_DIAMOND_BOT_IDS = (
    "btc_optimized", "btc_hybrid", "btc_regime_trend_etf", "btc_sage_daily_etf",
    "volume_profile_btc", "btc_hybrid_sage", "funding_rate_btc",
    "btc_crypto_scalp", "vwap_mr_btc", "btc_ensemble_2of3",
    "eth_perp", "eth_sage_daily",
    "volume_profile_mnq", "rsi_mr_mnq",
)


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError, ValueError):
        import sys as _sys
        _sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot", default=None, help="Single bot_id")
    p.add_argument("--diamonds", action="store_true",
                   help="Run on the diamond + post-fix top-earner set")
    p.add_argument("--all", action="store_true",
                   help="Run on every bot in per_bot_registry.ASSIGNMENTS")
    p.add_argument("--side", default="long", choices=("long", "short"),
                   help="Proposed entry side (default long)")
    p.add_argument("--schools", default=None,
                   help="Comma-separated school NAMEs to limit to")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.all:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS
        targets = tuple(a.bot_id for a in ASSIGNMENTS)
    elif args.diamonds:
        targets = _DIAMOND_BOT_IDS
    elif args.bot:
        targets = (args.bot,)
    else:
        p.error("specify --bot <id>, --diamonds, or --all")
        return 1

    enabled: set[str] | None = None
    if args.schools:
        enabled = {s.strip() for s in args.schools.split(",") if s.strip()}

    results = [consult_for_bot(b, side=args.side, enabled=enabled) for b in targets]

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            _print_text(r)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
