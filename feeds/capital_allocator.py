"""Capital Allocation Engine — futures-first pool system with performance-weighted sizing.

Pools (operator directive 2026-05-13):
  FUTURES   (100%): MNQ, NQ, MES, GC, CL, NG, ZN, EUR via IBKR — primary fleet.
                    *Crypto exposure*: CME micro crypto futures (MBT, MET) routed
                    through IBKR. NOT Alpaca spot — Alpaca lane is CLOSED.
                    Prop-firm crypto routes through Tradovate-when-enabled,
                    behind ETA_TRADOVATE_ENABLED=1 + per-account credentials.
  SPOT      (0%):   CLOSED per operator directive. BTC/ETH/SOL bots remain in
                    the registry with extras.deactivated=True as audit anchors;
                    re-opening requires both the venue layer AND the registry
                    flag to be flipped together.
  LEVERAGED (0%):   retired sleeve; CME micro crypto futures live in FUTURES.

POOL_SPLIT defaults to {futures: 1.0, spot: 0.0, leveraged: 0.0}. Env override
via ETA_POOL_*_FRAC is available for forward flexibility but should NOT be
used to re-open spot without also re-opening the Alpaca venue lane.

Within each pool, capital is allocated by multi-session performance:
  - Positive PnL across sessions → weighted higher
  - Negative PnL → zero allocation (paused)
  - Allocation is proportional to total_pnl among profitable bots
"""

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


@dataclass
class BotAllocation:
    bot_id: str
    symbol: str
    pool: str  # "spot", "futures", "leveraged"
    weight: float  # 0.0 - 1.0 within pool
    capital: float  # absolute capital allocated
    pnl_total: float
    win_rate: float
    sessions: int
    status: str  # "active", "paused", "no_data"
    # Wave-18: tier metadata for prop-fund routing
    tier: str = "TIER_CANDIDATE"  # TIER_PROP_READY / TIER_DIAMOND / TIER_CANDIDATE


@dataclass
class PortfolioAllocation:
    total_capital: float
    spot_pool: dict[str, Any] = field(default_factory=dict)
    futures_pool: dict[str, Any] = field(default_factory=dict)
    leveraged_pool: dict[str, Any] = field(default_factory=dict)
    bots: dict[str, BotAllocation] = field(default_factory=dict)


# Asset class → pool mapping
SPOT_SYMBOLS = {"BTC", "ETH", "SOL", "ADA", "AVAX", "LINK", "DOGE"}
FUTURES_SYMBOLS = {"MNQ", "MNQ1", "NQ", "NQ1", "MES", "M2K", "GC", "CL", "NG", "ZN", "6E", "EUR"}
LEVERAGED_SYMBOLS = {"MBT", "MET"}

# Pool allocations.
#
# Defaults (2026-05-13): futures-only. The operator explicitly cellared
# Alpaca/spot while the fleet prepares for futures, crypto futures, and
# commodity prop-firm work. Spot can be re-enabled later through the env
# override below without changing code.
#
# Env override for live tuning without a code change:
#   ETA_POOL_FUTURES_FRAC
#   ETA_POOL_SPOT_FRAC
#   ETA_POOL_LEVERAGED_FRAC
# All three must sum to 1.0 ± 0.001. Invalid env values silently fall back
# to the hard-coded defaults.


def _resolve_pool_split() -> dict[str, float]:
    """Read env-overridden pool fractions, validate, fall back to defaults."""
    defaults = {"futures": 1.0, "spot": 0.0, "leveraged": 0.0}
    try:
        out = {
            "futures": float(os.environ.get("ETA_POOL_FUTURES_FRAC", defaults["futures"])),
            "spot": float(os.environ.get("ETA_POOL_SPOT_FRAC", defaults["spot"])),
            "leveraged": float(os.environ.get("ETA_POOL_LEVERAGED_FRAC", defaults["leveraged"])),
        }
    except (TypeError, ValueError):
        return defaults
    # Validate: every fraction must be in [0,1] AND they must sum to 1.0
    if any(v < 0 or v > 1 for v in out.values()):
        return defaults
    total = sum(out.values())
    if abs(total - 1.0) > 0.001:
        return defaults
    return out


POOL_SPLIT = _resolve_pool_split()

