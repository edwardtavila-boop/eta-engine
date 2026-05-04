# Force Multiplier — Multi-Model Orchestration

**Wave-19 (2026-05-04)**. Routes every LLM task to the provider best suited for that work, using the operator's existing subscriptions instead of pay-per-token API keys for the premium tiers.

## The three roles

| Provider | Role | Auth | Cost model |
|---|---|---|---|
| **DeepSeek V4** | **Worker Bee** — bulk generation, boilerplate, large codebase mapping | `DEEPSEEK_API_KEY` (cheap API) | ~$0.14/1M input tokens |
| **Claude** | **Lead Architect** — planning, architecture, code review, red-team | `claude login` (Pro/Max subscription) | included in monthly plan |
| **Codex** | **Systems Expert** — debugging, test execution, security audits, computer-use | `codex login` (ChatGPT Plus/Pro subscription) | included in monthly plan |

If a CLI provider is unavailable (not authenticated, quota exhausted, timeout), the task transparently falls back to **DeepSeek API** so work never blocks.

## Setup

### 1. DeepSeek (already configured)

`eta_engine/.env`:

```
DEEPSEEK_API_KEY=sk-...
```

The loader (`eta_engine/brain/llm_provider.py::_ensure_dotenv`) reads both the workspace root `.env` and `eta_engine/.env`, with the submodule-local file winning for `ETA_*` keys.

### 2. Claude CLI (subscription)

Install once:

```bash
npm install -g @anthropic-ai/claude-code
```

Authenticate. The `claude` CLI has TWO auth states:

- `claude login` — interactive chat session (used when you type `claude` and chat)
- `claude setup-token` — long-lived token for `claude -p` non-interactive calls

The Force Multiplier integration uses `claude -p`, so you need `setup-token`:

```bash
claude setup-token   # opens browser, paste back token
```

Verify:

```bash
claude --version              # should print 2.x
claude auth status            # should show "loggedIn": true
claude -p "say hi" --print    # should produce a response, not 401
```

If `auth status` says logged in but `-p` returns 401, the issue is specifically the
non-interactive token — `setup-token` will fix it without touching the chat session auth.

### 3. Codex CLI (subscription)

Install once:

```bash
npm install -g @openai/codex
```

Authenticate (browser-based OAuth, uses your ChatGPT Plus/Pro plan):

```bash
codex login
```

Verify:

```bash
codex --version
codex exec "say hi" --full-auto --skip-git-repo-check
```

### 4. Health probe

After setup, run from the workspace root:

```bash
python -m eta_engine.scripts.force_multiplier_health           # config-only check
python -m eta_engine.scripts.force_multiplier_health --live    # also makes live calls
```

The `--live` flag costs ~$0.000010 on DeepSeek and burns a few seconds of subscription quota on Claude/Codex. Skip it if you're conserving monthly limits.

## Usage

### Single-shot routing — `route_and_execute`

Routes one task to the right provider based on its `TaskCategory`:

```python
from eta_engine.brain.multi_model import route_and_execute
from eta_engine.brain.model_policy import TaskCategory

resp = route_and_execute(
    category=TaskCategory.ARCHITECTURE_DECISION,  # → CLAUDE
    system_prompt="You are an architect.",
    user_message="Should we use Redis or PostgreSQL for the rate-limit cache?",
    max_tokens=400,
)

print(resp.provider.value)   # "claude" (or "deepseek" if claude unavailable)
print(resp.fallback_used)    # False if claude succeeded, True if it fell back
print(resp.text)
```

### Per-call provider override

Skip the routing policy and force a specific provider:

```python
from eta_engine.brain.model_policy import ForceProvider

resp = route_and_execute(
    category=TaskCategory.STRATEGY_EDIT,
    user_message="Refactor confluence_v3.py to use the new venue base class.",
    force_provider=ForceProvider.CLAUDE,   # override → use Claude even though policy says DeepSeek
)
```

### The chain — `force_multiplier_chain`

The canonical 3-stage pipeline (plan → implement → verify):

```python
from eta_engine.brain.multi_model import force_multiplier_chain

result = force_multiplier_chain(
    task="Add OCO bracket retry-with-jitter to the live BTC venue",
    workspace="/c/EvolutionaryTradingAlgo",
    max_tokens=2000,
)

print(result.plan.text)         # CLAUDE — architectural plan
print(result.implement.text)    # DEEPSEEK — implementation
print(result.verify.text)       # CODEX — verification / test plan
print(result.total_cost_usd)    # only counts DeepSeek API spend
print(result.fallbacks_used)    # any provider that fell back, with reason
```

