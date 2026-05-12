# Example: emergency kill switch

**Operator:** shut everything down, news is breaking.

**Hermes:** Restating: you want me to invoke `jarvis_kill_switch` with full-fleet scope. This will halt every active bot and is irreversible until you manually re-enable them.

To confirm, please type the exact phrase **`kill all`** in your next message. (Anything else — "shut it down", "kill it", "yes" — will be rejected. This is intentional; the phrase guards against me misinterpreting urgency.)

**Operator:** kill all

**Hermes:** *(invokes `jarvis_kill_switch` with `reason="operator: news-driven emergency stop"`, `confirm_phrase="kill all"`)*

> Killed at 2026-05-11T18:22:04Z. Scope: all 14 bots. Audit log: `var/eta_engine/state/hermes_actions.jsonl`.
> Re-enable manually via the JARVIS console when ready.

---

**Counter-example — rejected:**

**Operator:** kill everything now

**Hermes:** I can't invoke the kill switch without the exact phrase `kill all` in your message. Please retype if you want me to proceed.

*(No tool call is made. The exact-phrase gate is enforced by JARVIS server-side and by this skill's SOUL — both layers must agree.)*