# DIAMOND BOTS — protected from auto-kill, always get minimum capital.
#
# Stats are R-multiple basis (dimension-free, immune to position-sizing
# bugs that have plagued the USD ledger). Source: canonical dual-source
# trade-closes archive (eta_engine/state/jarvis_intel/trade_closes.jsonl  # HISTORICAL-PATH-OK
# + var/eta_engine/state/jarvis_intel/trade_closes.jsonl, deduped).
# Snapshot 2026-05-12 (wave-14 fleet expansion).
#
# Wave-14 expansion rationale:
# Operator mandate to "conquer futures + commodities + crypto" by
# bringing all paper-soak strategies with strong R-evidence into the
# diamond fleet for data gathering. The promotion gate's hard
# H4_calendar_days=5 requirement is paper-trading-irrelevant — we WANT
# more bots accumulating data, not fewer.
#
# Quarantined / NOT promoted:
#   - mym_sweep_reclaim: corrupt R-values (multiple R=+50/+80/+100 on
#     pnl=$1.25 — same scale-bug pattern as the eur_sweep records the
#     diamond_data_sanitizer quarantined; needs sanitizer pass before
#     promotion can be considered)
#   - mbt_overnight_gap, mbt_rth_orb, mbt_funding_basis,
#     mbt_sweep_reclaim: all trading (n=58-129) but realized_r=0
#     across the board; the R-multiple writer is broken for the MBT
#     family. Must fix the R writer for these bots before they can be
#     R-classified by the watchdog.
DIAMOND_BOTS: set[str] = {
    # ── Tier 1: large-sample sage learners ──────────────────────
    "mnq_futures_sage",  # n=1267 cum_r=+0.82R wr=55%  (marginal-but-large)
    "nq_futures_sage",  # n=1249 cum_r=+0.85R wr=57%  (marginal-but-large)
    # ── Tier 2: confirmed-strong sweep reclaim ──────────────────
    "m2k_sweep_reclaim",  # n=1151 cum_r=+533R  wr=70%  *PROMOTED 2026-05-12* (canonical-data kaizen)
    "eur_sweep_reclaim",  # n= 280 cum_r=+129R  wr=70%  (4/4 sessions positive)
    "mgc_sweep_reclaim",  # n= 158 cum_r= +30R  wr=58%  (wave-3+5 chisel)
    # ── Tier 2 (wave-14: conquer all 3 verticals via IBKR FUTURES) ──
    # Wave-16 mandate (2026-05-12): the diamond fleet is IBKR-FUTURES-ONLY.
    # Alpaca spot is cellared (POOL_SPLIT["spot"]=0.0); Tradovate dormant.
    # Crypto exposure comes from CME micro crypto futures (MET/MBT) routed
    # through IBKR — NOT from BTC/ETH/SOL spot via Alpaca.
    "met_sweep_reclaim",  # n= 208 cum_r=+136R wr=69%  *wave-14* (CME MET futures via IBKR — highest avg_R in fleet)
    "mes_sweep_reclaim_v2",  # n= 416 cum_r=+136R wr=63%  *wave-14* (CME MICRO S&P FUTURES via IBKR)
    "eur_range",  # n= 124 cum_r= +64R wr=71%  *wave-14* (CME 6E EUROFX FUTURES via IBKR)
    "ng_sweep_reclaim",  # n= 243 cum_r= +91R wr=65%  *wave-14* (CME NG NAT GAS FUTURES via IBKR)
    "mes_sweep_reclaim",  # n= 197 cum_r= +56R wr=61%  *wave-14* (CME MICRO S&P FUTURES via IBKR, paired with v2)
    # NOT promoted (wave-16 IBKR-futures-only mandate):
    #   volume_profile_btc — Alpaca SPOT BTC; cellared per POOL_SPLIT.
    #     Strong R-edge (+121R/n=339) but the wrong broker for the
    #     prop-fund routing layer. If/when the operator re-activates
    #     spot crypto (currently POOL_SPLIT["spot"]=0.0), this bot
    #     can be reconsidered.
    # ── Tier 3: small-sample but positive ───────────────────────
    "cl_macro",  # n=   2 cum_r= +2.4R wr=100% (sample too small)
    "gc_momentum",  # n=   8 cum_r= +0.24R wr=50% (R-positive; USD-CRITICAL is a sizing artifact)
    # ── Tier 4: small-sample structurally negative ──────────────
    # These two are net-negative in R-multiples too. Kept under
    # protection because n is too small (4-8) for retirement to be
    # statistically justified. Watch for the n>=20 inflection point.
    "cl_momentum",  # n=   4 cum_r= -1.71R wr=25% (under-baked)
    "mcl_sweep_reclaim",  # n=   8 cum_r= -0.22R wr=50% (flat)
}

