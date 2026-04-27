"""Daily sweep runner for the 4-layer APEX funnel.

Usage
-----
    python scripts/funnel_sweep_daily.py \\
        --state docs/funnel/funnel_state.json \\
        --out-dir docs/funnel

On every run:
  1. Loads or bootstraps a ``funnel_state.json`` with per-layer equities +
     peak-equity high water marks.
  2. Reads a ``funnel_input.json`` (optional) to pull today's realized PnL,
     vol regime, and vol z-score per layer. If missing, runs in dry-mode
     with zeros (safe: no sweeps).
  3. Calls ``FunnelWaterfall.plan`` for the sweep + directive plan.
  4. Writes three artifacts:
       - ``funnel_plan_<ts>.json``       -- machine-readable plan
       - ``funnel_digest_<ts>.md``       -- markdown digest for Discord
       - ``funnel_state.json``           -- updated state
  5. Does NOT execute transfers. That's the funnel.orchestrator's job --
     this script only produces the daily plan + digest.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.funnel.waterfall import (  # noqa: E402
    DEFAULT_TIERS,
    FunnelSnapshot,
    FunnelWaterfall,
    LayerId,
    LayerSnapshot,
    VolRegime,
    format_digest,
)


def _bootstrap_state() -> dict[str, Any]:
    """Sensible defaults for a fresh funnel."""
    return {
        LayerId.LAYER_1_MNQ.value: {
            "current_equity": 50_000.0,
            "peak_equity": 50_000.0,
        },
        LayerId.LAYER_2_BTC.value: {
            "current_equity": 2_000.0,
            "peak_equity": 2_000.0,
        },
        LayerId.LAYER_3_PERPS.value: {
            "current_equity": 0.0,
            "peak_equity": 0.0,
        },
        LayerId.LAYER_4_STAKING.value: {
            "current_equity": 0.0,
            "peak_equity": 0.0,
        },
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _bootstrap_state()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _bootstrap_state()


def _load_input(path: Path | None) -> dict[str, Any]:
    """Today's realized pnl + vol context per layer."""
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _build_snapshot(
    state: dict[str, Any],
    inp: dict[str, Any],
    ts_utc: str,
) -> FunnelSnapshot:
    layers: dict[LayerId, LayerSnapshot] = {}
    for lid in LayerId:
        s = state.get(lid.value, {"current_equity": 0.0, "peak_equity": 0.0})
        i = inp.get(lid.value, {})
        vol_regime_raw = i.get("vol_regime", VolRegime.NORMAL.value)
        try:
            vol_regime = VolRegime(vol_regime_raw)
        except ValueError:
            vol_regime = VolRegime.NORMAL
        layers[lid] = LayerSnapshot(
            layer=lid,
            current_equity=float(s.get("current_equity", 0.0)),
            peak_equity=float(s.get("peak_equity", 0.0)),
            realized_pnl_since_last_sweep=float(i.get("pnl", 0.0)),
            vol_regime=vol_regime,
            vol_z=float(i.get("vol_z", 0.0)),
        )
    return FunnelSnapshot(layers=layers, ts_utc=ts_utc)


def _apply_plan_to_state(
    state: dict[str, Any],
    snapshot: FunnelSnapshot,
    plan_dict: dict[str, Any],
) -> dict[str, Any]:
    """Advance per-layer equities post-sweep + update peak equities."""
    new_state = {
        lid.value: {
            "current_equity": layer.current_equity,
            "peak_equity": max(layer.peak_equity, layer.current_equity),
        }
        for lid, layer in snapshot.layers.items()
    }
    for sweep in plan_dict["sweeps"]:
        src = sweep["src"]
        dst = sweep["dst"]
        amt = float(sweep["amount_usd"])
        new_state[src]["current_equity"] -= amt
        new_state[dst]["current_equity"] += amt
        new_state[dst]["peak_equity"] = max(
            new_state[dst]["peak_equity"],
            new_state[dst]["current_equity"],
        )
    return new_state


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run one tick of the APEX 4-layer profit waterfall.",
    )
    default_state = ROOT / "docs" / "funnel" / "funnel_state.json"
    p.add_argument("--state", type=str, default=str(default_state))
    p.add_argument("--input", type=str, default="")
    p.add_argument(
        "--out-dir",
        type=str,
        default=str(ROOT / "docs" / "funnel"),
    )
    p.add_argument(
        "--global-kill-pct",
        type=float,
        default=0.08,
        help="Total funnel DD threshold that HALTs every layer.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    state_path = Path(args.state)
    input_path = Path(args.input) if args.input else None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    state = _load_state(state_path)
    inp = _load_input(input_path)
    ts_utc = datetime.now(UTC).isoformat()
    snapshot = _build_snapshot(state, inp, ts_utc)

    waterfall = FunnelWaterfall(
        tiers=DEFAULT_TIERS,
        global_kill_pct=args.global_kill_pct,
    )
    plan = waterfall.plan(snapshot)
    plan_dict = plan.as_dict()

    ts_label = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    plan_path = out_dir / f"funnel_plan_{ts_label}.json"
    plan_path.write_text(
        json.dumps(plan_dict, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "funnel_plan_latest.json").write_text(
        json.dumps(plan_dict, indent=2) + "\n",
        encoding="utf-8",
    )

    digest = format_digest(snapshot, plan)
    (out_dir / f"funnel_digest_{ts_label}.md").write_text(digest + "\n", encoding="utf-8")
    (out_dir / "funnel_digest_latest.md").write_text(digest + "\n", encoding="utf-8")

    new_state = _apply_plan_to_state(state, snapshot, plan_dict)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(new_state, indent=2) + "\n",
        encoding="utf-8",
    )

    print("APEX funnel sweep complete")
    print(f"  total_equity:   ${snapshot.total_equity:,.2f}")
    print(f"  funnel_dd_pct:  {snapshot.global_drawdown_pct:.2%}")
    print(f"  sweeps:         {len(plan.sweeps)}")
    print(f"  directives:     {len(plan.directives)}")
    print(f"  global_kill:    {plan.global_kill}")
    print(f"  plan:           {plan_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
