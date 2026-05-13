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
│   ├── llm_provider.py             ← API path (DeepSeek + OpenAI/Anthropic native)
│   ├── cli_provider.py             ← CLI path (Claude + Codex via subscription)
│   ├── model_policy.py             ← TaskCategory → ForceProvider routing
│   ├── multi_model.py              ← orchestrator: route_and_execute, chain
│   └── multi_model_telemetry.py    ← JSONL audit log + summary aggregation
├── scripts/
│   ├── force_multiplier_health.py  ← health probe with --live, --json, --json-out
│   ├── fm.py                       ← CLI: ask | chain | status | log
│   ├── fm_cost_report.py           ← per-day spend rollup + breakdowns
│   └── install_fm_health_task.ps1  ← Task Scheduler installer (every 4h)
├── state/
│   ├── force_multiplier_calls.jsonl  ← telemetry log (auto-created on first call)
│   └── fm_health.json              ← scheduled probe snapshot (when task installed)
└── docs/
    └── FORCE_MULTIPLIER.md         ← this file
```

## Migrating a `chat_completion` call to `route_and_execute`

The codebase still has direct `chat_completion()` calls predating the
Force-Multiplier integration. Each one is a chance to pick up automatic
telemetry, per-call budget enforcement, fallback semantics, and the
correct provider for the task type — for free, with no behavior change
on the happy path.

### When to migrate

**Always migrate** unless one of the exceptions below applies. The
pattern is: route by task purpose, not by which model you happen to
have configured. Examples already migrated:

- [`brain/jarvis_v3/llm_narrative.py`](../brain/jarvis_v3/llm_narrative.py) — verdict-to-prose narrative
- [`brain/jarvis_v3/sage/narrative.py`](../brain/jarvis_v3/sage/narrative.py) — sage report explanation
- [`deploy/scripts/run_task.py::_task_self_test`](../deploy/scripts/run_task.py) — daily LLM health ping

### When NOT to migrate (rare)

Skip the migration and leave a `# NOTE` comment when:

1. **The call deliberately targets a specific provider's caching/feature**
   that the orchestrator would route around. Example:
   `_task_prompt_warmup` in `run_task.py` populates the *Anthropic*
   prompt cache — routing through `route_and_execute` with HAIKU tier
   would send to DeepSeek, defeating the purpose.

2. **The call is in a tight inner loop** (e.g. per-tick) where the
   subprocess overhead of CLI providers would matter. Use
   `force_provider=ForceProvider.DEEPSEEK` if you want the orchestrator's
   safety nets but a guaranteed API path.

3. **You explicitly need the raw `LLMResponse` shape** (e.g. inspecting
   `reasoning_content` for thinking-model output). The orchestrator
   wraps responses in `MultiModelResponse` which doesn't expose that.

### The mechanical recipe

**Before** (direct API call):
```python
from eta_engine.brain.llm_provider import ModelTier, chat_completion

resp = chat_completion(
    tier=ModelTier.HAIKU,
    system_prompt=SYS,
    user_message=prompt,
    max_tokens=300,
    temperature=0.5,
)
```

**After** (Force-Multiplier orchestrator):
```python
from eta_engine.brain.model_policy import TaskCategory
from eta_engine.brain.multi_model import route_and_execute

resp = route_and_execute(
    category=TaskCategory.DOC_WRITING,   # pick the category that matches
    system_prompt=SYS,
    user_message=prompt,
    max_tokens=300,
    temperature=0.5,
    max_cost_usd=0.005,                  # hard ceiling — refuses runaway calls
)
```

### Picking the right `TaskCategory`

The category determines (a) which provider the call routes to and
(b) which tier (model size) is used. Pick from:

| Work type | Category | Routes to | Tier |
|---|---|---|---|
| Architecture / design / risk policy | `ARCHITECTURE_DECISION`, `RISK_POLICY_DESIGN`, `STATE_MACHINE_DESIGN` | CLAUDE | OPUS |
| Red-team / adversarial review | `RED_TEAM_SCORING`, `ADVERSARIAL_REVIEW` | CLAUDE | OPUS |
| Code review (pre-merge) | `CODE_REVIEW` | CLAUDE | SONNET |
| Strategy / refactor / scaffold | `STRATEGY_EDIT`, `REFACTOR`, `SKELETON_SCAFFOLD` | DEEPSEEK | SONNET |
| Narrative / docs / data pipeline | `DOC_WRITING`, `DATA_PIPELINE` | DEEPSEEK | SONNET |
| Boilerplate / formatting / commits | `BOILERPLATE`, `FORMATTING`, `COMMIT_MESSAGE` | DEEPSEEK | HAIKU |
| Log parsing / trivial lookups | `LOG_PARSING`, `TRIVIAL_LOOKUP`, `SIMPLE_EDIT` | DEEPSEEK | HAIKU |
| Debugging (run-and-fix) | `DEBUG`, `TEST_EXECUTION` | CODEX | SONNET |
| Security / computer-use | `SECURITY_AUDIT`, `COMPUTER_USE_TASK` | CODEX | SONNET |

Don't agonize — pick the closest match. The wrong category at most
sends the call to a sub-optimal provider; the response semantics are
identical and the fallback chain catches everything.

### Setting `max_cost_usd`

Compute the worst case: `max_tokens × output_rate / 1_000_000`. For
DeepSeek V4 Flash output ($0.28/1M), 1000 tokens = $0.00028. Pick a
ceiling 10–50× the worst case as a guardrail against runaway loops.
For the migrated `llm_narrative` example: 600 tokens worst case
(verbose mode) × $0.28/1M = $0.000168, capped at $0.005 (≈30× margin).

### Verifying the migration

After migrating, confirm the call shows up in the telemetry log:

```bash
# Trigger your code path (run a test, fire a task, etc.) then:
python -m eta_engine.scripts.fm log -n 5
```

You should see a row with the matching `category`, the expected
`provider`, and `fallback_used=N`. If `fallback_used=Y`, the
preferred provider was unavailable — check `fm status` to see why.
> 2026-05-13 operator policy: Codex is the subscription-backed architect/verifier, DeepSeek V4 is the only paid API lane, and Claude/Anthropic API usage is disabled. Historical Claude sections below are legacy context unless `ETA_ENABLE_CLAUDE_CLI=1` is deliberately set for a manual experiment.