# Minimum capital allocation for diamond bots (always active)
DIAMOND_MIN_CAPITAL: float = 2000.0

# Minimum sessions required for allocation
MIN_SESSIONS = 2

# Path to allocation state
ALLOCATION_PATH = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\capital_allocation.json")

# ────────────────────────────────────────────────────────────────────
# Wave-18: TIER SYSTEM
# ────────────────────────────────────────────────────────────────────
# Diamonds are no longer a flat set — they have tiers that determine
# CAPITAL ROUTING and FAILURE-MODE RESPONSE.
#
#   TIER_PROP_READY  — top 3 by leaderboard composite score; earn real
#                       prop-fund capital allocation.  Eligibility is
#                       data-driven and recomputed each leaderboard run.
#   TIER_DIAMOND     — full diamond fleet with 3-layer protection.
#                       Receive DIAMOND_MIN_CAPITAL floor regardless of
#                       short-term P&L.  Paper-soak data accumulation.
#   TIER_CANDIDATE   — bots not yet diamonds but tracked by the
#                       promotion gate.  No capital floor; receive
#                       performance-weighted allocation only.
#
# The tier is computed dynamically from:
#   - DIAMOND_BOTS membership (TIER_DIAMOND if member)
#   - Leaderboard snapshot's prop_ready_bots set (TIER_PROP_READY upgrade)
#   - Otherwise TIER_CANDIDATE
#
# A bot can be TIER_PROP_READY AND TIER_DIAMOND simultaneously
# (PROP_READY is a SUPERSET of DIAMOND for eligible bots).

TIER_PROP_READY = "TIER_PROP_READY"
TIER_DIAMOND = "TIER_DIAMOND"
TIER_CANDIDATE = "TIER_CANDIDATE"

#: How much real capital each PROP_READY bot gets routed through IBKR.
#: This is a STARTING DEFAULT — operator overrides via the prop-fund
#: control surface once live data warrants scaling up.  Conservative
#: until the bots have proven themselves on real fills.
PROP_READY_CAPITAL_PER_BOT: float = 2500.0

#: Leaderboard receipt path.  capital_allocator reads this to know
#: which bots earned PROP_READY status in the most recent run.
LEADERBOARD_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state"
    r"\diamond_leaderboard_latest.json",
)


def classify_pool(bot_id: str) -> str:
    """Classify a bot into spot, futures, or leveraged pool by its ID."""
    bid_lower = bot_id.lower()
    # Micro crypto futures (MBT/MET on CME) — part of futures pool
    if any(x in bid_lower for x in ("mbt_", "met_")):
        return "futures"
    # Spot crypto (BTC/ETH/SOL)
    if any(x in bid_lower for x in ("btc_", "eth_", "sol_")):
        # Exclude eth_sweep_reclaim which is futures-like on ETH
        if "perp" in bid_lower or "futures" in bid_lower:
            return "futures"
        return "spot"
    if any(x in bid_lower for x in ("vwap_mr_btc", "volume_profile_btc", "funding_rate_btc")):
        return "spot"
    # Everything else is futures
    return "futures"


def load_prop_ready_bots(
    leaderboard_path: Path = LEADERBOARD_PATH,
) -> frozenset[str]:
    """Read the most recent leaderboard receipt and return the set of
    bots currently designated PROP_READY.

    Returns frozenset() if the receipt is missing or malformed (so the
    allocator never crashes — it just degrades to no-PROP_READY routing
    instead of mis-allocating real capital).
    """
    if not leaderboard_path.exists():
        return frozenset()
    try:
        data = json.loads(leaderboard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    bots = data.get("prop_ready_bots") or []
    if not isinstance(bots, list):
        return frozenset()
    return frozenset(str(b) for b in bots if b)


def get_bot_tier(
    bot_id: str,
    prop_ready: frozenset[str] | None = None,
) -> str:
    """Return the canonical tier for a bot.

    Order of preference: PROP_READY > DIAMOND > CANDIDATE.

    PROP_READY membership is read from the leaderboard receipt
    (load_prop_ready_bots) — caller can pre-fetch to avoid re-reading
    the receipt across many lookups.
    """
    if prop_ready is None:
        prop_ready = load_prop_ready_bots()
    if bot_id in prop_ready:
        return TIER_PROP_READY
    if bot_id in DIAMOND_BOTS:
        return TIER_DIAMOND
    return TIER_CANDIDATE


#: Wave-22 supervisor-facing prop-guard state.  These three constants
#: are exposed so the supervisor's entry path can do a single lookup
#: before placing any PROP_READY order:
#:
#:    from eta_engine.feeds.capital_allocator import (
#:        get_prop_guard_signal, PROP_HALT_FLAG_PATH, PROP_WATCH_FLAG_PATH,
#:    )
#:    signal = get_prop_guard_signal()
#:    if signal == "HALT": skip entry
#:    elif signal == "WATCH": qty = qty // 2
PROP_HALT_FLAG_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\prop_halt_active.flag",
)
PROP_WATCH_FLAG_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\prop_watch_active.flag",
)