**Skip a stage** when a provider is unavailable or unnecessary:

```python
# Codex out of monthly quota? Skip verify:
result = force_multiplier_chain(
    task="Refactor X",
    skip=("verify",),
)

# Just want bulk implementation, no architecture plan first?
result = force_multiplier_chain(
    task="Generate 50 boilerplate dataclasses",
    skip=("plan", "verify"),
)
```

## Routing table

24 task categories, mapped at policy-time in [`brain/model_policy.py`](../brain/model_policy.py):

```
CLAUDE   (7 tasks):  red_team_scoring, gauntlet_gate_design, risk_policy_design,
                     architecture_decision, adversarial_review, state_machine_design,
                     code_review

CODEX    (4 tasks):  debug, test_execution, security_audit, computer_use_task

DEEPSEEK (13 tasks): strategy_edit, test_run, refactor, skeleton_scaffold, doc_writing,
                     data_pipeline, log_parsing, simple_edit, commit_message,
                     formatting, lint_fix, trivial_lookup, boilerplate
```

Adding a new category: edit `_CATEGORY_TO_TIER` and `_CATEGORY_TO_PROVIDER` in `brain/model_policy.py`. The test `test_every_category_has_a_tier` will fail if you forget.

## Failure modes & fallback semantics

`MultiModelResponse.fallback_used` is `True` whenever the preferred provider was unavailable and DeepSeek handled the task. Possible `fallback_reason` values:

| Reason | Cause | Fix |
|---|---|---|
| `claude CLI not installed` | npm package missing | `npm install -g @anthropic-ai/claude-code` |
| `claude CLI not authenticated` | OAuth not set up | `claude login` |
| `claude monthly quota exhausted` | Pro plan limit hit | wait for reset / upgrade plan |
| `claude CLI timed out` | network or long task | check connectivity / increase `ETA_CLI_TIMEOUT_SEC` |
| `codex CLI not authenticated` | OAuth not set up | `codex login` |
| `codex monthly quota exhausted (ChatGPT Plus/Pro monthly limit)` | usage cap hit | wait for billing-cycle reset |

The auth/quota detection lives in `multi_model._classify_cli_failure` and only matches on precise patterns (`api error: 401`, `usage limit`, etc.) — it will not false-positive on legitimate CLI output that happens to contain words like "please run".

## Environment variables

| Var | Purpose | Default |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek V4 API key | required for DeepSeek path |
| `ETA_CLAUDE_CLI` | Override claude binary | `claude` (PATH) → `npx @anthropic-ai/claude-code` |
| `ETA_CODEX_CLI` | Override codex binary | `codex` (PATH) → `npx codex` |
| `ETA_CLI_TIMEOUT_SEC` | Per-call CLI timeout | `300` |
| `ETA_CODEX_MODEL_OVERRIDE` | Force a specific Codex model | `gpt-5.4` (subscription-compatible) |
| `ANTHROPIC_API_KEY` | (optional) bypass OAuth, use API directly | unset |
| `OPENAI_API_KEY` | (optional) for OpenAI API path | unset |

> **Important: parent-process env wins over `.env`.** The loader uses
> `override=False` so credentials set by systemd, your shell profile, or CI
> always take precedence. If the probe says `API key rejected` but your `.env`
> looks correct, check `echo $env:DEEPSEEK_API_KEY` (PowerShell) or
> `printenv DEEPSEEK_API_KEY` — there may be a stale value at the user/system
> level that's overriding the `.env`. Either update or unset that env var.

Override examples:

```bash
# Force npx invocation even if claude is on PATH (useful for CI):
export ETA_CLAUDE_CLI="npx @anthropic-ai/claude-code"

# Use ANTHROPIC_API_KEY instead of OAuth (Pro plan not required):
export ANTHROPIC_API_KEY=sk-ant-...
```

## File layout

```
eta_engine/
├── brain/
│   ├── llm_provider.py      ← API path (DeepSeek + OpenAI/Anthropic native)
│   ├── cli_provider.py      ← CLI path (Claude + Codex via subscription)
│   ├── model_policy.py      ← TaskCategory → ForceProvider routing
│   └── multi_model.py       ← orchestrator: route_and_execute, force_multiplier_chain
├── scripts/
│   └── force_multiplier_health.py   ← health probe with --live mode
└── docs/
    └── FORCE_MULTIPLIER.md  ← this file
```
