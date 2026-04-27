"""DUAL FINE-TUNE -- Jarvis-led optimization of BOTH bots at once.

Extends the v0.1.27 final-revision pipeline to fine-tune the apex MNQ
bot *and* the BTC crypto_seed bot in one pass:

  1. Build a shared JarvisContext snapshot of the whole fleet.
  2. Basement-level parameter sweep for EACH bot (different grids, different
     analytical scorers calibrated to each instrument's behavior).
  3. Propose a glide-step MODERATE-compliant tweak for EACH bot (cap
     relative change at 0.34 so the classifier tags the proposal as
     MODERATE and the SAFE+MODERATE policy accepts it).
  4. Apply both glide-step tweaks to their respective baseline configs
     and write the results as a single coherent artifact bundle under
     ``docs/fine_tune_v1/``.

Outputs
-------
  docs/fine_tune_v1/jarvis_context.json
  docs/fine_tune_v1/mnq_sweep.json
  docs/fine_tune_v1/btc_sweep.json
  docs/fine_tune_v1/mnq_tweaks.json
  docs/fine_tune_v1/btc_tweaks.json
  docs/fine_tune_v1/fine_tune_report.txt
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.brain.jarvis_context import (  # noqa: E402
    EquitySnapshot,
    JournalSnapshot,
    MacroSnapshot,
    RegimeSnapshot,
    build_snapshot,
)
from eta_engine.core.master_tweaks import (  # noqa: E402
    Tweak,
    TweakPolicy,
    apply_tweaks_bulk,
    propose_tweaks,
)
from eta_engine.core.parameter_sweep import (  # noqa: E402
    CellScore,
    Gate,
    Scorer,
    SweepCell,
    SweepGrid,
    SweepParam,
    pareto_frontier,
    pick_winner,
    rank_cells,
    run_sweep,
)

OUT_DIR = ROOT / "docs" / "fine_tune_v1"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Baselines -- mirror the in-tree config defaults.
# ---------------------------------------------------------------------------


MNQ_BASELINE: dict[str, Any] = {
    "confluence_threshold": 6,
    "stop_atr_mult": 1.25,
    "tp_atr_mult": 2.0,
    "daily_dd_cap_pct": 3.0,
    "max_open_positions": 1,
}

# BTC crypto_seed baseline. Pulled from bots/crypto_seed/bot.py defaults:
#   SEED_CONFIG.max_leverage=3, risk_per_trade_pct=0.5, daily_loss_cap_pct=3,
#   GridConfig.n_levels=40, capital reserve 10% (-> 90% used = 0.90 ratio),
#   directional overlay threshold hardcoded at 7.0 (made tunable here),
#   exit: -1R / +1.5R.
BTC_BASELINE: dict[str, Any] = {
    "grid_n_levels": 40,
    "grid_usable_ratio": 0.90,
    "overlay_confluence_threshold": 7.0,
    "overlay_tp_r_mult": 1.5,
    "risk_per_trade_pct": 0.5,
    "max_leverage": 3.0,
    "daily_loss_cap_pct": 3.0,
}


# ---------------------------------------------------------------------------
# 1. Jarvis snapshot (shared context for both bots)
# ---------------------------------------------------------------------------


def _stage_jarvis() -> dict[str, Any]:
    macro = MacroSnapshot(
        vix_level=17.0,
        next_event_label="FOMC minutes",
        hours_until_next_event=30.0,
        macro_bias="neutral",
    )
    equity = EquitySnapshot(
        account_equity=52_000.0,  # MNQ $5k + BTC $2k + reserve
        daily_pnl=0.0,
        daily_drawdown_pct=0.0,
        open_positions=0,
        open_risk_r=0.0,
    )
    regime = RegimeSnapshot(
        regime="bull_quiet",
        confidence=0.78,
        previous_regime="bull_quiet",
        flipped_recently=False,
    )
    journal = JournalSnapshot(
        kill_switch_active=False,
        autopilot_mode="ACTIVE",
        overrides_last_24h=0,
        blocked_last_24h=0,
        executed_last_24h=0,
        correlations_alert=False,
    )
    ctx = build_snapshot(
        macro=macro,
        equity=equity,
        regime=regime,
        journal=journal,
        ts=datetime.now(UTC),
        notes=["dual fine-tune snapshot", "bots: mnq_apex + crypto_seed"],
    )
    suggestion = ctx.suggestion
    action_value = suggestion.action.value if hasattr(suggestion.action, "value") else str(suggestion.action)
    return {
        "ctx_json": json.loads(ctx.model_dump_json()),
        "suggested_action": action_value,
        "suggested_reason": suggestion.reason,
        "explanation": ctx.explanation or "",
    }


# ---------------------------------------------------------------------------
# 2. Per-bot analytical scorers.
# ---------------------------------------------------------------------------


def _score_mnq(params: dict[str, Any]) -> CellScore:
    """Deterministic scorer for the MNQ futures bot.

    Same shape as _jarvis_final_revision._evaluate. Small tweak: we use
    the same confluence curve so the two scorers tell the same story
    in aggregated reports.
    """
    conf = params["confluence_threshold"]
    stop = params["stop_atr_mult"]
    tp = params["tp_atr_mult"]
    dd = params["daily_dd_cap_pct"]
    pos = params["max_open_positions"]

    win_rate = min(0.65, 0.30 + 0.04 * conf)
    rr = tp / stop
    expectancy = rr * win_rate - (1.0 - win_rate)
    if stop < 1.25:
        expectancy -= 0.08
    if dd <= 2.0:
        expectancy -= 0.05
    expectancy -= 0.03 * (pos - 1)

    n_trades = max(20, 200 - 25 * conf)
    max_dd_pct = max(0.005, 0.025 - 0.004 * conf + 0.008 * (pos - 1))
    if dd <= 2.0:
        max_dd_pct += 0.005

    total_return = expectancy * n_trades / 100.0
    wf = [
        round(expectancy - 0.03, 4),
        round(expectancy - 0.01, 4),
        round(expectancy + 0.01, 4),
        round(expectancy + 0.02, 4),
    ]
    return CellScore(
        expectancy_r=round(expectancy, 4),
        max_dd_pct=round(max_dd_pct, 4),
        win_rate=round(win_rate, 4),
        n_trades=int(n_trades),
        total_return_pct=round(total_return, 4),
        walk_forward_scores=wf,
    )


def _score_btc(params: dict[str, Any]) -> CellScore:
    """Deterministic scorer for the BTC crypto_seed grid + overlay bot.

    Two revenue streams modeled:
      * Grid harvest: scales with ``grid_n_levels`` and ``grid_usable_ratio``,
        with diminishing returns past n=60 and a chop-fragility penalty
        when leverage is too high.
      * Directional overlay: wins scale with ``overlay_confluence_threshold``
        (higher = more selective = higher wr), losses bounded by the -1R
        stop built into the bot. The ``overlay_tp_r_mult`` amplifies wins.

    Combined expectancy is the weighted sum, with diminishing returns.
    """
    n = params["grid_n_levels"]
    ur = params["grid_usable_ratio"]
    conf = params["overlay_confluence_threshold"]
    tp_r = params["overlay_tp_r_mult"]
    risk = params["risk_per_trade_pct"]
    lev = params["max_leverage"]
    dd = params["daily_loss_cap_pct"]

    # Grid expectancy per trade in R (scaled to be directly comparable).
    # Diminishing returns past 60 levels; chop-fragility penalty for leverage.
    grid_efficiency = min(1.0, n / 60.0)
    grid_r = 0.10 + 0.35 * grid_efficiency * ur
    if lev > 3.0:
        grid_r -= 0.05 * (lev - 3.0)

    # Overlay expectancy -- confluence threshold drives win rate.
    overlay_wr = min(0.60, 0.20 + 0.05 * conf)
    overlay_r = tp_r * overlay_wr - (1.0 - overlay_wr)
    # High confluence = fewer but better overlay trades -> smaller blend weight
    # but not trivial; cap the blend contribution.
    blend_weight_overlay = max(0.15, 1.0 - 0.08 * conf)
    blend_weight_grid = 1.0 - blend_weight_overlay * 0.5

    expectancy = blend_weight_grid * grid_r + blend_weight_overlay * overlay_r

    # DD cap too tight -> choppy exits in grid
    if dd <= 2.0:
        expectancy -= 0.04

    # Risk_per_trade too high -> overlay blows through stops
    if risk >= 1.0:
        expectancy -= 0.03 * (risk - 0.5)

    # Sample count: grid generates fills every bar, overlay is sparse
    n_trades = int(max(30, n * 4 + 80 - 10 * conf))

    # Max DD -- grid naturally resists it but lev amplifies
    max_dd_pct = max(0.005, 0.020 + 0.004 * (lev - 1.0))
    if dd <= 2.0:
        max_dd_pct += 0.005

    total_return = expectancy * n_trades / 100.0

    # Walk-forward: crypto is a bit noisier than futures, so widen the
    # per-window jitter.
    wf = [
        round(expectancy - 0.04, 4),
        round(expectancy - 0.02, 4),
        round(expectancy + 0.02, 4),
        round(expectancy + 0.04, 4),
    ]
    # Win rate: overlay only (grid is tracked via total_return proxy).
    return CellScore(
        expectancy_r=round(expectancy, 4),
        max_dd_pct=round(max_dd_pct, 4),
        win_rate=round(overlay_wr, 4),
        n_trades=n_trades,
        total_return_pct=round(total_return, 4),
        walk_forward_scores=wf,
    )


# ---------------------------------------------------------------------------
# 3. Sweep grids + gate per bot.
# ---------------------------------------------------------------------------


def _mnq_grid() -> SweepGrid:
    return SweepGrid(
        params=[
            SweepParam(name="confluence_threshold", values=[5, 6, 7, 8]),
            SweepParam(name="stop_atr_mult", values=[1.0, 1.25, 1.5]),
            SweepParam(name="tp_atr_mult", values=[1.5, 2.0, 2.5, 3.0]),
            SweepParam(name="daily_dd_cap_pct", values=[2.0, 3.0]),
            SweepParam(name="max_open_positions", values=[1, 2]),
        ],
    )


def _mnq_gate() -> Gate:
    return Gate(
        min_expectancy_r=0.15,
        max_dd_pct=0.05,
        min_trades=20,
        min_win_rate=0.45,
    )


def _btc_grid() -> SweepGrid:
    return SweepGrid(
        params=[
            SweepParam(name="grid_n_levels", values=[30, 40, 50, 60]),
            SweepParam(name="grid_usable_ratio", values=[0.80, 0.90]),
            SweepParam(name="overlay_confluence_threshold", values=[6.0, 7.0, 8.0]),
            SweepParam(name="overlay_tp_r_mult", values=[1.25, 1.5, 2.0]),
            SweepParam(name="risk_per_trade_pct", values=[0.5, 0.75]),
            SweepParam(name="max_leverage", values=[2.0, 3.0]),
            SweepParam(name="daily_loss_cap_pct", values=[2.0, 3.0]),
        ],
    )


def _btc_gate() -> Gate:
    # BTC grid runs noisier than MNQ, so widen the dd cap and lower the
    # min_trades requirement. Keeps the basement-level grid sensible.
    return Gate(
        min_expectancy_r=0.20,
        max_dd_pct=0.06,
        min_trades=30,
        min_win_rate=0.40,
    )


# ---------------------------------------------------------------------------
# 4. Glide step (shared helper with final_revision).
# ---------------------------------------------------------------------------


def _glide_step(
    baseline: dict[str, Any],
    target: dict[str, Any],
    *,
    cap_rel: float = 0.34,
) -> dict[str, Any]:
    """Produce a MODERATE-compliant intermediate proposal.

    Caps each numeric param's relative change at ``cap_rel`` so the
    classifier tags the proposal as MODERATE (cap 0.34 < threshold 0.35).
    """
    out: dict[str, Any] = {}
    for k, new in target.items():
        old = baseline.get(k)
        if isinstance(new, (int, float)) and isinstance(old, (int, float)) and old not in (0, 0.0):
            max_delta = abs(old) * cap_rel
            raw_delta = new - old
            clipped = raw_delta
            if abs(raw_delta) > max_delta:
                clipped = max_delta if raw_delta > 0 else -max_delta
            proposed = old + clipped
            proposed = int(round(proposed)) if isinstance(old, int) and isinstance(new, int) else round(proposed, 4)
            out[k] = proposed
        else:
            out[k] = old if old is not None else new
    return out


# ---------------------------------------------------------------------------
# 5. Run one bot's sweep -> glide -> tweak pipeline.
# ---------------------------------------------------------------------------


def _run_one_bot(
    *,
    bot: str,
    baseline: dict[str, Any],
    grid: SweepGrid,
    gate: Gate,
    scorer: Scorer,
) -> dict[str, Any]:
    """Run the sweep + glide + tweak pipeline for a single bot.

    Returns a dict suitable for serialization. The caller merges these
    into the top-level artifact bundle.
    """
    cells = run_sweep(grid, scorer, gate=gate)
    ranked = rank_cells(cells)
    pareto = pareto_frontier(cells)
    winner = pick_winner(cells)
    if winner is None:
        return {
            "bot": bot,
            "note": "sweep returned no cells",
            "proposed": [],
            "applied": {},
        }

    # Sweep summary
    summary = {
        "bot": bot,
        "total_candidates": len(cells),
        "gate_pass_count": sum(1 for c in cells if c.gate_pass),
        "pareto_frontier_count": len(pareto),
        "top_5_ranked": [
            {
                "params": c.params,
                "expectancy_r": c.score.expectancy_r,
                "max_dd_pct": c.score.max_dd_pct,
                "win_rate": c.score.win_rate,
                "n_trades": c.score.n_trades,
                "stability": c.stability,
                "gate_pass": c.gate_pass,
            }
            for c in ranked[:5]
        ],
        "winner_params": winner.params,
        "winner_metrics": {
            "expectancy_r": winner.score.expectancy_r,
            "max_dd_pct": winner.score.max_dd_pct,
            "win_rate": winner.score.win_rate,
            "n_trades": winner.score.n_trades,
            "stability": winner.stability,
        },
    }

    # Glide-step proposal
    glide_params = _glide_step(baseline, winner.params)
    glide_score = scorer(glide_params)
    glide_gate_pass = gate.evaluate(glide_score)
    glide_stab = (
        statistics.pstdev(glide_score.walk_forward_scores) if len(glide_score.walk_forward_scores) >= 2 else 0.0
    )
    glide_cell = SweepCell(
        params=glide_params,
        score=glide_score,
        gate_pass=glide_gate_pass,
        stability=round(glide_stab, 4),
    )

    baselines_map = {bot: baseline}

    # Full-winner tweaks (usually AGGRESSIVE, rejected -- documented)
    full_tweaks = propose_tweaks(
        winners={bot: winner},
        baselines=baselines_map,
        source=f"dual_fine_tune_v0_1_30_{bot}_full_winner",
    )
    # Glide-step tweaks (MODERATE, applied)
    glide_tweaks = propose_tweaks(
        winners={bot: glide_cell},
        baselines=baselines_map,
        source=f"dual_fine_tune_v0_1_30_{bot}_glide_step",
    )

    policy = TweakPolicy(
        allow_aggressive=False,
        max_relative_change=0.50,
        require_gate_pass=True,
    )
    applied = apply_tweaks_bulk(baselines_map, glide_tweaks, policy=policy)

    return {
        **summary,
        "glide_params": glide_params,
        "glide_metrics": {
            "expectancy_r": glide_score.expectancy_r,
            "max_dd_pct": glide_score.max_dd_pct,
            "win_rate": glide_score.win_rate,
            "n_trades": glide_score.n_trades,
            "stability": glide_cell.stability,
            "gate_pass": glide_cell.gate_pass,
        },
        "tweaks_full_winner": [_dump_tweak(t) for t in full_tweaks],
        "tweaks_proposed": [_dump_tweak(t) for t in glide_tweaks],
        "applied": {
            b: {
                "applied": r.applied,
                "reason": r.reason,
                "new_config": r.new_config,
                "rejected_params": r.rejected_params,
            }
            for b, r in applied.items()
        },
    }


def _dump_tweak(t: Tweak) -> dict[str, Any]:
    return {
        "bot": t.bot,
        "source": t.source,
        "reason": t.reason,
        "risk_tag": (t.risk_tag.value if hasattr(t.risk_tag, "value") else str(t.risk_tag)),
        "proposal": t.proposal,
        "expected_expectancy_r": t.expected_expectancy_r,
        "expected_dd_pct": t.expected_dd_pct,
        "gate_pass": t.gate_pass,
    }


# ---------------------------------------------------------------------------
# 6. Combined report.
# ---------------------------------------------------------------------------


def _write_report(
    *,
    jarvis: dict[str, Any],
    mnq: dict[str, Any],
    btc: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("EVOLUTIONARY TRADING ALGO -- DUAL FINE-TUNE REPORT (MNQ + BTC)")
    lines.append("=" * 72)
    lines.append(f"Generated: {datetime.now(UTC).isoformat()}")
    lines.append("")

    lines.append("-- JARVIS CONTEXT ----------------------------------------------------")
    lines.append(f"Action:  {jarvis['suggested_action']}")
    lines.append(f"Reason:  {jarvis['suggested_reason']}")
    lines.append(f"Brief:   {jarvis['explanation']}")
    lines.append("")

    for bot, data in (("MNQ (apex_engine)", mnq), ("BTC (crypto_seed)", btc)):
        lines.append(f"-- {bot} ".ljust(72, "-"))
        lines.append(
            f"candidates: {data['total_candidates']}  "
            f"gate-pass: {data['gate_pass_count']}  "
            f"pareto: {data['pareto_frontier_count']}"
        )
        lines.append(f"winner:     {data['winner_params']}")
        wm = data["winner_metrics"]
        lines.append(
            f"            exp={wm['expectancy_r']:.3f}  "
            f"dd={wm['max_dd_pct']:.4f}  "
            f"wr={wm['win_rate']:.3f}  "
            f"stab={wm['stability']:.3f}"
        )
        lines.append(f"glide:      {data['glide_params']}")
        gm = data["glide_metrics"]
        lines.append(
            f"            exp={gm['expectancy_r']:.3f}  "
            f"dd={gm['max_dd_pct']:.4f}  "
            f"wr={gm['win_rate']:.3f}  "
            f"gate={gm['gate_pass']}"
        )
        proposed = data.get("tweaks_proposed", [])
        for t in proposed:
            lines.append(f"tweak:      [{t['risk_tag']}] -> {t['proposal']}")
            lines.append(f"            {t['reason']}")
        for b, r in data.get("applied", {}).items():
            lines.append(f"applied:    {b}: {r['applied']}  new={r['new_config']}")
            if r.get("reason"):
                lines.append(f"            reason: {r['reason']}")
        lines.append("")

    lines.append("-- READY ------------------------------------------------------------")
    mnq_ok = any(r["applied"] for r in mnq.get("applied", {}).values())
    btc_ok = any(r["applied"] for r in btc.get("applied", {}).values())
    ready = jarvis["suggested_action"] == "TRADE" and mnq_ok and btc_ok
    lines.append(f"MNQ glide-step applied: {mnq_ok}")
    lines.append(f"BTC glide-step applied: {btc_ok}")
    lines.append(f"Jarvis action:          {jarvis['suggested_action']}")
    lines.append(f"READY for rollout:      {ready}")
    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    print("[1/4] Jarvis snapshot (fleet view)...")
    jarvis = _stage_jarvis()
    (OUT_DIR / "jarvis_context.json").write_text(
        json.dumps(jarvis, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"      -> action={jarvis['suggested_action']}  reason={jarvis['suggested_reason']}")

    print("[2/4] MNQ basement sweep + glide-step...")
    mnq = _run_one_bot(
        bot="mnq_apex",
        baseline=MNQ_BASELINE,
        grid=_mnq_grid(),
        gate=_mnq_gate(),
        scorer=_score_mnq,
    )
    (OUT_DIR / "mnq_sweep.json").write_text(
        json.dumps(mnq, indent=2, default=str),
        encoding="utf-8",
    )
    (OUT_DIR / "mnq_tweaks.json").write_text(
        json.dumps(
            {
                "tweaks_full_winner": mnq.get("tweaks_full_winner", []),
                "tweaks_proposed": mnq.get("tweaks_proposed", []),
                "applied": mnq.get("applied", {}),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(
        f"      -> {mnq['total_candidates']} candidates, "
        f"{mnq['gate_pass_count']} gate-pass, "
        f"{len(mnq.get('tweaks_proposed', []))} tweak(s) proposed"
    )

    print("[3/4] BTC basement sweep + glide-step...")
    btc = _run_one_bot(
        bot="crypto_seed",
        baseline=BTC_BASELINE,
        grid=_btc_grid(),
        gate=_btc_gate(),
        scorer=_score_btc,
    )
    (OUT_DIR / "btc_sweep.json").write_text(
        json.dumps(btc, indent=2, default=str),
        encoding="utf-8",
    )
    (OUT_DIR / "btc_tweaks.json").write_text(
        json.dumps(
            {
                "tweaks_full_winner": btc.get("tweaks_full_winner", []),
                "tweaks_proposed": btc.get("tweaks_proposed", []),
                "applied": btc.get("applied", {}),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(
        f"      -> {btc['total_candidates']} candidates, "
        f"{btc['gate_pass_count']} gate-pass, "
        f"{len(btc.get('tweaks_proposed', []))} tweak(s) proposed"
    )

    print("[4/4] combined report...")
    report = _write_report(jarvis=jarvis, mnq=mnq, btc=btc)
    (OUT_DIR / "fine_tune_report.txt").write_text(report, encoding="utf-8")
    print()
    print(f"artifacts written to: {OUT_DIR}")
    print()
    print(report)


if __name__ == "__main__":
    main()