def get_prop_guard_signal() -> str:
    """Return the current prop-fund drawdown guard signal.

    Single source of truth for the supervisor's entry decision.
    Reads the flag files written by diamond_prop_drawdown_guard each
    15-min cycle:

      HALT  - either flag file says HALT; supervisor MUST skip entries
      WATCH - WATCH flag present; supervisor should halve position size
      OK    - no flag files present; supervisor proceeds normally

    Cheap: just two `Path.exists()` calls per invocation. Safe to call
    on every entry tick.
    """
    if PROP_HALT_FLAG_PATH.exists():
        return "HALT"
    if PROP_WATCH_FLAG_PATH.exists():
        return "WATCH"
    return "OK"


def should_block_prop_entry(bot_id: str) -> bool:
    """Convenience helper: return True if the supervisor must SKIP the
    entry for a prop-ready bot right now.

    The supervisor's entry-decision path calls this with the bot_id;
    True means hard-skip (no fallback, no retry).
    """
    prop_ready = load_prop_ready_bots()
    if bot_id not in prop_ready:
        return False  # not a prop-ready bot, prop guard doesn't apply
    return get_prop_guard_signal() == "HALT"


def prop_entry_size_multiplier(bot_id: str) -> float:
    """Return the multiplier the supervisor should apply to qty for a
    prop-ready bot's entry.

      1.0   normal sizing (OK signal)
      0.5   halved (WATCH signal — de-risk while approaching limits)
      0.0   block (HALT — should_block_prop_entry returns True; this is
            here for callers that don't want a separate skip check)
    """
    prop_ready = load_prop_ready_bots()
    if bot_id not in prop_ready:
        return 1.0  # not a prop-ready bot, no adjustment
    signal = get_prop_guard_signal()
    if signal == "HALT":
        return 0.0
    if signal == "WATCH":
        return 0.5
    return 1.0


# ────────────────────────────────────────────────────────────────────
# Wave-25 (2026-05-13) — pre-trade risk gate + lifecycle state
#
# These helpers let the supervisor make a finer-grained routing
# decision than the binary HALT/WATCH/OK guard:
#
#   1. evaluate_pre_trade_risk(): given a prospective stop-out loss,
#      decides whether to allow_live, route_to_paper, or reject.
#      Reads buffers from diamond_prop_drawdown_guard_latest.json so
#      we never hit the live broker if the trade would push the
#      account into the consistency / DD trigger zone.
#
#   2. get_bot_lifecycle() / set_bot_lifecycle(): per-bot state
#      machine that controls whether live execution is permitted
#      at all. States:
#        - EVAL_LIVE     -- prop-firm eval candidate after calendar/date gates
#        - EVAL_PAPER    -- paper-traded only (kaizen-only learning)
#        - FUNDED_LIVE   -- post-eval, on the funded account
#        - RETIRED       -- no entries, audit-only
#
#   3. resolve_execution_target(): composite of all gates above.
#      The supervisor calls this and gets back ``"live"``,
#      ``"paper"``, or ``"reject"`` plus a reason string.
# ────────────────────────────────────────────────────────────────────

PROP_DRAWDOWN_GUARD_RECEIPT = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\diamond_prop_drawdown_guard_latest.json",
)
BOT_LIFECYCLE_STATE_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\bot_lifecycle.json",
)

# Lifecycle states.
LIFECYCLE_EVAL_LIVE = "EVAL_LIVE"
LIFECYCLE_EVAL_PAPER = "EVAL_PAPER"
LIFECYCLE_FUNDED_LIVE = "FUNDED_LIVE"
LIFECYCLE_RETIRED = "RETIRED"

# Operator directive (2026-05-14): run broker-backed paper-live only until
# 2026-07-08. EVAL_LIVE/FUNDED_LIVE states may be staged for dry-run visibility,
# but runtime routing must fall back to paper until this date floor is reached.
LIVE_CAPITAL_NOT_BEFORE = date(2026, 7, 8)
LIVE_CAPITAL_NOT_BEFORE_ENV = "ETA_LIVE_CAPITAL_NOT_BEFORE"

