# CLAUDE.md — Apex Predator onboarding

Reference for Claude Code sessions. Read this first; ROADMAP.md is the long-form blueprint.

## What this is
4-bot profit-recycling trading organism: MNQ/NQ micro futures + crypto seed grid + ETH/SOL/XRP perps + staking funnel. Runs gated by "The Firm" — a 6-agent board (Quant, Red Team, Risk, Macro, Micro, PM) that votes on every phase transition. Phase 0 scaffold; bots are wired but boot **paused**.

## Master Command Center (MCC)
**`scripts/jarvis_dashboard.py`** is the canonical operator console — the *Master Command Center*. It is the single source of truth for live JARVIS state (drift, breaker, deadman, daemons, promotion queue, calibration, journals, alerts) and the primary tool for commanding the framework. It is a stdlib HTTP server (no FastAPI), installable as a PWA on phone/desktop home screens, and exposed remotely via Cloudflare Tunnel + Cloudflare Access. systemd unit: `deploy/systemd/jarvis-command-center.service`. Default bind: `127.0.0.1:8765`. Run locally:

```bash
PYTHONPATH=/tmp/_pkg_root:$PWD .venv/bin/python -m apex_predator.scripts.jarvis_dashboard
# then open http://127.0.0.1:8765
```

Routes: `/` (HTML shell), `/api/state`, `/api/state/stream` (SSE; sub-second pushes), `/healthz`, `/manifest.webmanifest`, `/sw.js`, `/icon.svg`, `/api/push/vapid-public-key`. Action POSTs (audit-logged, operator from `Cf-Access-Jwt-Assertion`): `/api/cmd/kill-switch-trip`, `/api/cmd/kill-switch-reset`, `/api/cmd/pause-bot`, `/api/cmd/unpause-bot` (requires `confirm: I_UNDERSTAND_LIVE_RISK` per the never-auto-unpause rule), `/api/cmd/ack-alert`, `/api/push/subscribe`. Audit log lands at `~/.local/state/apex_predator/mcc_audit.jsonl`; intent records at `~/.local/state/apex_predator/mcc_*` for the supervisor to read. Voice control runs in the browser via Web Speech API. Any new operator-facing surface area should be wired through the MCC — do not fork another dashboard.

## Repo layout (important)
The package directory IS the repo root. `from apex_predator.foo import bar` resolves to `./foo/bar.py`. To make imports work locally, symlink the repo into a parent dir named `apex_predator`:

```bash
mkdir -p /tmp/_pkg_root
ln -sf "$PWD" /tmp/_pkg_root/apex_predator
export PYTHONPATH=/tmp/_pkg_root:$PWD
```

`pyproject.toml` declares `name = "apex-predator"` with hatchling but the layout doesn't match a standard `pip install -e .` — `ci.yml` uses the symlink trick above; `test.yml` assumes the standard layout and currently doesn't match reality. Treat ci.yml as the source of truth for what actually runs.

## Local dev quickstart
```bash
python3.12 -m venv .venv
.venv/bin/pip install ruff pytest pytest-asyncio pytest-cov mypy \
  pydantic numpy pandas scipy aiohttp pyyaml
# (heavy deps — torch, ccxt, web3, arcticdb — only needed for runtime, not most tests)

# Lint (ci.yml scope — production code only):
.venv/bin/ruff check strategies/ scripts/_bump_roadmap_v0_1_4*.py \
  scripts/_pre_commit_check.py scripts/_new_roadmap_bump.py

# Tests (with symlink + PYTHONPATH set as above):
.venv/bin/python -m pytest tests/ -q -m "not slow"
```

Current baseline: ~3158 tests collected, ~10 collection failures (WIP modules referenced but not yet implemented: `core.basis_stress_breaker`, `core.live_shadow`, `core.kill_switch_latch`, `features.crowd_pain_index`, `scripts.sample_size_calc`, `bots.btc_hybrid.profile`, `obs.probes`), ~32 runtime failures on partial WIP. Don't be alarmed by these — they're expected during current phase work.

## Make targets
`make lint` / `make test` / `make verify` / `make backtest-demo` / `make firm-gate SPEC=...` / `make preflight` / `make all`. The Makefile assumes a nested `apex_predator/` package dir; if a target errors with "No such file or directory" on `apex_predator`, fall back to the symlink-based commands above.

## Live-mode safety rules
- Bots boot **paused**. `--unpause` is required to trade. Never auto-unpause.
- Tradovate is **DORMANT** (funding-blocked 2026-04-24). Active futures brokers: IBKR (primary), Tastytrade (fallback). Flip lives in `venues/router.py:DORMANT_BROKERS`.
- `make preflight` is mandatory before live mode. Will fail if `.env` is incomplete.
- Firm-gate workflow blocks merges to paper/live branches on KILL/NO_GO verdicts (`.github/workflows/firm-gate.yml`).

## Branch & PR flow
- All work in this session lands on `claude/push-main-changes-5F8iM` (per harness instructions).
- PRs open as **draft**, marked ready-for-review when complete, **auto-merge enabled** so they land on `main` once required checks pass.
- Never push directly to `main`. Never `--no-verify` or skip Firm gate.

## Where things live
- `bots/` — the four bot families (mnq, btc_hybrid, eth/sol/xrp perps, etc.)
- `brain/` — regime classifier, RL agents, Firm bridge, multi-agent supervisor
- `core/` — risk engine, kill switch, broker equity adapter, principles checklist, parameter sweep
- `strategies/` — primary lint-target dir; entry/exit logic
- `obs/` — observability probes, SLA event registry, drift detectors
- `venues/` — broker adapters (Tradovate dormant, IBKR/Tasty active, Bybit/OKX for crypto)
- `deploy/` — fleet task scripts, dashboard, supervisor wiring
- `docs/` — Firm specs, decisions, broker connection snapshots, roadmap dashboard
- `tests/` — flat layout, ~3100 tests; mirror module path in filename
- `ROADMAP.md` (17KB) — phase plan; `roadmap_state.json` (332KB) — live progress tree, structured but undocumented

## Conventions
- Commit format: `vX.Y.Z: short summary` for milestone bumps; `feat(scope):` / `fix(scope):` / `chore(scope):` otherwise. Match the existing log style.
- Every milestone bump touches `roadmap_state.json` via `scripts/_new_roadmap_bump.py` (dedup-safe append).
- Tests use the `slow` marker for cross-regime sims and multi-bar walk-forwards; deselect with `-m "not slow"` for fast iteration.
- Pre-commit hooks: ruff format/lint, mypy strict on `core/` only, AST/YAML/JSON validation, 500KB file-size cap.

## Quick "what state am I in?" checklist
```bash
git status && git log --oneline -5         # working tree + recent direction
git rev-list --left-right --count origin/main...HEAD   # ahead/behind main
ls docs/_backups/ 2>/dev/null | tail -3    # last roadmap state snapshots
```
