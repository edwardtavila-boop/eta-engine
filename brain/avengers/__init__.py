"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers
=================================
The Avengers fleet -- three development personas (Batman, Alfred, Robin)
plus the Fleet coordinator, all sitting beside JARVIS.

Why this package exists
-----------------------
JARVIS is the deterministic admin -- he runs the policy engine on the
risk-gate hot path with zero LLM latency. The Avengers are the
*development* side of the fleet, each locked to a model tier so cost is
predictable:

  * BATMAN  -> Opus 4.7  (architectural / adversarial work)
  * ALFRED  -> Sonnet 4.6 (routine dev work, the default)
  * ROBIN   -> Haiku 4.5 (mechanical grunt)

Any LLM-shaped work JARVIS might be tempted to run (log parsing, alert
drafting, post-hoc review) is offloaded to this fleet instead, keeping
JARVIS's hot path clean and the overall burn rate ~5x lower than if
everything defaulted to Opus.

Public API
----------
  * ``Fleet``           -- coordinator (one ``dispatch`` entrypoint)
  * ``HardenedFleet``   -- Fleet + full guard stack (breaker, deadman,
                            precedent, calibration, push alerts)
  * ``Batman``/``Alfred``/``Robin`` -- concrete Persona subclasses
  * ``Persona``         -- abstract base for future personas
  * ``PersonaId``       -- enum of persona identities
  * ``TaskEnvelope``    -- pydantic request envelope
  * ``TaskResult``      -- pydantic response envelope
  * ``Executor``        -- Protocol for LLM runners (inject in tests)
  * ``DryRunExecutor``  -- deterministic default executor
  * ``make_envelope``   -- short-form factory for callers
  * ``AVENGERS_JOURNAL``-- default JSONL audit log path

Hardening modules (opt-in; compose via ``HardenedFleet``)
---------------------------------------------------------
  * ``CircuitBreaker``   -- trip on failure / denial / cost bursts
  * ``DeadmanSwitch``    -- flip to conservative mode if operator goes AWOL
  * ``PrecedentCache``   -- short-circuit repeated envelopes via journal RAG
  * ``PreflightCache``   -- LRU+TTL cache of JARVIS verdicts
  * ``CalibrationLoop``  -- per-(persona, category) success scoreboard
  * ``CostForecast``     -- monthly burn projection from journal
  * ``RegimeGate``       -- regime-aware 1-in-N gating for sparse tasks
  * ``PromotionGate``    -- shadow -> paper -> 1-lot -> live pipeline
  * ``PushBus``          -- Pushover / Telegram / local alert fan-out
  * ``Watchdog``         -- sibling-daemon healer
