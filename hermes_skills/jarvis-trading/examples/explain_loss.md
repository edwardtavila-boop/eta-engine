# Example: explain a loss

**Operator:** why did vwap_mr_mnq lose today?

**Hermes:** *(invokes `jarvis_trace_tail` with `n=50`, filters for `bot_id="vwap_mr_mnq"` and the most recent realized loss; grabs the `consult_id`)*

*(invokes `jarvis_explain_verdict` with that `consult_id`)*

> Consult `7c91d2` — verdict APPROVED at 13:42 ET, realized −1.4R.
>
> Evidence:
> - Regime classifier: trend (confidence 0.71) — VWAP-mean-reversion edge is degraded in trend regimes.
> - Hot-learner weight for mean-reversion school: 0.42 (down from 0.61 yesterday).
> - Upcoming-event filter: clear — no econ release within 30 min.
> - Portfolio brain: net exposure within bounds; no veto.
>
> The verdict approved because the bot-level R-budget had room and the per-school Sharpe gate hadn't tripped. Loss came from the regime — the kaizen loop will likely down-weight the school further tomorrow.

**Operator:** worth retiring it?

**Hermes:** Not on a single-day loss. Suggest waiting for the 06:00 kaizen verdict before considering retire — JARVIS needs two confirming runs to apply.
