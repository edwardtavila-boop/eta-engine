# Nasdaq Prop Sleeve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared MNQ/NQ prop-sleeve exposure guard so NQ acts as a conviction amplifier instead of accidentally stacking the same Nasdaq risk as MNQ.

**Architecture:** Extend the existing `CrossBotPositionTracker` with a Nasdaq equivalent-contract cap: `MNQ = 1 MNQ-equivalent`, `NQ = 10 MNQ-equivalent`. The supervisor calls this cap after the existing same-root fleet cap and before broker submission, while keeping per-order and broker/live-money gates unchanged.

**Tech Stack:** Python, pytest, existing ETA supervisor runtime, existing `eta_engine.safety.cross_bot_position_tracker`.

---

### Task 1: Nasdaq Equivalent Exposure Policy

**Files:**
- Modify: `safety/cross_bot_position_tracker.py`
- Modify: `tests/test_cross_bot_position_cap.py`

- [x] **Step 1: Write failing policy tests**

Added tests proving `1 NQ` consumes the same sleeve as `10 MNQ`, same-direction MNQ exposure is blocked, and opposite-side MNQ exposure reduction is allowed.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/test_cross_bot_position_cap.py::test_nasdaq_sleeve_blocks_mnq_when_nq_already_open tests/test_cross_bot_position_cap.py::test_nasdaq_sleeve_allows_reducing_opposite_exposure tests/test_cross_bot_position_cap.py::test_supervisor_blocks_nasdaq_sleeve_breach -q`

Expected and observed initial failure: import error for missing `PropSleeveCapExceeded`.

- [x] **Step 3: Implement minimal policy**

Added Nasdaq sleeve constants, `PropSleeveCapExceeded`, `resolve_prop_sleeve_cap()`, and `CrossBotPositionTracker.assert_prop_sleeve_cap()`.

- [x] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_cross_bot_position_cap.py tests/test_jarvis_strategy_supervisor.py::test_stale_force_flatten_blocks_immediate_reentry -q`

Observed: `32 passed, 1 warning`.

### Task 2: Supervisor Pre-Trade Gate

**Files:**
- Modify: `scripts/jarvis_strategy_supervisor.py`
- Modify: `tests/test_cross_bot_position_cap.py`

- [x] **Step 1: Write failing supervisor gate test**

Added a test proving `_maybe_enter()` blocks before `submit_entry()` when an existing NQ position plus a proposed MNQ entry would exceed the Nasdaq sleeve cap.

- [x] **Step 2: Verify RED**

Run: `python -m pytest tests/test_cross_bot_position_cap.py::test_supervisor_blocks_nasdaq_sleeve_breach -q`

Expected and observed initial failure: test reached `submit_entry()`.

- [x] **Step 3: Implement supervisor call**

Added `assert_prop_sleeve_cap()` immediately after the same-root fleet cap, with a heartbeat-visible `prop_sleeve_cap:NASDAQ...` rejection reason and a `prop_sleeve_cap_blocked` risk event.

- [x] **Step 4: Verify focused runtime tests**

Run: `python -m pytest tests/test_cross_bot_position_cap.py tests/test_jarvis_strategy_supervisor.py::test_stale_force_flatten_blocks_immediate_reentry -q`

Observed: `32 passed, 1 warning`.

### Task 3: Verification, Commit, Deploy

**Files:**
- Modify: root gitlink after child push only.

- [x] **Step 1: Static and focused verification**

Run: `python -m ruff check safety/cross_bot_position_tracker.py safety/__init__.py scripts/jarvis_strategy_supervisor.py tests/test_cross_bot_position_cap.py`

Observed: `All checks passed!`

- [ ] **Step 2: Child commit and push**

Run: `git add safety/cross_bot_position_tracker.py safety/__init__.py scripts/jarvis_strategy_supervisor.py tests/test_cross_bot_position_cap.py docs/superpowers/plans/2026-05-08-nasdaq-prop-sleeve.md`

Run: `git commit -m "feat: gate nasdaq prop sleeve exposure"`

Run: `git push origin codex/paper-live-runtime-hardening`

- [ ] **Step 3: Root gitlink bump and push**

Run from `C:\EvolutionaryTradingAlgo`: `git add eta_engine && git commit -m "chore: bump eta_engine nasdaq prop sleeve gate" && git push origin main`

- [ ] **Step 4: VPS deploy and live probe**

Deploy the pushed engine branch to the VPS using `deploy/scripts/sync_dashboard_api_live.ps1`, restart `ETA-Jarvis-Strategy-Supervisor`, and confirm `/api/bot-fleet` still reports fresh bots, connected gateway, router OK, and stale count `0`.