# When the prospective loss would consume more than this fraction of
# today's daily-DD buffer, the trade is routed to paper instead of
# live. 0.5 = "if a single trade would burn through half of today's
# remaining buffer, don't risk it."
SOFT_DAILY_DD_FRACTION = 0.5


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _parse_iso_date(raw: object) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def live_capital_not_before_date() -> date:
    """Return the effective live-capital date floor.

    Operators may push the floor later with ETA_LIVE_CAPITAL_NOT_BEFORE, but an
    earlier env value is ignored so local config cannot accidentally override
    the July 8 paper-only mandate.
    """
    env_date = _parse_iso_date(os.environ.get(LIVE_CAPITAL_NOT_BEFORE_ENV))
    if env_date and env_date > LIVE_CAPITAL_NOT_BEFORE:
        return env_date
    return LIVE_CAPITAL_NOT_BEFORE


def live_capital_calendar_gate(today: date | None = None) -> tuple[bool, str]:
    """Return whether live-capital routing is allowed by calendar policy."""
    observed = today or _utc_today()
    not_before = live_capital_not_before_date()
    if observed < not_before:
        days = (not_before - observed).days
        return (
            False,
            f"live_capital_calendar_hold_until_{not_before.isoformat()}: "
            f"paper_live only for {days} more day(s)",
        )
    return (True, f"live_capital_calendar_ok_after_{not_before.isoformat()}")


def build_live_capital_calendar_status(today: date | None = None) -> dict[str, Any]:
    """Machine-readable calendar policy receipt for dashboards and launch checks."""
    observed = today or _utc_today()
    not_before = live_capital_not_before_date()
    allowed, reason = live_capital_calendar_gate(today=observed)
    return {
        "today": observed.isoformat(),
        "not_before": not_before.isoformat(),
        "live_capital_allowed_by_date": allowed,
        "days_until_live_capital": max((not_before - observed).days, 0),
        "reason": reason,
        "paper_live_required": not allowed,
    }


