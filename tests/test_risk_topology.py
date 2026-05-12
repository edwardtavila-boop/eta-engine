"""Tests for risk_topology — T17 fleet-graph builder."""
from __future__ import annotations

import json
from pathlib import Path


def test_build_topology_returns_empty_when_no_kaizen(tmp_path: Path) -> None:
    """No kaizen_latest.json → empty graph with error field, no exception."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    out = risk_topology.build_topology(kaizen_path=tmp_path / "missing.json")
    assert out["n_nodes"] == 0
    assert out["nodes"] == []
    assert out["links"] == []
    assert out.get("error") == "no_kaizen_latest"


def test_build_topology_from_actions_list(tmp_path: Path) -> None:
    """A kaizen report with `actions` list produces one node per unique bot."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    kaizen = tmp_path / "kaizen_latest.json"
    kaizen.write_text(json.dumps({
        "asof": "2026-05-12T15:00:00Z",
        "actions": [
            {"bot_id": "atr_breakout_mnq", "tier": "ELITE", "score": 1.8,
             "asset_class": "MNQ"},
            {"bot_id": "vp_mnq", "tier": "PRODUCER", "score": 0.9,
             "asset_class": "MNQ"},
            {"bot_id": "btc_mom", "tier": "DECAY", "score": -0.3,
             "asset_class": "BTC"},
        ],
    }), encoding="utf-8")

    out = risk_topology.build_topology(kaizen_path=kaizen)
    assert out["n_nodes"] == 3
    assert out["asof"] == "2026-05-12T15:00:00Z"
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"atr_breakout_mnq", "vp_mnq", "btc_mom"}


def test_correlation_edges_link_same_asset_bots(tmp_path: Path) -> None:
    """Two MNQ bots get a same_asset edge."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    kaizen = tmp_path / "kaizen_latest.json"
    kaizen.write_text(json.dumps({
        "asof": "2026-05-12T15:00:00Z",
        "actions": [
            {"bot_id": "mnq_a", "tier": "ELITE", "asset_class": "MNQ"},
            {"bot_id": "mnq_b", "tier": "PRODUCER", "asset_class": "MNQ"},
            {"bot_id": "btc_a", "tier": "ELITE", "asset_class": "BTC"},
        ],
    }), encoding="utf-8")

    out = risk_topology.build_topology(kaizen_path=kaizen)
    # mnq_a ↔ mnq_b should be linked, mnq_a ↔ btc_a should not (different groups)
    mnq_link = [
        e for e in out["links"]
        if {e["source"], e["target"]} == {"mnq_a", "mnq_b"}
    ]
    assert len(mnq_link) == 1
    assert mnq_link[0]["kind"] == "same_asset"


def test_correlation_edges_link_same_group_bots(tmp_path: Path) -> None:
    """BTC and ETH (same crypto group) get a same_group edge (weight 0.3)."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    kaizen = tmp_path / "kaizen_latest.json"
    kaizen.write_text(json.dumps({
        "asof": "2026-05-12T15:00:00Z",
        "actions": [
            {"bot_id": "btc_a", "tier": "ELITE", "asset_class": "BTC"},
            {"bot_id": "eth_a", "tier": "PRODUCER", "asset_class": "ETH"},
        ],
    }), encoding="utf-8")

    out = risk_topology.build_topology(kaizen_path=kaizen)
    btc_eth = [
        e for e in out["links"]
        if {e["source"], e["target"]} == {"btc_a", "eth_a"}
    ]
    assert len(btc_eth) == 1
    assert btc_eth[0]["kind"] == "same_group"
    assert btc_eth[0]["weight"] == 0.3


def test_node_color_tier_palette(tmp_path: Path) -> None:
    """Each tier maps to a distinct color hex string."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    kaizen = tmp_path / "kaizen_latest.json"
    kaizen.write_text(json.dumps({
        "asof": "2026-05-12T15:00:00Z",
        "actions": [
            {"bot_id": "a", "tier": "ELITE", "asset_class": "MNQ"},
            {"bot_id": "b", "tier": "PRODUCER", "asset_class": "MNQ"},
            {"bot_id": "c", "tier": "DECAY", "asset_class": "MNQ"},
            {"bot_id": "d", "tier": "MARGINAL", "asset_class": "MNQ"},
            {"bot_id": "e", "tier": "INSUFFICIENT", "asset_class": "MNQ"},
        ],
    }), encoding="utf-8")

    out = risk_topology.build_topology(kaizen_path=kaizen)
    colors = {n["id"]: n["color"] for n in out["nodes"]}
    # All distinct
    assert len(set(colors.values())) == 5
    # Each is a valid hex
    for c in colors.values():
        assert c.startswith("#")
        assert len(c) in (4, 7)  # #RGB or #RRGGBB


def test_build_topology_deduplicates_bots(tmp_path: Path) -> None:
    """Same bot appearing in both actions and elite_summary becomes one node."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    kaizen = tmp_path / "kaizen_latest.json"
    kaizen.write_text(json.dumps({
        "asof": "2026-05-12T15:00:00Z",
        "actions": [
            {"bot_id": "atr_breakout_mnq", "tier": "ELITE", "asset_class": "MNQ"},
        ],
        "elite_summary": {
            "top5_elite": [
                {"bot_id": "atr_breakout_mnq", "tier": "ELITE",
                 "asset_class": "MNQ", "score": 1.8},
            ],
        },
    }), encoding="utf-8")

    out = risk_topology.build_topology(kaizen_path=kaizen)
    assert out["n_nodes"] == 1
    assert out["nodes"][0]["id"] == "atr_breakout_mnq"


def test_build_topology_node_link_format_compatibility(tmp_path: Path) -> None:
    """Output shape matches D3 force-directed expectation: nodes + links."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    kaizen = tmp_path / "kaizen_latest.json"
    kaizen.write_text(json.dumps({
        "asof": "2026-05-12T15:00:00Z",
        "actions": [
            {"bot_id": "a", "tier": "ELITE", "asset_class": "MNQ", "score": 1.0},
            {"bot_id": "b", "tier": "PRODUCER", "asset_class": "MNQ", "score": 0.5},
        ],
    }), encoding="utf-8")

    out = risk_topology.build_topology(kaizen_path=kaizen)
    assert "nodes" in out
    assert "links" in out
    for n in out["nodes"]:
        assert {"id", "label", "tier", "color", "size", "asset_class", "score", "r_today"}.issubset(n.keys())
    for link in out["links"]:
        assert {"source", "target", "weight", "kind"}.issubset(link.keys())


def test_corrupt_kaizen_returns_empty_graph(tmp_path: Path) -> None:
    """Garbage kaizen JSON → empty graph, no exception."""
    from eta_engine.brain.jarvis_v3 import risk_topology

    kaizen = tmp_path / "kaizen_latest.json"
    kaizen.write_text("not json at all {{{{", encoding="utf-8")
    out = risk_topology.build_topology(kaizen_path=kaizen)
    assert out["n_nodes"] == 0
