---
name: jarvis-topology
version: 1.0.0
description: Live risk topology — force-directed graph of the fleet (bots as nodes sized by notional / colored by tier, edges by correlation). Pushes to Claw3D.
tags: [trading, visualization, claw3d, topology, risk]
trigger_phrases:
  - "show me the topology"
  - "fleet graph"
  - "topology view"
  - "risk map"
  - "what does the fleet look like"
---

# jarvis-topology (T17)

Real-time topology view of the fleet. Bots are nodes sized by notional
and colored by tier (ELITE green → DECAY orange). Edges represent
correlation between bots (asset-class for v1; per-bot returns
correlation in a future track).

Two modes:

## Mode A: snapshot (operator query)

Operator: "show me the topology" → activate this skill → call
`jarvis_topology` → render the JSON in a chat-friendly format
(text-only since Hermes can't draw graphs inline).

Output:

```
═══════════ JARVIS FLEET TOPOLOGY · {asof} ═══════════

{N} bots active · {edge_count} correlation edges

Top 5 by tier:
  ELITE:    {bot, bot, ...}
  PRODUCER: {bot, bot, ...}
  DECAY:    {bot, bot, ...}

Largest clusters (by edge count):
  • {asset_class} group: {bot_a, bot_b, bot_c}  ({n} edges)
  • ...

For the visual graph, open Claw3D — the topology updates every 30s
once Mode B is enabled.
═══════════════════════════════════════════
```

## Mode B: live push to Claw3D (scheduled task)

Operator enables Claw3D rendering via:

```yaml
# in ~/.hermes/config.yaml scheduled_tasks
- name: topology_push
  cron: "*/30 * * * * *"   # every 30 seconds (Hermes cron supports 6-field)
  delivery: webhook
  delivery_extra:
    url: "http://127.0.0.1:8765/claw3d/topology"
    method: POST
  prompt: |
    Call jarvis_topology with no args. Return the response data as a
    JSON string. The webhook delivery will POST it to Claw3D's render
    endpoint. Do not narrate, just return the JSON.
```

Claw3D then renders a force-directed layout with:
* Node radius ∝ `size` field (8-48px)
* Node fill = `color` field (hex)
* Edge thickness ∝ `weight` field (0.3 = thin, 0.5 = medium)
* Click node → expand bot details (operator UX in Claw3D)

## Edge semantics

* `kind=same_asset` (weight 0.5): two bots trade the same instrument.
  Correlation in entries/exits is near-perfect when both fire.
* `kind=same_group` (weight 0.3): two bots in correlated assets
  (BTC↔ETH, MNQ↔ES, MGC↔SI, CL↔NG, FX pairs). Looser correlation
  but worth watching for cluster risk.

A future track can replace the synthetic edges with per-bot return
correlation computed from actual trade closes (T17.v2).

## Operator playbook

| Want to ... | Say |
|---|---|
| Quick text snapshot | "topology" |
| See cluster risk | "what does the fleet look like — any concentration?" |
| Enable live Claw3D push | wire the scheduled_task above + restart gateway |
| Disable live push | `schtasks /Change /TN ETA-Hermes-Agent /DISABLE` then re-enable after editing config to remove topology_push |

## What this surfaces

* **Concentration risk**: if 8 of 10 ELITE bots are in the equity_index
  group, that's a single regime risk — the graph makes it obvious.
* **Orphan strategies**: a bot with zero edges (no correlated peers)
  is doing something unique. That's interesting — either a real
  alpha or an anomaly worth investigating.
* **Tier transitions**: bots drifting from ELITE green → DECAY orange
  visually pop. Operator can catch a fading strategy faster than by
  reading kaizen reports.

## Cost

`jarvis_topology` is a pure read (no LLM, no broker). Operator queries
cost one chat completion (~$0.05). Live push every 30s would cost
~$140/day if naively wired — DON'T enable Mode B with chat-LLM in the
loop. Instead the scheduled task uses `deliver_only: true` to bypass
the LLM entirely, making cost zero.

## Edge cases

* **Empty fleet** (no kaizen report yet): graph renders as an empty
  canvas with "no fleet data" footer. Auto-recovers once a kaizen pass
  runs.
* **>50 bots**: Claw3D's default force-directed layout struggles past
  ~50 nodes. The skill can optionally pass `tier_filter=["ELITE","PRODUCER"]`
  to focus the view (future enhancement).
