"""
JARVIS v3 // risk_topology (T17)

Builds a graph representation of the fleet's risk surface for the
Claw3D visualizer (or any topology renderer). Each bot is a node sized
by notional, colored by R-today; edges between bots represent
correlation in their entries/exits.

Pure read module — no writes, no live broker calls. Reads from:

  * kaizen_latest.json     — for per-bot tier, score, R
  * dashboard_events.jsonl — for fleet snapshots if available
  * Optional correlation matrix from a sidecar (if a later track ever
    ships per-bot correlation; for now we synthesize from asset_class)

Public interface:
  * ``build_topology()`` — returns a ``TopologyGraph`` dict (node-link
    format compatible with D3 force-directed layouts).
  * ``EXPECTED_HOOKS`` — wiring-audit declaration.

The MCP tool ``jarvis_topology`` (added in jarvis_mcp_server.py)
exposes this through the standard envelope.

The OUTPUT is JSON, designed to be either:
  (a) Polled by Claw3D every 30s via a GET /v1/jarvis-topology route
      (a follow-up wires this), OR
  (b) Pushed via webhook from a scheduled task — the operator picks
      polling vs pushing based on latency tolerance.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.risk_topology")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
DEFAULT_KAIZEN_LATEST = _STATE_ROOT / "kaizen_latest.json"

EXPECTED_HOOKS = ("build_topology",)

# Color palette for node states. Hex strings keep this portable to any
# renderer (D3, Three.js, Cytoscape). Operator can override per skill.
_TIER_COLOR = {
    "ELITE": "#22c55e",  # green
    "PRODUCER": "#06b6d4",  # cyan
    "MARGINAL": "#eab308",  # yellow
    "DECAY": "#f97316",  # orange
    "INSUFFICIENT": "#94a3b8",  # slate
}


def _read_kaizen_latest(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_KAIZEN_LATEST
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("risk_topology kaizen read failed: %s", exc)
        return {}


def _node_size(record: dict[str, Any]) -> float:
    """Normalize node radius from notional or score.

    Falls back to a default size if neither field is present so the
    graph still renders during the initial-data window.
    """
    notional = record.get("notional", 0.0)
    if not notional:
        # Synthesize from score (Sharpe-ish) so the graph isn't flat
        score = record.get("score", 0.0)
        try:
            return max(8.0, 8.0 + abs(float(score)) * 5.0)
        except (TypeError, ValueError):
            return 12.0
    try:
        return max(8.0, min(48.0, 8.0 + float(notional) / 5000.0))
    except (TypeError, ValueError):
        return 12.0


def _node_color(record: dict[str, Any]) -> str:
    tier = str(record.get("tier", "INSUFFICIENT")).upper()
    return _TIER_COLOR.get(tier, _TIER_COLOR["INSUFFICIENT"])


def _correlation_edges(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize correlation edges from asset_class shared between bots.

    For v1 we use a simple rule: bots in the same asset_class get a
    weight-0.5 edge; bots in correlated asset_classes (BTC↔ETH,
    MNQ↔ES, MGC↔SI) get weight-0.3 edges. A future track can replace
    this with a real per-bot correlation matrix sourced from trade
    closes.
    """
    correlated_groups = {
        "crypto": {"BTC", "ETH", "SOL", "LTC", "ADA"},
        "equity_index": {"MNQ", "MES", "NQ", "ES"},
        "metals": {"MGC", "GC", "SI", "MSI"},
        "energy": {"CL", "MCL", "NG"},
        "fx": {"6E", "6B", "6J", "EUR", "GBP", "JPY"},
    }

    def _group_of(asset: str) -> str | None:
        for g, syms in correlated_groups.items():
            if asset.upper() in syms:
                return g
        return None

    edges: list[dict[str, Any]] = []
    n = len(nodes)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = nodes[i], nodes[j]
            ga = _group_of(str(a.get("asset_class", "")))
            gb = _group_of(str(b.get("asset_class", "")))
            same_asset = str(a.get("asset_class", "")).upper() == str(b.get("asset_class", "")).upper() and a.get(
                "asset_class"
            )
            if same_asset:
                edges.append(
                    {
                        "source": a["id"],
                        "target": b["id"],
                        "weight": 0.5,
                        "kind": "same_asset",
                    }
                )
            elif ga and gb and ga == gb:
                edges.append(
                    {
                        "source": a["id"],
                        "target": b["id"],
                        "weight": 0.3,
                        "kind": "same_group",
                    }
                )
    return edges


def build_topology(kaizen_path: Path | None = None) -> dict[str, Any]:
    """Build the topology graph from the latest kaizen report.

    Returns a node-link dict suitable for D3 force-directed layouts:

        {
            "asof": ISO timestamp,
            "n_nodes": int,
            "n_edges": int,
            "nodes": [
                {"id": str, "label": str, "tier": str, "color": str,
                 "size": float, "asset_class": str, "score": float,
                 "r_today": float},
                ...
            ],
            "links": [
                {"source": id, "target": id, "weight": float, "kind": str},
                ...
            ],
        }

    NEVER raises. Returns ``{"n_nodes": 0, "nodes": [], "links": []}``
    when the kaizen report is missing or unparseable.
    """
    rep = _read_kaizen_latest(kaizen_path)
    if not rep:
        return {
            "asof": None,
            "n_nodes": 0,
            "n_edges": 0,
            "nodes": [],
            "links": [],
            "error": "no_kaizen_latest",
        }

    # Different kaizen report shapes — actions list OR per_bot dict.
    bot_records: list[dict[str, Any]] = []
    actions = rep.get("actions") or []
    if isinstance(actions, list):
        bot_records.extend(r for r in actions if isinstance(r, dict))

    # Also pull from elite_summary if available
    summary = rep.get("elite_summary") or {}
    for key in ("top5_elite", "top5_dark"):
        for r in summary.get(key) or []:
            if isinstance(r, dict):
                bot_records.append(r)

    # De-dupe by bot_id
    seen_ids: set[str] = set()
    nodes: list[dict[str, Any]] = []
    for rec in bot_records:
        bot_id = str(rec.get("bot_id", "")).strip()
        if not bot_id or bot_id in seen_ids:
            continue
        seen_ids.add(bot_id)
        nodes.append(
            {
                "id": bot_id,
                "label": bot_id,
                "tier": str(rec.get("tier", "INSUFFICIENT")).upper(),
                "color": _node_color(rec),
                "size": _node_size(rec),
                "asset_class": rec.get("asset_class") or rec.get("asset") or "",
                "score": float(rec.get("score", 0.0)) if _is_number(rec.get("score")) else 0.0,
                "r_today": float(rec.get("r_today", 0.0)) if _is_number(rec.get("r_today")) else 0.0,
            }
        )

    edges = _correlation_edges(nodes)

    return {
        "asof": rep.get("asof"),
        "n_nodes": len(nodes),
        "n_edges": len(edges),
        "nodes": nodes,
        "links": edges,
    }


def _is_number(v: Any) -> bool:  # noqa: ANN401
    if isinstance(v, bool):
        return False  # don't treat bool as number for this purpose
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        with contextlib.suppress(ValueError):
            float(v)
            return True
    return False
