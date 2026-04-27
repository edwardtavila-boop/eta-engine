"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.base
======================================
Shared contract for the Avengers fleet (Batman, Alfred, Robin) and the
JARVIS coordinator.

Why this exists
---------------
Edward's directive (2026-04-23): "fully link up all 4 AI systems to compliment
and work together and to operate as one unit ... admin jarvis who is
responsible for upkeeping of the vps and running the operation then you have
batman and robin and alfred who control the development of the operation
supercharge them to be maximum and efficient and calculative pooling
resources to help jarvis spare no limitations from alfred robin and claude
and reduce the strain on jarvis."

JARVIS is the deterministic admin (policy engine, hot path, zero LLM). The
three Avengers are *development* personas, each locked to a single model
tier so cost is predictable:

* BATMAN  -> Opus 4.7  (architectural / adversarial / tactical)
* ALFRED  -> Sonnet 4.6 (knowledge steward / default reasoner)
* ROBIN   -> Haiku 4.5 (mechanical grunt work)

This module defines the *contract*: the typed envelope a caller sends, the
typed result a persona returns, the abstract Persona base class, and the
append-only JSONL journal the fleet writes to. Persona subclasses live in
``brain.avengers.{batman,alfred,robin}`` and stay narrowly focused on their
prompt templates + tier enforcement.

Design principles (mirrors ``brain.jarvis_admin``)
--------------------------------------------------
1. Pydantic-typed envelopes. No untyped dicts on the wire.
2. StrEnum for the cross-persona taxonomies so JSON round-trip is lossless.
3. Dispatch is a pure method + one file-append side effect. Persona-level
   LLM invocation is injected (``Executor`` callable) so tests can stub it.
4. Every dispatch is gated through JARVIS via ``ActionType.LLM_INVOCATION``.
   If JARVIS DENIES / DEFERS the routing, the Persona short-circuits.
5. Tier-locked: a Persona refuses an envelope whose ``TaskCategory`` resolves
   to a tier other than its own. The Fleet coordinator is responsible for
   routing -- personas don't cross lanes.
6. No network, no blocking I/O in the contract layer. Only the executor
   talks to the outside world.

Public API
----------
  * ``PersonaId``       -- enum of the personas (JARVIS + 3 Avengers)
  * ``TaskEnvelope``    -- pydantic: what a caller sends to a persona
  * ``TaskResult``      -- pydantic: what a persona returns
  * ``Executor``        -- Protocol for injectable LLM runners
  * ``DryRunExecutor``  -- deterministic default (no network)
  * ``Persona``         -- abstract base; subclasses = Batman/Alfred/Robin
  * ``AVENGERS_JOURNAL``-- default JSONL path (``~/.jarvis/avengers.jsonl``)
  * ``append_journal``  -- small helper used by every Persona
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime  # noqa: TC003 -- pydantic needs runtime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_admin import (
    ActionType,
    SubsystemId,
    Verdict,
    make_action_request,
)
from eta_engine.brain.model_policy import (
    COST_RATIO,
    ModelTier,
    TaskBucket,
    TaskCategory,
    bucket_for,
    select_model,
    tier_for,
)

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_admin import ActionResponse, JarvisAdmin


# ---------------------------------------------------------------------------
# Persona identity
# ---------------------------------------------------------------------------


class PersonaId(StrEnum):
    """Every persona that can sit behind the fleet coordinator.

    JARVIS is listed for symmetry but never actually runs an LLM -- it is
    the deterministic admin. Including it in the enum lets the JSONL
    journal attribute setup / shutdown events to the right actor.
    """

    JARVIS = "persona.jarvis"
    BATMAN = "persona.batman"
    ALFRED = "persona.alfred"
    ROBIN = "persona.robin"


# Persona -> locked tier. Changing these is an architectural decision --
# keep it in sync with ``brain.model_policy._CATEGORY_TO_TIER`` buckets.
PERSONA_TIER: dict[PersonaId, ModelTier | None] = {
    PersonaId.JARVIS: None,  # deterministic, no LLM
    PersonaId.BATMAN: ModelTier.OPUS,  # architectural
    PersonaId.ALFRED: ModelTier.SONNET,  # routine
    PersonaId.ROBIN: ModelTier.HAIKU,  # grunt
}


# Quick lookup: given a persona, which TaskBucket does it cover?
PERSONA_BUCKET: dict[PersonaId, TaskBucket | None] = {
    PersonaId.JARVIS: None,
    PersonaId.BATMAN: TaskBucket.ARCHITECTURAL,
    PersonaId.ALFRED: TaskBucket.ROUTINE,
    PersonaId.ROBIN: TaskBucket.GRUNT,
}