"""

from eta_engine.brain.avengers.adaptive_cron import (
    GateDecision,
    RegimeGate,
    RegimeTag,
)
from eta_engine.brain.avengers.alfred import Alfred
from eta_engine.brain.avengers.base import (
    AVENGERS_JOURNAL,
    COST_RATIO,
    PERSONA_BUCKET,
    PERSONA_TIER,
    DryRunExecutor,
    Executor,
    Persona,
    PersonaId,
    TaskBucket,
    TaskCategory,
    TaskEnvelope,
    TaskResult,
    append_journal,
    bucket_for,
    describe_persona,
    make_envelope,
    select_model,
    tier_for,
)
from eta_engine.brain.avengers.batman import Batman
from eta_engine.brain.avengers.calibration_loop import (
    CALIBRATION_JOURNAL,
    CalibrationLoop,
    PersonaScore,
)
from eta_engine.brain.avengers.circuit_breaker import (
    BreakerState,
    BreakerStatus,
    BreakerTripped,
    CircuitBreaker,
)
from eta_engine.brain.avengers.cost_forecast import (
    BurnReport,
    BurnWindow,
    CostForecast,
)
from eta_engine.brain.avengers.daemon import (
    VALID_PERSONAS,
    AvengerDaemon,
    DaemonHeartbeat,
    envelope_for_task,
    is_due,
    run_daemon_cli,
)
from eta_engine.brain.avengers.deadman import (
    DEADMAN_JOURNAL,
    DEADMAN_SENTINEL,
    DeadmanDecision,
    DeadmanState,
    DeadmanStatus,
    DeadmanSwitch,
)
from eta_engine.brain.avengers.dispatch import (
    TASK_CADENCE,
    TASK_OWNERS,
    AvengersDispatch,
    BackgroundTask,
    DispatchResult,
    DispatchRoute,
)
from eta_engine.brain.avengers.drift_detector import (
    DRIFT_JOURNAL,
    DriftDetector,
    DriftReport,
    DriftVerdict,
    read_drift_journal,
)
from eta_engine.brain.avengers.fleet import Fleet, FleetMetrics
from eta_engine.brain.avengers.hardened_fleet import HardenedFleet
from eta_engine.brain.avengers.precedent_cache import (
    PrecedentCache,
    PrecedentHit,
    SkipVerdict,
)
from eta_engine.brain.avengers.preflight_cache import (
    CacheEntry,
    CacheKey,
    PreflightCache,
)
from eta_engine.brain.avengers.promotion import (
    DEFAULT_MIN_LIVE_SLIPPAGE_BPS,
    DEFAULT_TIGHT_MARGIN_PCT,
    DEFAULT_TRADES_SAFETY_FACTOR,
    PROMOTION_JOURNAL,
    PROMOTION_STATE,
    RED_TEAM_GATED_TRANSITIONS,
    PromotionAction,
    PromotionDecision,
    PromotionGate,
    PromotionSpec,
    PromotionStage,
    RedTeamGate,
    RedTeamVerdict,
    StageMetrics,
    StageThresholds,
    default_red_team_gate,
)
from eta_engine.brain.avengers.push import (
    ALERTS_JOURNAL,
    Alert,
    AlertLevel,
    LocalFileNotifier,
    Notifier,
    PushBus,
    PushoverNotifier,
    TelegramNotifier,
    default_bus,
    push,
)
from eta_engine.brain.avengers.robin import Robin
from eta_engine.brain.avengers.shared_breaker import (
    DEFAULT_BREAKER_PATH,
    SharedCircuitBreaker,
    read_shared_status,
    reset_shared,
)
from eta_engine.brain.avengers.watchdog import (
    FLEET_PERSONAS,
    DaemonHealth,
    HealthStatus,
    Watchdog,
    WatchdogRelauncher,
    WatchdogReport,
)

__all__ = [
    "ALERTS_JOURNAL",
    "AVENGERS_JOURNAL",
    "CALIBRATION_JOURNAL",
    "COST_RATIO",
    "DEADMAN_JOURNAL",
    "DEADMAN_SENTINEL",
    "DEFAULT_BREAKER_PATH",
    "DEFAULT_MIN_LIVE_SLIPPAGE_BPS",
    "DEFAULT_TIGHT_MARGIN_PCT",
    "DEFAULT_TRADES_SAFETY_FACTOR",
    "DRIFT_JOURNAL",
    "FLEET_PERSONAS",
    "PERSONA_BUCKET",
    "PERSONA_TIER",
    "PROMOTION_JOURNAL",
    "PROMOTION_STATE",
    "RED_TEAM_GATED_TRANSITIONS",
    "TASK_CADENCE",
    "TASK_OWNERS",
    "VALID_PERSONAS",
    "Alert",
    "AlertLevel",
    "Alfred",
    "AvengerDaemon",
    "AvengersDispatch",
    "BackgroundTask",
    "Batman",
    "BreakerState",
    "BreakerStatus",
    "BreakerTripped",
    "BurnReport",
    "BurnWindow",
    "CacheEntry",
    "CacheKey",
    "CalibrationLoop",
    "CircuitBreaker",
    "CostForecast",
    "DaemonHealth",
    "DaemonHeartbeat",
    "DeadmanDecision",
    "DeadmanState",
    "DeadmanStatus",
    "DeadmanSwitch",
    "DispatchResult",
    "DispatchRoute",
    "DriftDetector",
    "DriftReport",
    "DriftVerdict",
    "DryRunExecutor",
    "Executor",
    "Fleet",
    "FleetMetrics",
    "GateDecision",
    "HardenedFleet",
    "HealthStatus",
    "LocalFileNotifier",
    "Notifier",
    "Persona",
    "PersonaId",
    "PersonaScore",
    "PrecedentCache",
    "PrecedentHit",
    "PreflightCache",
    "PromotionAction",
    "PromotionDecision",
    "PromotionGate",
    "PromotionSpec",
    "PromotionStage",
    "PushBus",
    "PushoverNotifier",
    "RedTeamGate",
    "RedTeamVerdict",
    "RegimeGate",
    "RegimeTag",
    "Robin",
    "SharedCircuitBreaker",
    "SkipVerdict",
    "StageMetrics",
    "StageThresholds",
    "TaskBucket",
    "TaskCategory",
    "TaskEnvelope",
    "TaskResult",
    "TelegramNotifier",
    "Watchdog",
    "WatchdogRelauncher",
    "WatchdogReport",
    "append_journal",
    "bucket_for",
    "default_bus",
    "default_red_team_gate",
    "describe_persona",
    "envelope_for_task",
    "is_due",
    "make_envelope",
    "push",
    "read_drift_journal",
    "read_shared_status",
    "reset_shared",
    "run_daemon_cli",
    "select_model",
    "tier_for",
]
