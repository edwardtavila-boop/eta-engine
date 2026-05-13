"""Feature flag system for ETA Engine graduated rollout (wave-6, 2026-04-27).

Read-only per-feature env-var gates so the operator can flip parts of
the system on/off WITHOUT redeploying. Defaults are conservative
(everything OFF for live trading; everything ON for paper).

Usage::

    from eta_engine.brain.feature_flags import is_enabled, ETA_FLAGS

    if is_enabled("BANDIT_LIVE_ROUTING"):
        arm = bandit.choose_arm(...)
        verdict = arm.policy(req, ctx)
    else:
        verdict = evaluate_request(req, ctx)  # always champion

    # Inspect what's currently flipped
    print(ETA_FLAGS.snapshot())

Flags are SET via env vars with the prefix ``ETA_FF_<NAME>``. e.g.::

    ETA_FF_BANDIT_LIVE_ROUTING=true
    ETA_FF_CONTEXTUAL_BANDIT=true
    ETA_FF_PER_BOT_PRE_FLIGHT=false   # leave bots on legacy _ask_jarvis

This is the SINGLE knob for "what's live". Operator runs::

    Get-ChildItem env: | Where-Object {$_.Name -like "ETA_FF_*"}

to inspect the entire rollout state at a glance.

Ladder of safety (recommended order to flip on)
-----------------------------------------------
  1. PRE_FLIGHT_CORRELATION   -- correlation throttle in pre_flight
  2. KAIZEN_DAILY_CLOSE       -- nightly +1 ticket
  3. CRITIQUE_NIGHTLY         -- 2nd reviewer
  4. CALIBRATION_DAILY        -- Platt sigmoid fit
  5. ANOMALY_SCAN_15M         -- KS-stat regime alerter
  6. BANDIT_LIVE_ROUTING      -- begin epsilon-greedy traffic split
  7. CONTEXTUAL_BANDIT        -- upgrade to Thompson-sampling per-context
  8. AUTO_PROMOTE             -- bandit auto-promotes a winner

Each flag is independent. Earlier ones provide signal for whether to
flip later ones.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class _FlagDef:
    name: str
    default: bool
    description: str


# Single source of truth for what's flippable + what each flag does.
_FLAGS: list[_FlagDef] = [
    _FlagDef(
        "PRE_FLIGHT_CORRELATION",
        default=True,
        description="Correlation throttle in bot_pre_flight. Default ON: cheap dict lookup.",
    ),
    _FlagDef(
        "KAIZEN_DAILY_CLOSE",
        default=True,
        description="Nightly run of run_kaizen_close_cycle.py. Default ON: doctrine-mandated.",
    ),
    _FlagDef("CRITIQUE_NIGHTLY", default=True, description="2nd reviewer over the day's audit. Default ON: pure-read."),
    _FlagDef(
        "CALIBRATION_DAILY",
        default=True,
        description="Daily Platt sigmoid fit. Default ON: pure-read + write to state/.",
    ),
    _FlagDef(
        "ANOMALY_SCAN_15M",
        default=True,
        description="Every-15min KS-stat regime alerter. Default ON: read-only with cooldown.",
    ),
    _FlagDef(
        "BANDIT_LIVE_ROUTING",
        default=False,
        description="Bandit splits LIVE traffic across registered candidates. Default OFF: wait for confidence.",
    ),
    _FlagDef(
        "CONTEXTUAL_BANDIT",
        default=False,
        description="Use Thompson-sampling per-context bandit instead of global epsilon-greedy. Default OFF.",
    ),
    _FlagDef(
        "AUTO_PROMOTE",
        default=False,
        description=(
            "When promotion-check finds a winner, auto-flip the champion "
            "(skip operator approval). Default OFF: HUMAN IN THE LOOP."
        ),
    ),
    _FlagDef(
        "PER_BOT_PRE_FLIGHT",
        default=False,
        description=(
            "Bots route through bot_pre_flight() instead of legacy _ask_jarvis() direct. Default OFF: opt-in."
        ),
    ),
    _FlagDef(
        "ONLINE_LEARNING",
        default=False,
        description="Per-bot OnlineUpdater observes fills and can safely shrink cold setup buckets. Default OFF.",
    ),
    _FlagDef(
        "PORTFOLIO_REBALANCER",
        default=False,
        description="Weekly Sharpe-rank reallocation across bots via set_equity_ceiling. Default OFF.",
    ),
    _FlagDef(
        "VERDICT_WEBHOOK",
        default=False,
        description="Forward verdict stream to Slack/Discord. Default OFF: requires ETA_VERDICT_WEBHOOK_URL.",
    ),
    _FlagDef(
        "NOTION_EXPORT", default=False, description="Daily digest -> Notion/Airtable. Default OFF: requires creds."
    ),
    _FlagDef(
        "V22_SAGE_MODULATION", default=False, description="v22 candidate uses sage confluence. Default OFF: in dev."
    ),
]


# Indexed
_FLAG_BY_NAME: dict[str, _FlagDef] = {f.name: f for f in _FLAGS}


def is_enabled(name: str) -> bool:
    """Return True if flag is set true (env var override) OR default-true.

    Unknown flag names return False (safe default for unknowns).
    """
    flag = _FLAG_BY_NAME.get(name)
    if flag is None:
        return False
    env_val = os.environ.get(f"ETA_FF_{name}", "")
    if not env_val:
        return flag.default
    return env_val.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class _Flags:
    """Singleton helper exposing snapshot + listing."""

    def snapshot(self) -> dict[str, bool]:
        """All flag states (env-overridden if set)."""
        return {f.name: is_enabled(f.name) for f in _FLAGS}

    def descriptions(self) -> dict[str, str]:
        return {f.name: f.description for f in _FLAGS}

    def diff_from_default(self) -> dict[str, bool]:
        """Flags whose effective state differs from their default."""
        return {f.name: is_enabled(f.name) for f in _FLAGS if is_enabled(f.name) != f.default}


ETA_FLAGS = _Flags()