# ---------------------------------------------------------------------------
# Task envelope + result
# ---------------------------------------------------------------------------


def _new_task_id() -> str:
    return uuid.uuid4().hex[:12]


class TaskEnvelope(BaseModel):
    """What a caller (JARVIS, a bot, the Fleet, etc.) sends to a persona.

    The envelope is intentionally small -- personas are stateless and each
    dispatch must carry everything needed to reproduce the work.
    """

    model_config = ConfigDict(frozen=False)  # allow default_factory fields

    task_id: str = Field(default_factory=_new_task_id, min_length=1)
    category: TaskCategory
    goal: str = Field(
        min_length=1,
        description="Single-sentence description of what the persona must do.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured supporting data (file paths, error messages, "
        "metric snapshots, etc.). Kept as dict so JSON round-trip "
        "stays lossless.",
    )
    caller: SubsystemId = Field(
        default=SubsystemId.OPERATOR,
        description="Which subsystem originated the request. Used in the JSONL journal for cross-persona audit.",
    )
    rationale: str = Field(
        default="",
        description="Why the caller is asking for this work -- audit-only.",
    )
    # Optional override. If None, the Fleet routes by category->tier.
    requested_tier: ModelTier | None = None
    # Optional hard deadline. Informational for now -- personas respect
    # it by setting soft timeouts in the executor.
    deadline: datetime | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskResult(BaseModel):
    """What a persona returns. Denormalized so the JSONL entry is complete
    without needing to re-resolve anything.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str = Field(min_length=1)
    persona_id: PersonaId
    tier_used: ModelTier | None = Field(
        default=None,
        description="Tier actually consumed. None for JARVIS deterministic paths or when the executor short-circuited.",
    )
    success: bool
    artifact: str = Field(
        default="",
        description="Primary output (markdown, code, analysis). Empty on rejection / deferral.",
    )
    reason_code: str = Field(
        min_length=1,
        description="Stable machine-readable code, e.g. 'tier_mismatch', 'jarvis_denied', 'ok'.",
    )
    reason: str = Field(min_length=1)
    cost_multiplier: float = Field(ge=0.0, le=10.0)
    jarvis_verdict: Verdict | None = Field(
        default=None,
        description="Verdict JARVIS returned for the LLM_INVOCATION pre-flight, if the persona consulted JARVIS.",
    )
    ms_elapsed: float = Field(ge=0.0, default=0.0)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Executor protocol
# ---------------------------------------------------------------------------


class Executor(Protocol):
    """Minimal callable contract every Persona accepts.

    Real implementations hand the prompts to an LLM API; the default
    ``DryRunExecutor`` just echoes them back with structured metadata so
    tests run offline and the fleet is usable before Anthropic credentials
    are wired up.
    """

    def __call__(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        envelope: TaskEnvelope,
    ) -> str:
        """Return the model's text response.

        Raising is fine; ``Persona.dispatch`` catches and records the error
        in the JSONL journal instead of propagating.
        """


class DryRunExecutor:
    """Deterministic default executor -- no network, no LLM.

    Produces a structured markdown artifact so downstream consumers have
    something real to parse while the production Anthropic executor is
    wired up elsewhere.
    """

    def __call__(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        envelope: TaskEnvelope,
    ) -> str:
        header = (
            f"# DRY-RUN ({tier.value}) :: {envelope.category.value}\n\n"
            f"- task_id: `{envelope.task_id}`\n"
            f"- caller: `{envelope.caller.value}`\n"
            f"- ts: {envelope.ts.isoformat()}\n\n"
        )
        sys_block = f"## System prompt\n\n```\n{system_prompt.strip()}\n```\n\n"
        usr_block = f"## User prompt\n\n```\n{user_prompt.strip()}\n```\n\n"
        ctx_block = f"## Context\n\n```json\n{json.dumps(envelope.context, indent=2, default=str)}\n```\n"
        return header + sys_block + usr_block + ctx_block


# ---------------------------------------------------------------------------
# JSONL journal
# ---------------------------------------------------------------------------


AVENGERS_JOURNAL: Path = Path.home() / ".jarvis" / "avengers.jsonl"


def append_journal(
    path: Path,
    *,
    envelope: TaskEnvelope,
    result: TaskResult,
    persona_id: PersonaId,
    jarvis_response: ActionResponse | None = None,
) -> None:
    """Append one line per (envelope, result) pair.

    Best-effort: if the journal can't be opened we swallow the OSError so
    the fleet stays live. Audit gaps show up in the admin console.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "ts": result.ts.isoformat(),
        "persona": persona_id.value,
        "envelope": envelope.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }
    if jarvis_response is not None:
        record["jarvis_response"] = jarvis_response.model_dump(mode="json")
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        # Audit is best-effort -- never let journaling kill the fleet.
        return


