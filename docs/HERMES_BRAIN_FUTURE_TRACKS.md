# Hermes-as-Brain: Future Tracks Menu

After landing the first 5 brain/OS tracks (event stream, write-back, memory,
skills, multi-channel), there's a long bench of follow-on tracks that can
elevate Hermes from "operator interface" to "operator extension." This
doc enumerates them with effort estimates, payoff, and order
recommendations so future you (or another agent) can pick up the next one
with full context.

The numbering continues from the original 5.

---

## Track 6 — Causal Layer Exposure

**What:** Hermes today can see verdicts but not *causal explanations*.
A verdict says "PROCEED at 0.7×, dissent=mean_revert". A causal layer
would say "the EMA momentum learner crossed +1.4σ, which is correlated
with PROCEED 73% of the time, and the dissent came because mean_revert
detected a 2σ overshoot from the 20-period MA." Same data, different
framing — Hermes can reason about *why* a school dissented rather than
just *that* it did.

**How:** Add a new MCP tool `jarvis_explain_consult_causal(consult_id)`
that runs a synthetic counterfactual: it takes the consult inputs, varies
each school's signal by ±1σ, observes which verdict flips, and reports
the marginal-effect attribution.

**Effort:** Medium — needs a new module `causal_attribution.py` that
re-runs the verdict cascade with perturbed inputs. ~400 LOC.

**Payoff:** Huge for the *anomaly-investigator* skill — instead of "loss
streak shares dissent from mean_revert school", you get "all 5 losses
were within 0.2σ of the buy-zone boundary, marginal-effect analysis says
the cascade was 50/50 → strategy operating at its edge, regime change
not broken strategy."

**Order:** Before Track 7 (replay). Causal attribution is the prerequisite
for replay being useful — replay without attribution just says "different
verdicts came out", not *why*.

---

## Track 7 — Replay & Counterfactual Engine

**What:** Operator asks "what would JARVIS have done at 14:23 yesterday
if I had pinned momentum=1.5 instead?" Today: impossible. With replay:
Hermes loads the historical consult inputs, applies the hypothetical
override, re-runs portfolio_brain + hot_learner + the verdict cascade,
and shows the alt verdict.

**How:** New module `replay_engine.py` reading `jarvis_trace.jsonl` and
`hermes_overrides.json` history. Two MCP tools: `jarvis_replay_at(ts)`
and `jarvis_counterfactual(ts, override_overrides)`. Reuses the existing
cascade code — the trace already stores enough inputs to reconstruct.

**Effort:** Medium-high — ~600 LOC + tests. The hard part is making the
schools deterministic on replay (some have RNG seeds for bootstrap
intervals — need to capture seeds in the trace, which means amending
the trace schema and bumping its version).

**Payoff:** Genuinely game-changing. Operator can:
* Stress-test "what if I had pinned X" before doing it for real.
* Investigate "the system did the right thing 3 days ago — why did
  the same setup fail today?" with a deterministic comparison.
* Use the same engine in walkforward backtests.

**Order:** After Track 6 (causal layer).

---

## Track 8 — Regime Classifier + Adaptive Overlay