def _read_drawdown_guard_state() -> dict[str, Any]:
    """Read the most recent prop-drawdown-guard receipt.

    Returns an empty dict if the receipt is missing or malformed.
    """
    try:
        if not PROP_DRAWDOWN_GUARD_RECEIPT.exists():
            return {}
        return json.loads(PROP_DRAWDOWN_GUARD_RECEIPT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_bot_lifecycle(bot_id: str) -> str:
    """Return the lifecycle state for a bot.

    Defaults:
      - PROP_READY bots without an explicit entry default to EVAL_PAPER
        (conservative: must be opted into EVAL_LIVE explicitly).
      - All other bots default to EVAL_PAPER as well — there is no live
        execution for non-prop bots in the current architecture.
    """
    try:
        if not BOT_LIFECYCLE_STATE_PATH.exists():
            return LIFECYCLE_EVAL_PAPER
        state = json.loads(BOT_LIFECYCLE_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return LIFECYCLE_EVAL_PAPER
        bots = state.get("bots") or state  # accept both shapes
        if not isinstance(bots, dict):
            return LIFECYCLE_EVAL_PAPER
        value = str(bots.get(bot_id, "")).strip().upper()
        if value in {
            LIFECYCLE_EVAL_LIVE,
            LIFECYCLE_EVAL_PAPER,
            LIFECYCLE_FUNDED_LIVE,
            LIFECYCLE_RETIRED,
        }:
            return value
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return LIFECYCLE_EVAL_PAPER


VALID_LIFECYCLE_STATES = frozenset(
    {
        LIFECYCLE_EVAL_LIVE,
        LIFECYCLE_EVAL_PAPER,
        LIFECYCLE_FUNDED_LIVE,
        LIFECYCLE_RETIRED,
    },
)


def set_bot_lifecycle(bot_id: str, state: str) -> bool:
    """Persist a bot's lifecycle state. Operator-facing helper.

    Returns True if the file was modified, False if the requested state
    already matched (idempotent — no write performed).

    Raises ValueError if state is not a recognized constant.

    Hardening (wave-25e):
      * Atomic write via temp file + replace so a partial-write crash
        does not corrupt the lifecycle JSON.
      * Idempotent: skips the write when the on-disk state is already
        the requested value.
      * Validates the existing file's structure on read; rebuilds from
        scratch if corrupt rather than silently merging into garbage.
    """
    if state not in VALID_LIFECYCLE_STATES:
        msg = f"unknown lifecycle state: {state!r}"
        raise ValueError(msg)

    BOT_LIFECYCLE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    current: dict[str, Any] = {}
    if BOT_LIFECYCLE_STATE_PATH.exists():
        try:
            loaded = json.loads(BOT_LIFECYCLE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            current = {}

    bots = current.get("bots")
    if not isinstance(bots, dict):
        bots = {}
        current["bots"] = bots

    # Idempotency: no-op if already in the requested state.
    if bots.get(bot_id) == state:
        return False

    bots[bot_id] = state
    payload = json.dumps(current, indent=2, sort_keys=True) + "\n"
    # Atomic write: temp file + os.replace (rename is atomic on POSIX
    # and NTFS for same-filesystem moves).
    tmp = BOT_LIFECYCLE_STATE_PATH.with_suffix(BOT_LIFECYCLE_STATE_PATH.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(BOT_LIFECYCLE_STATE_PATH)
    return True


def evaluate_pre_trade_risk(
    bot_id: str,
    *,
    prospective_loss_usd: float,
    soft_dd_fraction: float = SOFT_DAILY_DD_FRACTION,
) -> tuple[str, str]:
    """Pre-trade risk gate. Returns (verdict, reason).

    Verdicts:
      - "allow_live": safe to submit to live broker.
      - "route_to_paper": would breach the soft DD threshold; skip live.
      - "reject": would breach hard daily-DD or static DD limits; refuse
        entirely (don't even paper, the signal is too risky to honor).

    ``prospective_loss_usd`` is the dollar loss if the trade hits its
    stop. Pass a positive number (e.g. 250.0 for "if this stops out we
    lose $250").
    """
    if prospective_loss_usd <= 0:
        # Defensive: zero-or-negative prospective loss makes no sense
        # for a stop-out scenario; allow but log on the caller side.
        return ("allow_live", "non_positive_prospective_loss")

    state = _read_drawdown_guard_state()
    if not state:
        # No guard receipt → fail open (the prop_guard layer will have
        # blocked us already if anything was actually wrong).
        return ("allow_live", "no_guard_state")

    daily = state.get("daily_dd_check") or {}
    static = state.get("static_dd_check") or {}
    daily_buffer = float(daily.get("buffer_usd") or 0.0)
    static_buffer = float(static.get("buffer_usd") or 0.0)
    daily_limit = float(daily.get("limit_usd") or 0.0)

    if static_buffer > 0 and prospective_loss_usd >= static_buffer:
        return (
            "reject",
            f"would_breach_static_dd: loss=${prospective_loss_usd:.2f} >= buffer=${static_buffer:.2f}",
        )
    if daily_buffer > 0 and prospective_loss_usd >= daily_buffer:
        return (
            "reject",
            f"would_breach_daily_dd: loss=${prospective_loss_usd:.2f} >= buffer=${daily_buffer:.2f}",
        )
    soft_threshold = daily_limit * soft_dd_fraction if daily_limit > 0 else 0.0
    if soft_threshold > 0 and prospective_loss_usd >= soft_threshold:
        reason = (
            f"would_breach_soft_dd: loss=${prospective_loss_usd:.2f} "
            f">= soft=${soft_threshold:.2f} ({soft_dd_fraction:.0%} of ${daily_limit:.0f})"
        )
        return (
            "route_to_paper",
            reason,
        )
    return ("allow_live", "ok")


def resolve_execution_target(
    bot_id: str,
    *,
    prospective_loss_usd: float,
) -> tuple[str, str]:
    """Composite gate: lifecycle + prop guard + pre-trade risk.

    Returns (target, reason). ``target`` is one of:
      - "live"      -- submit to the live broker
      - "paper"     -- route to the paper-trading sim or broker-backed paper lane
      - "reject"    -- refuse the signal entirely

    Order of precedence:
      1. Lifecycle RETIRED → reject ("retired bot")
      2. Lifecycle EVAL_PAPER → paper ("eval_paper lifecycle")
      3. Prop guard HALT for prop_ready → reject ("prop_guard_halt")
      4. Pre-trade risk reject → reject (passes through reason)
      5. Pre-trade risk route_to_paper → paper (passes through reason)
      6. Default → live ("ok")
    """
    lifecycle = get_bot_lifecycle(bot_id)
    if lifecycle == LIFECYCLE_RETIRED:
        return ("reject", "lifecycle_retired")
    if lifecycle == LIFECYCLE_EVAL_PAPER:
        return ("paper", "lifecycle_eval_paper")

    calendar_ok, calendar_reason = live_capital_calendar_gate()
    if not calendar_ok:
        return ("paper", calendar_reason)

    if should_block_prop_entry(bot_id):
        return ("reject", "prop_guard_halt")

    risk_verdict, risk_reason = evaluate_pre_trade_risk(
        bot_id,
        prospective_loss_usd=prospective_loss_usd,
    )
    if risk_verdict == "reject":
        return ("reject", risk_reason)
    if risk_verdict == "route_to_paper":
        return ("paper", risk_reason)
    return ("live", "ok")


def is_ibkr_futures_eligible(bot_id: str) -> bool:
    """Return True if this bot's strategy can route through IBKR futures.

    Wave-16 operator mandate (2026-05-12): the prop-fund routing layer is
    IBKR-futures-only.  Alpaca spot is cellared (POOL_SPLIT["spot"]=0.0);
    Tradovate dormant.  Crypto exposure comes from CME micro crypto
    futures (MET/MBT) routed through IBKR — NOT from BTC/ETH/SOL spot
    via Alpaca.

    The PROP_READY badge in diamond_leaderboard requires this gate so
    a high-scoring spot bot doesn't earn real-capital routing through
    a broker the operator has cellared.

    Returns True when classify_pool(bot_id) in ("futures", "leveraged").
    Spot bots return False even if their R-edge is excellent.
    """
    return classify_pool(bot_id) in ("futures", "leveraged")


def compute_allocations(ledger_path: Path, total_capital: float = 100_000.0) -> PortfolioAllocation:
    """Compute per-bot capital allocations from paper soak ledger data."""
    if not ledger_path.exists():
        return PortfolioAllocation(total_capital=total_capital)

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    sessions = ledger.get("bot_sessions", {})

    allocation = PortfolioAllocation(total_capital=total_capital)

    # Compute per-bot stats
    bot_stats = {}
    for bot_id, bot_sessions in sessions.items():
        if len(bot_sessions) < MIN_SESSIONS:
            continue
        pnls = [s.get("pnl", 0) for s in bot_sessions]
        total_pnl = sum(pnls)
        winners = sum(1 for p in pnls if p > 0)
        win_rate = winners / len(pnls) if pnls else 0
        pool = classify_pool(bot_id)
        bot_stats[bot_id] = {
            "symbol": bot_id,
            "pool": pool,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "sessions": len(bot_sessions),
        }

    # Group by pool and compute weights
    for pool_name in ("spot", "futures", "leveraged"):
        pool_bots = {k: v for k, v in bot_stats.items() if v["pool"] == pool_name}
        profitable = {k: v for k, v in pool_bots.items() if v["total_pnl"] > 0}
        total_profitable_pnl = sum(v["total_pnl"] for v in profitable.values())
        pool_capital = total_capital * POOL_SPLIT[pool_name]

        pool_data = {
            "capital": pool_capital,
            "bot_count": len(pool_bots),
            "profitable_count": len(profitable),
            "total_profitable_pnl": total_profitable_pnl,
            "bots": {},
        }

        # Wave-18: pre-fetch PROP_READY set once per allocation pass
        prop_ready = load_prop_ready_bots()

        for bot_id, stats in pool_bots.items():
            is_diamond = bot_id in DIAMOND_BOTS
            is_prop_ready = bot_id in prop_ready
            tier = get_bot_tier(bot_id, prop_ready=prop_ready)

            # Wave-18: PROP_READY tier gets a FLOOR of PROP_READY_CAPITAL_PER_BOT
            # ON TOP of any performance-weighted allocation. This is how the
            # elite-3 earn real-capital routing once the operator's prop-fund
            # wiring reads the bot_allocations.tier field.
            if stats["total_pnl"] > 0 and total_profitable_pnl > 0:
                weight = stats["total_pnl"] / total_profitable_pnl
                capital = pool_capital * weight
                if is_prop_ready:
                    capital = max(capital, PROP_READY_CAPITAL_PER_BOT)
                status = "active"
            elif is_prop_ready:
                # PROP_READY ALWAYS-FLOOR (even on -PnL paper window):
                # the leaderboard's eligibility gate (n>=100, avg_r>=+0.20,
                # watchdog non-CRITICAL, sizing non-BREACHED) is more
                # discriminating than total_pnl > 0 — trust it.
                weight = max(0.05, PROP_READY_CAPITAL_PER_BOT / pool_capital)
                capital = PROP_READY_CAPITAL_PER_BOT
                status = "active"
            elif is_diamond:
                # DIAMOND PROTECTION: always active with minimum capital
                weight = 0.05  # minimum weight
                capital = max(DIAMOND_MIN_CAPITAL, pool_capital * 0.05)
                status = "active"
            else:
                weight = 0.0
                capital = 0.0
                status = "paused"

            ba = BotAllocation(
                bot_id=bot_id,
                symbol=stats["symbol"],
                pool=pool_name,
                weight=weight,
                capital=capital,
                pnl_total=stats["total_pnl"],
                win_rate=stats["win_rate"],
                sessions=stats["sessions"],
                status=status,
            )
            # Wave-18: attach tier metadata so the prop-fund routing
            # layer can pick PROP_READY allocations vs DIAMOND vs CANDIDATE.
            ba.tier = tier  # type: ignore[attr-defined]
            allocation.bots[bot_id] = ba
            pool_data["bots"][bot_id] = {
                "weight": weight,
                "capital": capital,
                "pnl_total": stats["total_pnl"],
                "status": status,
            }

        setattr(allocation, f"{pool_name}_pool", pool_data)

    return allocation


def save_allocation(allocation: PortfolioAllocation, path: Path = ALLOCATION_PATH) -> None:
    """Persist allocation to disk for the supervisor to read."""
    data = {
        "total_capital": allocation.total_capital,
        "spot_pool": allocation.spot_pool,
        "futures_pool": allocation.futures_pool,
        "leveraged_pool": allocation.leveraged_pool,
        "bot_allocations": {
            bid: {
                "pool": ba.pool,
                "weight": ba.weight,
                "capital": ba.capital,
                "status": ba.status,
                "pnl_total": ba.pnl_total,
            }
            for bid, ba in allocation.bots.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_allocation(path: Path = ALLOCATION_PATH) -> PortfolioAllocation | None:
    """Load persisted allocation."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    alloc = PortfolioAllocation(total_capital=data["total_capital"])
    alloc.spot_pool = data.get("spot_pool", {})
    alloc.futures_pool = data.get("futures_pool", {})
    alloc.leveraged_pool = data.get("leveraged_pool", {})
    for bid, ba_data in data.get("bot_allocations", {}).items():
        alloc.bots[bid] = BotAllocation(
            bot_id=bid,
            symbol=ba_data.get("symbol", "?"),
            pool=ba_data["pool"],
            weight=ba_data["weight"],
            capital=ba_data["capital"],
            pnl_total=ba_data["pnl_total"],
            win_rate=0.0,
            sessions=0,
            status=ba_data["status"],
        )
    return alloc


def get_bot_capital(bot_id: str, path: Path = ALLOCATION_PATH) -> float:
    """Get allocated capital for a bot. Returns 0 if paused/no-data."""
    alloc = load_allocation(path)
    if alloc and bot_id in alloc.bots:
        return alloc.bots[bot_id].capital
    return 0.0


def _read_registry_map() -> dict[str, dict[str, str]]:
    """Parse per_bot_registry for bot->symbol mapping."""
    import re

    reg_path = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\strategies\per_bot_registry.py")
    reg_map = {}
    if reg_path.exists():
        content = reg_path.read_text(encoding="utf-8")
        for m in re.finditer(
            r'"(\w+)"\s*:\s*BotAssignment\(\s*symbol\s*=\s*"(\w+)"',
            content,
        ):
            reg_map[m.group(1)] = {"symbol": m.group(2)}
    return reg_map


if __name__ == "__main__":
    # Compute and save allocations from current soak data
    import sys

    ledger = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\paper_soak_ledger.json")
    total = float(sys.argv[1]) if len(sys.argv) > 1 else 100_000.0
    alloc = compute_allocations(ledger, total)
    save_allocation(alloc)
    print(f"Allocation saved to {ALLOCATION_PATH}")
    print(f"Total capital: ${total:,.0f}")
    for pool_name in ("spot", "futures", "leveraged"):
        pool = getattr(alloc, f"{pool_name}_pool")
        print(f"\n{pool_name.upper()} ({POOL_SPLIT[pool_name] * 100:.0f}% = ${pool['capital']:,.0f}):")
        for bid, bd in sorted(pool.get("bots", {}).items(), key=lambda x: -x[1]["pnl_total"]):
            print(
                f"  {bid}: {bd['status']:6s}  weight={bd['weight']:.1%}  "
                f"capital=${bd['capital']:,.0f}  PnL=${bd['pnl_total']:+,.0f}"
            )
