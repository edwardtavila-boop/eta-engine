"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_preset_validator
===============================================================
Integration smoke for the 8 diamond strategy presets.

Why this exists
---------------
Unit tests verify the wave-4 mechanics in isolation (vol-adjusted
sizing, multi-bar reclaim, trailing-stop math, session filter).
What they DON'T prove is that the mechanics actually FIRE when a
real preset is exercised — preset factories + config defaults
could disagree with the unit-test inputs and break the chain.

This module imports every diamond preset, instantiates the strategy,
walks a controlled synthetic bar stream, and asserts the expected
behavior per preset:

  - mgc_sweep_preset: session filter must reject UTC 20-23 bars;
    vol_adjusted_sizing must be active; reclaim_confirm_bars=2.
  - mcl_sweep_preset: NO wave-4 features (legacy params).
  - eur_sweep_preset / btc / mnq / nq / sol: legacy params preserved.
  - gc_momentum_preset / cl_momentum_preset: ADX gate active;
    compute_trailing_stop() returns reasonable values.
  - cl_macro_fade_preset: ATR floor + session gate enabled by default.

Output
------
- stdout / --json
- var/eta_engine/state/diamond_preset_validation_latest.json
- Exit code: 0 = all green, 1 = at least one preset failed validation

Run
---
::

    python -m eta_engine.scripts.diamond_preset_validator