**What:** Hermes runs a lightweight regime classifier (volatility cluster,
trend-vs-range, correlation-cluster heat) on intraday market features
and selects from a small set of pre-defined override packs. Operator
defines packs once ("calm-trend pack: momentum schools ×1.1, mean_revert
×0.9"), Hermes applies the matching pack automatically.

**How:** Two parts.
1. Classifier — feature extractor + small XGBoost or rule-based ladder.
2. Pack store + auto-apply — extends `hermes_overrides.py` with
   `apply_pack(name)`.

**Effort:** High. Classifier needs training data; pack store is simple.
The thing that takes long is figuring out which features actually
discriminate regimes for *your specific bot fleet*.

**Payoff:** Compounding edge — every override the operator pins is a
hint to the classifier about what regime calls for what pack. After 30
days of training data, the classifier can pre-empt the operator.

**Order:** After Track 7 (because the replay engine is needed to
backtest pack performance).

---

## Track 9 — Multi-Agent Council (Mixture of Agents)

**What:** For high-stakes decisions (kill-switch trip, retire a
production-tier bot, sizing change >50%), the decision goes through a
council: JARVIS produces its verdict, then Hermes-with-DeepSeek-V4-Pro
critiques it, then a third agent (Claude Sonnet 4 or GPT-5 Codex)
audits both. Three voices, vote for consensus.

**How:** A new orchestrator module + a "council" decoration on selected
MCP tools. Each agent is queried in parallel via Hermes's multi-provider
support (already configured for OpenRouter, OpenAI, Anthropic). Token
cost is non-trivial but only fires for marked-high-stakes calls.

**Effort:** Medium. The hardest part is defining *which* decisions
trigger the council (false positives waste tokens; false negatives miss
the protection).

**Payoff:** Reduces single-model failure modes. If DeepSeek is having an
off-day and would have agreed with a bad JARVIS call, the third voice
catches it.

**Order:** After Track 8. Lower priority than the others because most
JARVIS decisions are *already* low-stakes (just sizing nudges).

---

## Track 10 — Live Trade Narrator + Journal

**What:** Hermes turns every consult into a paragraph and appends it to
a daily journal. End of week, Hermes generates a 1-page narrative
synthesis. Operator reviews on Sunday — what played out, what surprised,
what to adjust.

**How:** New skill `jarvis-trade-narrator` that subscribes to the trace
stream and emits one paragraph per `final_verdict=ENTER` and one per
`action=EXIT/STOP_LOSS`. Saves to `var/eta_engine/state/trade_journal/YYYY-MM-DD.md`.

**Effort:** Low — ~200 LOC + a skill markdown file. Leans heavily on the
existing trace + hermes memory tools.

**Payoff:** Operator reflection becomes effortless. Pattern recognition
across days. Becomes prep material for Sunday-evening review.

**Order:** Can ship in parallel with anything else. Pure value-add.

---

## Track 11 — Adversarial Inspector

**What:** A devil's-advocate skill that runs *against* JARVIS's verdict.
Whenever JARVIS says PROCEED, the inspector argues for HOLD (and vice
versa). It's not voting — it's a stress test. Operator can ask "what
would the inspector say about consult abc123?" and get the strongest
counter-argument.

**How:** A skill + a wrapper around `jarvis_explain_verdict` that
restates the consult inputs and asks the LLM to argue the opposite
verdict, citing the same evidence. No new code, no new state — just a
prompt-engineered skill.

**Effort:** Low — one skill markdown file.

**Payoff:** Catches confirmation bias. Useful for new-strategy validation
("the inspector says this strategy works ONLY in a calm-trend regime
— makes sense, let's add a regime filter").

**Order:** Anytime. Low-effort high-leverage.

---

## Track 12 — Performance Attribution Cube

**What:** Roll up trade outcomes by (school × asset × regime × hour-of-day)
into a multi-dim attribution cube. Operator asks "which school is paying
the bills in MNQ after 2pm?" and gets a slice.

**How:** New `attribution_cube.py` that joins the trace stream with
trade_closes.jsonl. Probably a small SQLite + a few SQL aggregations.
Hermes wraps with `jarvis_attribution(slice_by, filter)`.

**Effort:** Medium — ~500 LOC + SQL views. The cube schema needs
thought to balance dimensionality vs query speed.

**Payoff:** Decisions like "retire momentum on BTC after losing edge"
become data-driven instead of vibes-driven. Compounds with Track 8
(regime classifier).

**Order:** After Track 7. Reads naturally next to the replay engine.

---

## Track 13 — Position Sizing Optimizer (Kelly + Drawdown Penalty)

**What:** Each morning Hermes runs an optimization: given the past 30
days of per-bot R-distributions, recompute optimal sizing factors using
fractional Kelly with a drawdown-tolerance penalty. The new factors
become baseline `size_modifier` values that operator can accept (apply
to all bots) or reject (keep current).

**How:** Add `kelly_optimizer.py`. Computes per-bot µ, σ, autocorr, then
runs constrained optimization. Output written to a new sidecar
`recommended_sizing.json`. New MCP tool `jarvis_recommend_sizing()`
fetches it; existing `jarvis_set_size_modifier` is the apply path.

**Effort:** High — the math itself is contained (200 LOC) but tuning the
drawdown-tolerance penalty for the operator's loss aversion takes
multiple cycles.

**Payoff:** Catches "bot is producing alpha but you're under-sized it"
and the inverse. Direct R-improvement once tuned.

**Order:** After Track 12 (attribution cube). The optimizer needs clean
per-bot R-streams to consume.

---

## Track 14 — Inter-Agent Bus

**What:** Multiple Claude Code sessions (one for strategy research, one
for execution monitoring, one for operations) talk to Hermes over the
api_server. Hermes routes messages, persists per-agent state, and
prevents conflicting actions (two agents trying to retire the same bot).

**How:** Add a `gateway/agents.py` registry + a new MCP tool
`jarvis_register_agent(agent_id, role)`. Conflict-detection layer on
top of the destructive tools — second agent attempting the same
retire returns `status=LOCKED_BY_OTHER_AGENT, locked_by=...`.

**Effort:** Medium-high — locking & coordination is fiddly. ~600 LOC + tests.

**Payoff:** Lets the operator run a "swarm" — 5 specialized agents that
divide and conquer. Especially useful overnight when one Claude Code
session might be researching while another is monitoring live trades.

**Order:** Last in this list — needs the brain to be more "load-bearing"
first.

---

## Track 15 — Voice + Wake Word

**What:** Operator says "Hey JARVIS, kill atr_breakout for the day" out
loud, gets a verbal confirmation back. Pure UX track.

**How:** Add a local Whisper STT + TTS layer that runs on the desktop
(not VPS — latency-sensitive). Push voice transcripts as text to Hermes,
synth Hermes replies back to audio. Open-source stack: whisper.cpp +
piper TTS.

**Effort:** Medium. Desktop-side wiring, not VPS-side.

**Payoff:** Hands-free during active sessions. Especially useful when
the operator is doing manual research alongside the bots — voice removes
the context-switching cost.

**Order:** Anytime — orthogonal to the rest.

---

## Track 16 — Sentiment + News Overlay

**What:** Hermes subscribes to a news/social-sentiment feed (LunarCrush,
Bigdata, an RSS aggregator) and tags consults with current sentiment
regime. Override pins can then condition on sentiment ("trim momentum
when crypto twitter goes euphoric").

**How:** Use the existing MCP `lunarcrush` integration (already on the
operator's connector list). New extractor module that distills sentiment
to a 0–1 fear/greed scalar + topic flags. Stored in `var/eta_engine/state/sentiment.json`,
refreshed every 15 min. Operator overrides can read it.

**Effort:** Low-medium — the data feeds already exist; just need the
glue.

**Payoff:** External-information edge. Most quant strategies are
price-only; this layer says "BTC twitter is euphoric, momentum overlay
×0.7 for the next 4h". Useful for crypto bots specifically.

**Order:** Anytime, but most useful AFTER Track 8 (regime classifier)
can consume it as a feature.

---

## Track 17 — Live Risk Topology Visualizer

**What:** Real-time topology view of the fleet — bots as nodes, sized by
notional, colored by R-today, edges showing correlation. Rendered in
Claw3D as a force-directed graph. Click a node → consult history.

**How:** Add a new MCP tool `jarvis_topology()` that returns the graph
JSON. Claw3D consumes via the inbound webhook from Track 5. The
correlation edges come from `dashboard_events.jsonl` correlation snapshots.

**Effort:** Medium — graph JSON is straightforward, Claw3D rendering
takes a JS slice on the office side.

**Payoff:** Operator sees the *shape* of risk, not just the numbers.
Especially valuable when fleet grows past ~20 bots and tabular views
stop scaling.

**Order:** After Track 5 (webhook outbound is the plumbing).

---

## Recommended cadence

If you want a tight 90-day plan from here:

| Week | Track | Rationale |
|---|---|---|
| 1 | T10 trade narrator | Low effort, high reflection value, kicks off Sunday-review habit |
| 2-3 | T6 causal layer | Foundation for everything else; biggest single insight unlock |
| 4-6 | T7 replay engine | Highest-leverage analyst tool once causal is in |
| 7-8 | T12 attribution cube | Quant-grade decision data |
| 9-10 | T11 adversarial inspector | Defense layer; ships fast |
| 11-12 | T8 regime classifier | Auto-overlays; closes the loop |

T9 (council), T13 (Kelly), T14 (multi-agent), T15 (voice), T16 (sentiment),
T17 (topology) are bonus tracks — pick based on what surfaces friction
in your daily workflow.

---

## What to avoid

Some directions sound exciting but I think are traps for this setup:

* **Reinforcement learning from human feedback (RLHF)** on Hermes/JARVIS
  output. You don't have enough trades to train; you'd be fitting noise.
  Use rule-based heuristics + memory until you've got 1000+ labeled
  episodes.
* **Custom LLM fine-tuning** for trading vocabulary. DeepSeek-V4-Pro
  already handles every trading term you'll throw at it; fine-tuning
  buys nothing and adds an ops burden.
* **Replacing JARVIS as the policy authority.** Hermes is the
  interface/brain; JARVIS is the decision engine. Conflating them costs
  you the auditability that the current split provides.
* **Anything that puts Hermes in the *hot path* of order routing.** The
  20-100ms latency between Hermes (deepseek) and JARVIS (in-process)
  would devastate fast strategies. Hermes assists; the supervisor + bots
  execute.