# ---------------------------------------------------------------------------
# Persona abstract base
# ---------------------------------------------------------------------------


class Persona(ABC):
    """Abstract parent for the three Avengers.

    Subclasses override:
      * ``PERSONA_ID``  (class attribute) -- which PersonaId they are.
      * ``supported_categories`` (classmethod) -- which TaskCategory values
        they accept. Default = every category whose bucket matches the
        persona's locked tier.
      * ``_system_prompt`` -- persona-specific voice / role instructions.
      * ``_user_prompt``   -- how the envelope is serialized into user
        content for the LLM.

    The public ``dispatch`` method is concrete and handles:
      1. Tier guard (refuses work that routes to a different tier).
      2. JARVIS pre-flight via ActionType.LLM_INVOCATION.
      3. Executor invocation (with per-call timing).
      4. JSONL journaling.
    """

    PERSONA_ID: ClassVar[PersonaId]

    def __init__(
        self,
        *,
        executor: Executor | None = None,
        admin: JarvisAdmin | None = None,
        journal_path: Path | None = None,
    ) -> None:
        self._executor: Executor = executor or DryRunExecutor()
        self._admin = admin
        self._journal_path = journal_path or AVENGERS_JOURNAL

    # --- class-level introspection helpers ---------------------------------

    @property
    def persona_id(self) -> PersonaId:
        return self.PERSONA_ID

    @property
    def tier(self) -> ModelTier | None:
        return PERSONA_TIER[self.PERSONA_ID]

    @property
    def bucket(self) -> TaskBucket | None:
        return PERSONA_BUCKET[self.PERSONA_ID]

    @property
    def cost_multiplier(self) -> float:
        """Cost vs Sonnet baseline (1.0x)."""
        t = self.tier
        if t is None:
            return 0.0
        return COST_RATIO[t]

    @classmethod
    def supported_categories(cls) -> frozenset[TaskCategory]:
        """Default = every category whose policy tier matches ours.

        Subclasses can override to narrow / widen the lane, but the default
        is deliberately permissive: if ``model_policy`` routes the category
        to my tier, I'll take it.
        """
        my_tier = PERSONA_TIER[cls.PERSONA_ID]
        if my_tier is None:
            return frozenset()
        return frozenset(cat for cat in TaskCategory if tier_for(cat) == my_tier)

    # --- abstract prompt surface -------------------------------------------

    @abstractmethod
    def _system_prompt(self, envelope: TaskEnvelope) -> str:
        """Persona-specific system prompt. Subclasses MUST override."""

    def _user_prompt(self, envelope: TaskEnvelope) -> str:
        """Default: goal + context JSON. Subclasses may override for a
        more opinionated layout.
        """
        ctx_block = ""
        if envelope.context:
            ctx_block = f"\n\nContext:\n```json\n{json.dumps(envelope.context, indent=2, default=str)}\n```"
        rationale = f"\n\nWhy: {envelope.rationale}" if envelope.rationale else ""
        return f"Task: {envelope.goal}{rationale}{ctx_block}"

    # --- public dispatch ---------------------------------------------------

    def dispatch(self, envelope: TaskEnvelope) -> TaskResult:
        """Run one task through the persona.

        Never raises for expected failure paths (tier mismatch, JARVIS
        denial, executor exception). Returns a TaskResult with
        ``success=False`` and a stable ``reason_code`` instead.
        """
        started = datetime.now(UTC)

        # 1. Tier guard -- personas stay in their lane.
        policy_tier = tier_for(envelope.category)
        if self.tier is not None and policy_tier != self.tier:
            res = self._make_result(
                envelope=envelope,
                success=False,
                artifact="",
                reason_code="tier_mismatch",
                reason=(
                    f"{self.PERSONA_ID.value} is locked to "
                    f"{self.tier.value}; category "
                    f"{envelope.category.value} routes to "
                    f"{policy_tier.value}"
                ),
                jarvis_verdict=None,
                started_at=started,
            )
            append_journal(
                self._journal_path,
                envelope=envelope,
                result=res,
                persona_id=self.PERSONA_ID,
            )
            return res

        # 2. JARVIS pre-flight (cost-optimization check, stress-independent).
        jarvis_response: ActionResponse | None = None
        if self._admin is not None:
            req = make_action_request(
                subsystem=envelope.caller,
                action=ActionType.LLM_INVOCATION,
                rationale=(f"persona={self.PERSONA_ID.value} goal={envelope.goal[:80]}"),
                task_category=envelope.category.value,
            )
            jarvis_response = self._admin.request_approval(req)
            if jarvis_response.verdict in {Verdict.DENIED, Verdict.DEFERRED}:
                res = self._make_result(
                    envelope=envelope,
                    success=False,
                    artifact="",
                    reason_code=f"jarvis_{jarvis_response.verdict.value.lower()}",
                    reason=(f"JARVIS {jarvis_response.verdict.value}: {jarvis_response.reason}"),
                    jarvis_verdict=jarvis_response.verdict,
                    started_at=started,
                )
                append_journal(
                    self._journal_path,
                    envelope=envelope,
                    result=res,
                    persona_id=self.PERSONA_ID,
                    jarvis_response=jarvis_response,
                )
                return res

        # 3. Build prompts + invoke executor.
        sys_prompt = self._system_prompt(envelope)
        usr_prompt = self._user_prompt(envelope)
        try:
            # Tier is guaranteed non-None here: JARVIS persona never
            # inherits from Persona so the abstract tier is always
            # ModelTier for concrete subclasses.
            assert self.tier is not None, f"{self.PERSONA_ID.value} must have a locked tier"
            artifact = self._executor(
                tier=self.tier,
                system_prompt=sys_prompt,
                user_prompt=usr_prompt,
                envelope=envelope,
            )
        except Exception as exc:  # noqa: BLE001 -- executor failure is a
            # first-class, logged outcome.
            res = self._make_result(
                envelope=envelope,
                success=False,
                artifact="",
                reason_code="executor_error",
                reason=f"executor raised: {exc!r}",
                jarvis_verdict=(jarvis_response.verdict if jarvis_response else None),
                started_at=started,
            )
            append_journal(
                self._journal_path,
                envelope=envelope,
                result=res,
                persona_id=self.PERSONA_ID,
                jarvis_response=jarvis_response,
            )
            return res

        res = self._make_result(
            envelope=envelope,
            success=True,
            artifact=artifact,
            reason_code="ok",
            reason=f"{self.PERSONA_ID.value} completed {envelope.category.value}",
            jarvis_verdict=(jarvis_response.verdict if jarvis_response else None),
            started_at=started,
        )
        append_journal(
            self._journal_path,
            envelope=envelope,
            result=res,
            persona_id=self.PERSONA_ID,
            jarvis_response=jarvis_response,
        )
        return res

    # --- internal ----------------------------------------------------------

    def _make_result(
        self,
        *,
        envelope: TaskEnvelope,
        success: bool,
        artifact: str,
        reason_code: str,
        reason: str,
        jarvis_verdict: Verdict | None,
        started_at: datetime,
    ) -> TaskResult:
        elapsed_ms = max(
            0.0,
            (datetime.now(UTC) - started_at).total_seconds() * 1000.0,
        )
        return TaskResult(
            task_id=envelope.task_id,
            persona_id=self.PERSONA_ID,
            tier_used=self.tier,
            success=success,
            artifact=artifact,
            reason_code=reason_code,
            reason=reason,
            cost_multiplier=self.cost_multiplier,
            jarvis_verdict=jarvis_verdict,
            ms_elapsed=elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def make_envelope(
    *,
    category: TaskCategory,
    goal: str,
    caller: SubsystemId = SubsystemId.OPERATOR,
    rationale: str = "",
    **context: Any,  # noqa: ANN401 -- deliberately untyped by design
) -> TaskEnvelope:
    """Short-form factory for callers that don't want pydantic ceremony."""
    return TaskEnvelope(
        category=category,
        goal=goal,
        caller=caller,
        rationale=rationale,
        context=context,
    )


def describe_persona(persona_id: PersonaId) -> str:
    """One-line human-readable summary -- used by the VPS console."""
    tier = PERSONA_TIER[persona_id]
    bucket = PERSONA_BUCKET[persona_id]
    if tier is None or bucket is None:
        return f"{persona_id.value}: deterministic admin (no LLM, policy engine only)"
    cost = COST_RATIO[tier]
    return f"{persona_id.value}: tier={tier.value} bucket={bucket.value} cost={cost:g}x Sonnet"


# Exported names kept stable -- avengers/__init__.py re-exports these.
__all__ = [
    "AVENGERS_JOURNAL",
    "COST_RATIO",
    "DryRunExecutor",
    "Executor",
    "PERSONA_BUCKET",
    "PERSONA_TIER",
    "Persona",
    "PersonaId",
    "TaskBucket",
    "TaskCategory",
    "TaskEnvelope",
    "TaskResult",
    "append_journal",
    "bucket_for",
    "describe_persona",
    "make_envelope",
    "select_model",
    "tier_for",
]