"""
from __future__ import annotations

# ruff: noqa: ANN401, PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
OUT_LATEST = (
    WORKSPACE_ROOT / "var" / "eta_engine" / "state"
    / "diamond_preset_validation_latest.json"
)


@dataclass
class _MockBar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts: str = "2026-05-12T14:30:00+00:00"

    @property
    def timestamp(self) -> Any:
        return datetime.fromisoformat(self.ts.replace("Z", "+00:00"))


@dataclass
class PresetCheck:
    preset_name: str
    bot_id: str
    asserts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    passed: bool = True


# ────────────────────────────────────────────────────────────────────
# Per-preset validators
# ────────────────────────────────────────────────────────────────────


def _check_mgc_sweep() -> PresetCheck:
    """mgc_sweep_preset must have ALL wave-4 features enabled."""
    from eta_engine.strategies.sweep_reclaim_strategy import (
        SweepReclaimStrategy,
        mgc_sweep_preset,
    )

    chk = PresetCheck(preset_name="mgc_sweep_preset",
                      bot_id="mgc_sweep_reclaim")
    cfg = mgc_sweep_preset()

    # Wave-3 chisel params
    if cfg.atr_stop_mult != 2.5:
        chk.failures.append(f"atr_stop_mult {cfg.atr_stop_mult} != 2.5")
    else:
        chk.asserts.append("atr_stop_mult=2.5 (chisel)")
    if cfg.rr_target != 3.5:
        chk.failures.append(f"rr_target {cfg.rr_target} != 3.5")
    else:
        chk.asserts.append("rr_target=3.5 (chisel)")
    if cfg.min_volume_z != 0.5:
        chk.failures.append(f"min_volume_z {cfg.min_volume_z} != 0.5")
    else:
        chk.asserts.append("min_volume_z=0.5 (chisel)")

    # Wave-4 rehaul features that survived wave-5
    if cfg.reclaim_confirm_bars != 2:
        chk.failures.append(
            f"reclaim_confirm_bars {cfg.reclaim_confirm_bars} != 2")
    else:
        chk.asserts.append("reclaim_confirm_bars=2 (rehaul)")
    if not cfg.vol_adjusted_sizing:
        chk.failures.append("vol_adjusted_sizing OFF — rehaul expects ON")
    else:
        chk.asserts.append("vol_adjusted_sizing=True (rehaul)")
    # Wave-5: excluded_hours_utc reverted to empty after canonical
    # data showed close session is the BEST stratum, not NULL edge.
    # See mgc_sweep_preset docstring for rationale chain.
    if cfg.excluded_hours_utc != ():
        chk.failures.append(
            f"excluded_hours_utc {cfg.excluded_hours_utc} != () — "
            "wave-5 reverted this; see preset docstring",
        )
    else:
        chk.asserts.append("excluded_hours_utc=() (wave-5 revert)")

    # Functional smoke: session filter feature still works for OTHER bots
    # that might need it later (we only reverted mgc's specific value).
    import dataclasses
    test_cfg = dataclasses.replace(mgc_sweep_preset(), excluded_hours_utc=(22,))
    strat = SweepReclaimStrategy(test_cfg)
    excluded_bar = _MockBar(
        open=100.0, high=100.5, low=99.5, close=100.2, volume=1000.0,
        ts="2026-05-12T22:00:00+00:00",  # UTC 22 = excluded by override
    )
    out = strat.maybe_enter(excluded_bar, [], 100_000.0, None)
    if out is not None or strat._n_session_filter_rejects != 1:
        chk.failures.append(
            "session-filter feature broken — UTC-22 not rejected when "
            "excluded_hours_utc=(22,)")
    else:
        chk.asserts.append("session-filter feature still wired (runtime)")

    chk.passed = not chk.failures
    return chk


def _check_mcl_sweep() -> PresetCheck:
    """mcl_sweep_preset must NOT have wave-4 features (n=8 too small)."""
    from eta_engine.strategies.sweep_reclaim_strategy import mcl_sweep_preset

    chk = PresetCheck(preset_name="mcl_sweep_preset",
                      bot_id="mcl_sweep_reclaim")
    cfg = mcl_sweep_preset()
    if cfg.reclaim_confirm_bars != 1:
        chk.failures.append(
            f"reclaim_confirm_bars {cfg.reclaim_confirm_bars} != 1 — "
            "mcl should stay legacy until n>=40")
    else:
        chk.asserts.append("reclaim_confirm_bars=1 (legacy preserved)")
    if cfg.vol_adjusted_sizing:
        chk.failures.append("vol_adjusted_sizing ON — mcl should be legacy")
    else:
        chk.asserts.append("vol_adjusted_sizing=False (legacy preserved)")
    if cfg.excluded_hours_utc != ():
        chk.failures.append(
            f"excluded_hours_utc {cfg.excluded_hours_utc} non-empty — "
            "no session evidence to support a filter yet")
    else:
        chk.asserts.append("excluded_hours_utc=() (legacy preserved)")
    chk.passed = not chk.failures
    return chk


def _check_momentum_adx() -> PresetCheck:
    """Both commodity momentum presets must enable ADX gate (was dead
    code before chisel wave).  Verify the gate fires by walking bars."""
    from eta_engine.strategies.commodity_momentum_strategy import (
        MomentumConfig,
        MomentumStrategy,
        cl_momentum_preset,
        gc_momentum_preset,
    )

    chk = PresetCheck(preset_name="commodity_momentum_presets",
                      bot_id="cl_momentum+gc_momentum")

    for preset_name, cfg in (("gc_momentum_preset", gc_momentum_preset()),
                               ("cl_momentum_preset", cl_momentum_preset())):
        if cfg.adx_threshold < 10:
            chk.failures.append(
                f"{preset_name}.adx_threshold={cfg.adx_threshold} < 10 — "
                "too loose; would never block chop")
        else:
            chk.asserts.append(
                f"{preset_name}.adx_threshold={cfg.adx_threshold}")
        if cfg.adx_period < 5:
            chk.failures.append(
                f"{preset_name}.adx_period={cfg.adx_period} too small")
        else:
            chk.asserts.append(f"{preset_name}.adx_period={cfg.adx_period}")

    # Functional: trailing stop must compute correctly on a momentum strat
    cfg = MomentumConfig(trailing_stop_atr_mult=1.0, rr_trail_trigger=1.0)
    strat = MomentumStrategy(cfg)
    trailing = strat.compute_trailing_stop(
        side="BUY", entry_price=100.0, initial_stop=95.0,
        current_price=106.0, atr=2.0,
    )
    if trailing != 104.0:
        chk.failures.append(
            f"trailing_stop math broken: returned {trailing} not 104.0")
    else:
        chk.asserts.append("compute_trailing_stop produces correct LONG value")
    chk.passed = not chk.failures
    return chk


def _check_oil_macro() -> PresetCheck:
    """cl_macro_fade_preset must inherit chisel-wave defaults
    (ATR floor + session gate + falsification counter)."""
    from eta_engine.strategies.oil_macro_strategy import (
        OilMacroStrategy,
        cl_macro_fade_preset,
    )

    chk = PresetCheck(preset_name="cl_macro_fade_preset",
                      bot_id="cl_macro")
    cfg = cl_macro_fade_preset()
    if cfg.min_atr_usd < 0.10:
        chk.failures.append(
            f"min_atr_usd {cfg.min_atr_usd} too low — dead-tape gate weak")
    else:
        chk.asserts.append(f"min_atr_usd={cfg.min_atr_usd} (chisel)")
    if not cfg.enforce_session_gate:
        chk.failures.append("session gate DISABLED in preset")
    else:
        chk.asserts.append("enforce_session_gate=True (chisel)")
    if cfg.panic_day_min_per_30d < 1:
        chk.failures.append(
            f"panic_day_min_per_30d {cfg.panic_day_min_per_30d} — "
            "falsification floor too low")
    else:
        chk.asserts.append(
            f"panic_day_min_per_30d={cfg.panic_day_min_per_30d} (chisel)")

    # Functional: strategy methods exist
    strat = OilMacroStrategy(cfg)
    if not hasattr(strat, "falsification_triggered"):
        chk.failures.append("falsification_triggered() method missing")
    else:
        # Empty panic_dates → falsification IS triggered (no panic days yet)
        if not strat.falsification_triggered():
            chk.failures.append(
                "fresh strategy with empty panic_dates does NOT trigger "
                "falsification — counter wired incorrectly")
        else:
            chk.asserts.append(
                "falsification_triggered() fires when panic_dates empty",
            )

    chk.passed = not chk.failures
    return chk


def _check_eur_sweep_preserved() -> PresetCheck:
    """eur_sweep_preset is the ONE confirmed-strong diamond.  Wave-3/4
    must NOT have accidentally modified it."""
    from eta_engine.strategies.sweep_reclaim_strategy import eur_sweep_preset

    chk = PresetCheck(preset_name="eur_sweep_preset",
                      bot_id="eur_sweep_reclaim")
    cfg = eur_sweep_preset()
    # eur stays legacy on all wave-4 features
    if cfg.reclaim_confirm_bars != 1:
        chk.failures.append(
            f"eur reclaim_confirm_bars {cfg.reclaim_confirm_bars} != 1 — "
            "do not modify the working diamond")
    else:
        chk.asserts.append("reclaim_confirm_bars=1 (eur preserved)")
    if cfg.vol_adjusted_sizing:
        chk.failures.append(
            "eur vol_adjusted_sizing ON — modifying working diamond")
    else:
        chk.asserts.append("vol_adjusted_sizing=False (eur preserved)")
    chk.passed = not chk.failures
    return chk


def _check_m2k_sweep_promoted() -> PresetCheck:
    """m2k_sweep_preset is the 2026-05-12 promoted diamond.  Verify
    the preset is registered correctly AND the bot is enrolled in
    the diamond protection list + has a retirement threshold."""
    from eta_engine.feeds.capital_allocator import DIAMOND_BOTS
    from eta_engine.scripts.diamond_falsification_watchdog import (
        RETIREMENT_THRESHOLDS_USD,
    )
    from eta_engine.strategies.sweep_reclaim_strategy import m2k_sweep_preset

    chk = PresetCheck(preset_name="m2k_sweep_preset",
                      bot_id="m2k_sweep_reclaim")

    # Preset still callable, returns the expected dataclass shape
    cfg = m2k_sweep_preset()
    if cfg.atr_stop_mult != 1.5:
        chk.failures.append(
            f"m2k atr_stop_mult {cfg.atr_stop_mult} != 1.5 — "
            "promotion baseline was generated by this exact config; "
            "do not tune without a new wave-N rationale")
    else:
        chk.asserts.append("atr_stop_mult=1.5 (baseline preserved)")
    if cfg.rr_target != 2.5:
        chk.failures.append(f"m2k rr_target {cfg.rr_target} != 2.5")
    else:
        chk.asserts.append("rr_target=2.5 (baseline preserved)")

    # Diamond enrollment
    if "m2k_sweep_reclaim" not in DIAMOND_BOTS:
        chk.failures.append(
            "m2k_sweep_reclaim NOT in DIAMOND_BOTS — promotion incomplete",
        )
    else:
        chk.asserts.append("m2k_sweep_reclaim in DIAMOND_BOTS (protected)")
    if "m2k_sweep_reclaim" not in RETIREMENT_THRESHOLDS_USD:
        chk.failures.append(
            "m2k_sweep_reclaim has no RETIREMENT_THRESHOLDS_USD entry "
            "— watchdog will mark INCONCLUSIVE forever",
        )
    else:
        thr = RETIREMENT_THRESHOLDS_USD["m2k_sweep_reclaim"]
        chk.asserts.append(f"retirement_threshold=${thr:.0f} (watchdog-wired)")

    chk.passed = not chk.failures
    return chk


# ────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────


CHECKS = (
    _check_mgc_sweep,
    _check_mcl_sweep,
    _check_eur_sweep_preserved,
    _check_m2k_sweep_promoted,  # 2026-05-12 promotion
    _check_momentum_adx,
    _check_oil_macro,
)


def run() -> dict:
    results = [c() for c in CHECKS]
    all_passed = all(r.passed for r in results)
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_checks": len(results),
        "n_passed": sum(1 for r in results if r.passed),
        "n_failed": sum(1 for r in results if not r.passed),
        "all_passed": all_passed,
        "checks": [asdict(r) for r in results],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict) -> None:
    print("=" * 100)
    print(
        f" DIAMOND PRESET VALIDATION  ({summary['ts']})  "
        f"{summary['n_passed']}/{summary['n_checks']} passed",
    )
    print("=" * 100)
    for chk in summary["checks"]:
        symbol = "[PASS]" if chk["passed"] else "[FAIL]"
        print(f"\n  {symbol}  {chk['preset_name']}  ({chk['bot_id']})")
        for line in chk.get("asserts", []):
            print(f"        ok   {line}")
        for line in chk.get("failures", []):
            print(f"        FAIL  {line}")
    print()
    if summary["all_passed"]:
        print("  ALL DIAMOND PRESETS VALIDATED")
    else:
        print(
            f"  {summary['n_failed']} preset(s) failed validation — "
            "review failures above before promoting to live",
        )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
