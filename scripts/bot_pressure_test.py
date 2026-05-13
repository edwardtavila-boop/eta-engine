"""Bot pressure test — diagnose what's holding a bot back.

For one bot, surface every concrete diagnostic the system has:

  1. Strategy assignment from per_bot_registry (config + extras)
  2. Lab report (sharpe, WR, expectancy, parameter heatmap, regime
     breakdown, max drawdown)
  3. Live close distribution from trade_closes.jsonl:
       - WR by regime / session / time-of-day
       - Top 10 winning trades, top 10 losing trades
       - Distribution of realized_r (skewness, fat-tail check)
  4. Concrete UPGRADE CANDIDATES — specific parameter changes
     supported by the heatmap data, with expected sharpe delta.

Output is a single page per bot. Operator reads → picks 1-2
upgrades → applies via registry edit → re-runs the lab to verify.

Usage:
    python -m eta_engine.scripts.bot_pressure_test --bot btc_hybrid
    python -m eta_engine.scripts.bot_pressure_test --bot btc_hybrid --json
    python -m eta_engine.scripts.bot_pressure_test --diamonds  # run on all 6 diamond bots
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_LAB_REPORTS_DIR = Path(r"C:\EvolutionaryTradingAlgo\reports\lab_reports")
_TRADE_CLOSES = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl")


_DIAMOND_BOT_IDS = (
    # Original DIAMOND tier (pre-brake-fix lab classification)
    "volume_profile_btc",
    "btc_hybrid",
    "btc_regime_trend_etf",
    "btc_sage_daily_etf",
    "volume_profile_mnq",
    "rsi_mr_mnq",
    # Post-brake-fix top earners (2026-05-04, edge_analyzer --since
    # 2026-05-04T23:31:00). These were classified outside the diamond
    # tier historically but emerged as the strongest dollar producers
    # once R-magnitudes started matching planned bracket distances.
    "btc_hybrid_sage",  # +$18.10 / 3 trades / 100% WR
    "funding_rate_btc",  # +$14.86 / 3 trades / 100% WR
    "btc_optimized",  # +$14.04 / 3 trades / 100% WR
    "btc_crypto_scalp",  # +$7.67  / 4 trades / 75%  WR
    "vwap_mr_btc",  # +$7.14  / 3 trades / 67%  WR
    "eth_sage_daily",  # +$6.28  / 2 trades / 100% WR
)


# ─── Registry ────────────────────────────────────────────────────


def _registry_for(bot_id: str) -> dict[str, Any]:
    try:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS
    except ImportError as exc:
        return {"error": f"registry import failed: {exc}"}
    for a in ASSIGNMENTS:
        if a.bot_id == bot_id:
            return {
                "bot_id": a.bot_id,
                "strategy_id": a.strategy_id,
                "strategy_kind": a.strategy_kind,
                "symbol": a.symbol,
                "timeframe": a.timeframe,
                "rationale": getattr(a, "rationale", ""),
                "extras": dict(getattr(a, "extras", {}) or {}),
            }
    return {"error": f"bot_id {bot_id} not found in registry"}


# ─── Lab report ─────────────────────────────────────────────────


def _lab_for(bot_id: str) -> dict[str, Any]:
    sub = _LAB_REPORTS_DIR / bot_id
    if not sub.exists():
        return {"error": f"no lab dir for {bot_id}"}
    candidates = sorted(sub.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return {"error": "no lab JSONs"}
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"lab read failed: {exc}"}


# ─── Live closes ────────────────────────────────────────────────


def _live_closes_for(bot_id: str) -> list[dict[str, Any]]:
    if not _TRADE_CLOSES.exists():
        return []
    out = []
    try:
        with _TRADE_CLOSES.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("bot_id") == bot_id:
                    out.append(rec)
    except OSError:
        pass
    return out


def _live_metrics(closes: list[dict[str, Any]]) -> dict[str, Any]:
    rs: list[float] = []
    by_regime: dict[str, list[float]] = defaultdict(list)
    by_session: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    actions: Counter = Counter()

    for c in closes:
        with contextlib.suppress(TypeError, ValueError):
            r = float(c.get("realized_r") or 0)
            rs.append(r)
            by_regime[str(c.get("regime") or "unknown")].append(r)
            by_session[str(c.get("session") or "unknown")].append(r)
            actions[c.get("action_taken", "?")] += 1
            ts = c.get("ts") or c.get("close_ts") or ""
            try:
                hour = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).hour
                by_hour[hour].append(r)
            except (ValueError, TypeError):
                pass

    n = len(rs)
    if n == 0:
        return {"n": 0}

    mean_r = sum(rs) / n
    std_r = math.sqrt(sum((r - mean_r) ** 2 for r in rs) / n) if n > 1 else 0.0
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0
    skew = statistics.mean([(r - mean_r) ** 3 for r in rs]) / (std_r**3) if std_r > 0 else 0.0

    def _cell(group: dict, label: str) -> dict:
        return {
            label: {
                k: {
                    "n": len(v),
                    "mean_r": round(sum(v) / len(v), 4) if v else 0.0,
                    "wr": round(sum(1 for r in v if r > 0) / len(v), 4) if v else 0.0,
                }
                for k, v in group.items()
            }
        }

    return {
        "n": n,
        "sharpe": round(sharpe, 3),
        "expectancy_r": round(mean_r, 4),
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "std_r": round(std_r, 4),
        "skew": round(skew, 4),
        "max_loss_r": round(min(rs), 4) if rs else 0.0,
        "max_win_r": round(max(rs), 4) if rs else 0.0,
        **_cell(dict(by_regime), "by_regime"),
        **_cell(dict(by_session), "by_session"),
        **_cell({str(k): v for k, v in by_hour.items()}, "by_hour"),
        "actions_taken": dict(actions),
    }


# ─── Upgrade candidates ─────────────────────────────────────────


def _detect_upgrades(lab: dict[str, Any], live: dict[str, Any], extras: dict) -> list[dict[str, Any]]:
    """Concrete parameter improvement candidates with quantified reasoning."""
    out: list[dict[str, Any]] = []

    # 1. Stop ATR — pick the heatmap row with the best sharpe
    heatmap = lab.get("parameter_heatmap") or {}
    stop_rows = [(k, v) for k, v in heatmap.items() if k.startswith("stop_atr_")]
    if stop_rows:
        # Find current
        current_atr = None
        sub_extras = extras.get("sub_strategy_extras") or {}
        co_extras = extras.get("crypto_orb_config") or {}
        sc_extras = extras.get("scorecard_config") or {}
        for src in (sub_extras, co_extras, sc_extras, extras):
            v = src.get("atr_stop_mult") or src.get("stop_atr")
            if v is not None:
                current_atr = float(v)
                break

        # Best sharpe row
        best_row = max(stop_rows, key=lambda kv: float(kv[1].get("sharpe", 0) or 0))
        best_atr = float(best_row[0].replace("stop_atr_", "").rstrip("x"))
        best_sharpe = float(best_row[1].get("sharpe", 0))

        if current_atr is not None and abs(best_atr - current_atr) > 0.05:
            cur_sharpe = 0.0
            for k, v in stop_rows:
                if abs(float(k.replace("stop_atr_", "").rstrip("x")) - current_atr) < 0.05:
                    cur_sharpe = float(v.get("sharpe", 0))
                    break
            out.append(
                {
                    "param": "atr_stop_mult",
                    "current": current_atr,
                    "suggested": best_atr,
                    "expected_sharpe_delta": round(best_sharpe - cur_sharpe, 4),
                    "rationale": (
                        f"heatmap shows sharpe={best_sharpe:.2f} at {best_atr}x "
                        f"vs {cur_sharpe:.2f} at current {current_atr}x"
                    ),
                }
            )

    # 2. Win rate uplift via min_wick_pct or min_volume_z if WR < 45%
    lab_wr = float(lab.get("win_rate", 0))
    if lab_wr < 0.45:
        cur_wick = (extras.get("sub_strategy_extras") or {}).get("min_wick_pct")
        if cur_wick is not None and float(cur_wick) < 0.40:
            out.append(
                {
                    "param": "min_wick_pct",
                    "current": float(cur_wick),
                    "suggested": round(float(cur_wick) + 0.10, 2),
                    "expected_sharpe_delta": "untested — try +0.10 to filter weaker setups",
                    "rationale": (
                        f"lab WR {lab_wr:.1%} < 45% — tighter wick threshold should "
                        f"reject low-conviction sweeps and lift WR"
                    ),
                }
            )

    # 3. Confluence threshold uplift if min_score is low
    sc = extras.get("scorecard_config") or {}
    cur_min_score = sc.get("min_score")
    if cur_min_score is not None and int(cur_min_score) < 3:
        out.append(
            {
                "param": "scorecard_config.min_score",
                "current": int(cur_min_score),
                "suggested": int(cur_min_score) + 1,
                "expected_sharpe_delta": "untested — pressurize for fewer / higher-conviction trades",
                "rationale": (
                    "raising the confluence min_score reduces trade count but "
                    "should raise per-trade expectancy if signal quality is the limiter"
                ),
            }
        )

    # 4. Live skew check — fat left tail = stop is too wide
    if live.get("n", 0) >= 30:
        skew = float(live.get("skew", 0))
        max_loss = float(live.get("max_loss_r", 0))
        if skew < -0.5 and max_loss < -3.0:
            out.append(
                {
                    "param": "atr_stop_mult",
                    "current": "current",
                    "suggested": "tighten by 0.25x",
                    "expected_sharpe_delta": (
                        f"untested — live max_loss={max_loss:.1f}R, "
                        f"skew={skew:.2f} suggests fat left tail; tighter stop caps it"
                    ),
                    "rationale": (
                        "live distribution has worse-than-normal left tail — tightening "
                        "the stop trades some WR for cap on catastrophic losses"
                    ),
                }
            )

    # 5. Regime concentration — if any regime is significant fraction of losses
    by_regime = live.get("by_regime") or {}
    if isinstance(by_regime, dict):
        for regime, stats in by_regime.items():
            if not isinstance(stats, dict):
                continue
            n = int(stats.get("n", 0))
            wr = float(stats.get("wr", 0))
            mean_r = float(stats.get("mean_r", 0))
            # If a regime has 20+ trades and is losing, suggest a block
            if n >= 20 and (wr < 0.35 or mean_r < -0.10):
                out.append(
                    {
                        "param": "block_regimes",
                        "current": list(extras.get("block_regimes") or ()),
                        "suggested": f"add '{regime}' to block_regimes",
                        "expected_sharpe_delta": (
                            f"removes {n} losing trades (WR={wr:.1%}, mean_r={mean_r:.3f}) from sample"
                        ),
                        "rationale": (
                            f"live: regime '{regime}' has WR {wr:.1%} over {n} trades "
                            f"and mean_r {mean_r:+.3f} — block this regime"
                        ),
                    }
                )

    return out


# ─── Output ─────────────────────────────────────────────────────


def analyze(bot_id: str) -> dict[str, Any]:
    reg = _registry_for(bot_id)
    lab = _lab_for(bot_id)
    closes = _live_closes_for(bot_id)
    live = _live_metrics(closes)
    extras = (reg.get("extras") or {}) if isinstance(reg, dict) else {}
    upgrades = _detect_upgrades(lab, live, extras)
    return {
        "bot_id": bot_id,
        "registry": reg,
        "lab": lab,
        "live": live,
        "upgrades": upgrades,
    }


def _print_text(snap: dict) -> None:
    bid = snap["bot_id"]
    reg = snap["registry"]
    lab = snap["lab"]
    live = snap["live"]
    upgrades = snap["upgrades"]

    print("=" * 78)
    print(f" PRESSURE TEST: {bid}")
    print("=" * 78)

    if reg.get("error"):
        print(f"  registry error: {reg['error']}")
        return

    print("\n* IDENTITY")
    print(f"  strategy_id:   {reg['strategy_id']}")
    print(f"  symbol/tf:     {reg['symbol']}/{reg['timeframe']}")
    print(f"  strategy_kind: {reg['strategy_kind']}")
    print(f"  rationale:     {reg.get('rationale', '')[:150]}...")

    extras = reg.get("extras") or {}
    print("\n* CONFIG (key params)")
    for cfg_name in ("sub_strategy_extras", "crypto_orb_config", "scorecard_config", "confluence_config"):
        cfg = extras.get(cfg_name)
        if isinstance(cfg, dict):
            print(f"  {cfg_name}:")
            for k, v in cfg.items():
                print(f"    {k}: {v}")

    if lab.get("error"):
        print(f"\n* LAB: {lab['error']}")
    else:
        print(f"\n* LAB ({lab.get('total_trades', 0)} trades over {lab.get('coverage_days', 0):.0f} days)")
        print(f"  sharpe:        {float(lab.get('sharpe', 0)):.3f}")
        print(f"  win_rate:      {float(lab.get('win_rate', 0)):.1%}")
        print(f"  expectancy_r:  {float(lab.get('expectancy', 0)):+.4f}")
        print(f"  profit_factor: {float(lab.get('profit_factor', 0)):.2f}")
        print(f"  max_drawdown:  {float(lab.get('max_drawdown', 0)):.1f}R")
        print(f"  avg_win/loss:  {float(lab.get('avg_win', 0)):+.2f}R / {float(lab.get('avg_loss', 0)):.2f}R")

        heatmap = lab.get("parameter_heatmap") or {}
        if heatmap:
            print("  parameter heatmap:")
            for k, v in heatmap.items():
                print(
                    f"    {k:<20} sharpe={float(v.get('sharpe', 0)):>5.2f} "
                    f"wr={float(v.get('win_rate', 0)):>5.1%} "
                    f"n={int(v.get('trades', 0)):>5}"
                )

    print(f"\n* LIVE ({live.get('n', 0)} closes)")
    if live.get("n", 0) > 0:
        print(f"  sharpe:        {float(live.get('sharpe', 0)):.3f}")
        print(f"  win_rate:      {float(live.get('win_rate', 0)):.1%}")
        print(f"  expectancy_r:  {float(live.get('expectancy_r', 0)):+.4f}")
        print(f"  avg_win/loss:  {float(live.get('avg_win', 0)):+.3f}R / {float(live.get('avg_loss', 0)):.3f}R")
        print(f"  std_r:         {float(live.get('std_r', 0)):.4f}")
        print(f"  skew:          {float(live.get('skew', 0)):+.3f}")
        print(f"  max_win/loss:  {float(live.get('max_win_r', 0)):+.2f}R / {float(live.get('max_loss_r', 0)):+.2f}R")
        if live.get("by_regime"):
            print("  by_regime:")
            for k, v in live["by_regime"].items():
                print(f"    {k:<12} n={int(v['n']):>4} wr={float(v['wr']):.1%} mean_r={float(v['mean_r']):+.4f}")
        actions = live.get("actions_taken") or {}
        if actions:
            print(f"  actions:       {dict(actions)}")
    else:
        print("  no closes yet")

    print("\n* UPGRADE CANDIDATES")
    if not upgrades:
        print("  none — bot is at its current local optimum")
    for i, u in enumerate(upgrades, 1):
        print(f"  [{i}] {u['param']}")
        print(f"      current:  {u['current']}")
        print(f"      suggested:{u['suggested']}")
        print(f"      expected: {u.get('expected_sharpe_delta', 'unknown')}")
        print(f"      rationale: {u['rationale']}")

    print("=" * 78)


def _print_fleet_summary(results: list[dict[str, Any]]) -> None:
    """Cross-fleet ranked summary of upgrade candidates.

    Used by --all to surface the highest-impact tunables across every
    registered bot in one screen. Only candidates with a *numeric*
    expected_sharpe_delta are ranked (heatmap-evidenced); the
    "untested — try X" hints are listed below as low-confidence picks.
    """
    print("=" * 102)
    print(f" FLEET PRESSURE TEST — {len(results)} bots")
    print("=" * 102)

    ranked: list[tuple[float, str, dict[str, Any]]] = []
    untested: list[tuple[str, dict[str, Any]]] = []
    no_upgrade: list[str] = []

    for r in results:
        bid = r.get("bot_id", "?")
        ups = r.get("upgrades") or []
        if not ups:
            no_upgrade.append(bid)
            continue
        for u in ups:
            d = u.get("expected_sharpe_delta")
            if isinstance(d, (int, float)):
                ranked.append((float(d), bid, u))
            else:
                untested.append((bid, u))

    ranked.sort(reverse=True)
    print(f"\n* HIGH-CONFIDENCE UPGRADES (heatmap-evidenced) — {len(ranked)}")
    print("-" * 102)
    for delta, bid, u in ranked:
        print(
            f"  +{delta:>5.2f}sh  {bid:<26} {u.get('param', '?'):<30} "
            f"{str(u.get('current', '?')):>6} -> {str(u.get('suggested', '?')):<6}"
        )
        print(f"               rationale: {u.get('rationale', '')}")

    print(f"\n* LOW-CONFIDENCE HINTS (untested) — {len(untested)}")
    print("-" * 102)
    for bid, u in untested[:20]:
        print(
            f"  ?         {bid:<26} {u.get('param', '?'):<30} "
            f"{str(u.get('current', '?')):>6} -> {str(u.get('suggested', '?')):<6}"
        )
    if len(untested) > 20:
        print(f"  ... and {len(untested) - 20} more")

    print(f"\n* AT LOCAL OPTIMUM (no upgrade surfaced) — {len(no_upgrade)}")
    print("-" * 102)
    print("  " + ", ".join(no_upgrade))
    print("=" * 102)


def main(argv: list[str] | None = None) -> int:
    # Windows cp1252 console can't encode every char in registry rationales
    # (e.g. σ, ✓, →). Reconfigure stdout to swap unencodable chars rather
    # than crash the whole --diamonds sweep on the first bot that has one.
    with contextlib.suppress(AttributeError, ValueError):
        import sys as _sys

        _sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot", default=None, help="Single bot_id")
    p.add_argument("--diamonds", action="store_true", help="Run on the 12 DIAMOND tier + post-fix top earners")
    p.add_argument("--all", action="store_true", help="Run on every bot in per_bot_registry.ASSIGNMENTS")
    p.add_argument("--json", action="store_true")
    p.add_argument("--summary", action="store_true", help="Print fleet-ranked summary instead of per-bot pages")
    args = p.parse_args(argv)

    if args.all:
        from eta_engine.strategies.per_bot_registry import ASSIGNMENTS

        targets = tuple(a.bot_id for a in ASSIGNMENTS)
    elif args.diamonds:
        targets = _DIAMOND_BOT_IDS
    elif args.bot:
        targets = (args.bot,)
    else:
        p.error("must specify --bot <id>, --diamonds, or --all")
        return 1

    results = [analyze(b) for b in targets]
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    elif args.summary or args.all:
        _print_fleet_summary(results)
    else:
        for r in results:
            _print_text(r)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
