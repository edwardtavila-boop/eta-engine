---
name: jarvis-bus
version: 1.0.0
description: Inter-agent coordination — multiple Claude Code sessions / specialized agents register, claim locks, and coordinate to prevent conflicting destructive actions.
tags: [trading, coordination, multi-agent, locks, bus]
trigger_phrases:
  - "who else is online"
  - "list agents"
  - "claim this bot"
  - "release lock on"
  - "coordinate with"
---

# jarvis-bus (T14)

Inter-agent coordination so multiple Claude Code sessions can divide
the work without stepping on each other.

The typical pattern:

* Operator runs 3 Claude Code sessions in parallel:
  * `research-bot`: explores new strategies, reads docs, drafts code
  * `monitor-bot`: watches live consults, alerts on anomalies
  * `execution-bot`: invokes destructive actions (retire, kill) on operator approval
* Each session registers with `jarvis_register_agent(agent_id="<your-id>",
  role="research"|"monitor"|"execution")` at the start.
* Before invoking any destructive tool (retire, kill, deep size-trim),
  the session calls `jarvis_acquire_lock` on the affected resource.
* If `LOCKED_BY_OTHER` returns, the session waits or defers — the other
  agent is mid-action on this resource.
* On completion (success or failure), session calls
  `jarvis_release_lock` to free the resource.

## Resource naming convention

Use stable, hierarchical strings as resource IDs:

| Pattern | Use case |
|---|---|
| `bot:<bot_id>` | Per-bot operation (retire, set_size_modifier, etc.) |
| `asset:<asset_class>` | Per-asset overlay (pin_school_weight on asset) |
| `fleet:kill` | Kill switch — only one agent can trip at a time |
| `fleet:deploy` | Bulk deploy operation |
| `kaizen:run` | A kaizen pass — only one at a time |
| `topology:rebuild` | Topology graph rebuild |

## Standard workflow

```
1. Agent startup:
   jarvis_register_agent(agent_id="research-claude-session-1",
                          role="research",
                          version="1.0.0")

2. Periodic heartbeat (every ~5 min during long-running sessions):
   [implicit via any tool call OR explicit jarvis_register_agent re-call]

3. Before destructive action:
   result = jarvis_acquire_lock(agent_id="research-claude-session-1",
                                  resource="bot:vp_mnq",
                                  purpose="investigate anomaly",
                                  ttl_seconds=600)
   if result.status == "LOCKED_BY_OTHER":
       # Another agent has this; wait or defer
       inform_operator(f"vp_mnq is locked by {result.owner_agent_id} until {result.expires_at}")
       return
   if result.status == "ACQUIRED" or "REACQUIRED":
       proceed_with_destructive_action()

4. After action completes (success or failure):
   jarvis_release_lock(agent_id="research-claude-session-1",
                       resource="bot:vp_mnq")

5. Agent shutdown:
   jarvis_deregister_agent(agent_id="research-claude-session-1")
```

## Conflict resolution

Locks are NOT mandatory — destructive tools don't check locks before
firing (yet). The bus is a coordination DISCIPLINE, not enforcement.
Agents that follow the protocol benefit; agents that ignore it can
still fire actions and the operator sees both in the audit log.

A future track (T14.v2) can add enforcement: destructive MCP tools
check for an unowned lock or a lock owned by the calling agent_id
before proceeding. For v1, soft coordination is enough.

## Cost

Bus operations are pure read/write to a JSON file on the VPS — zero
LLM cost. An agent that registers + heartbeats every 5 min adds maybe
~100 KB/day of audit log entries. Negligible.

## When to use

* **Two or more Claude Code sessions active at once** on the same fleet.
* **Overnight runs** where an autonomous agent might race with a future
  operator session.
* **Operator + research session** doing parallel work (e.g. operator
  is reviewing fleet, research session is backtesting; they shouldn't
  both retire the same bot).

## When NOT to use

* **Single-session operation** — the bus adds latency for no benefit.
  Don't register the only agent.
* **Cross-machine coordination** — the bus is single-VPS. Not for
  multi-VPS topologies.

## Operator-facing summary

```
═══════════ JARVIS INTER-AGENT BUS ═══════════

Online agents:
  • {agent_id}  role={role}  last_seen={time-ago}
  • {agent_id}  role={role}  last_seen={time-ago}

Active locks:
  • {resource}  → {owner_agent_id}  expires_in={time}  purpose="{purpose}"
  • {resource}  → {owner_agent_id}  expires_in={time}  purpose="{purpose}"

(For lockless operations: no agent has acquired this resource;
any agent may proceed.)
═══════════════════════════════════════════════
```

Operator can invoke this view anytime with "who's online" or
"list agents".

## Memory note

The bus does NOT save agent activity to long-term memory. It's
operational state — relevant for the current session, not the
operator's durable knowledge.

## Edge cases

* **Agent crashes mid-action**: lock auto-expires on TTL (default
  10 min). Other agents can then proceed. Acceptable for v1; a future
  enhancement can add operator-triggered force-release.
* **Two agents call register with the same agent_id**: second
  registration silently overwrites the first. Operator should pick
  unique agent_ids per session (timestamp + role works well).
* **Clock skew** (multi-VPS, not applicable today): all timestamps are
  UTC and on a single VPS, so skew is zero.
