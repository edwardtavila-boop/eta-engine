"""
MultiModel Executor — bridges the Avengers Fleet to the Force Multiplier orchestrator.

Allows Fleet dispatch to route through the full multi-model pipeline
(Claude CLI → Codex CLI → DeepSeek API) instead of a single Anthropic API backend.

Usage:
    from eta_engine.brain.multi_model_executor import MultiModelExecutor
    fleet = Fleet(executor=MultiModelExecutor(), deepseek_personas=True)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from eta_engine.brain.llm_provider import ModelTier
from eta_engine.brain.model_policy import force_provider_for
from eta_engine.brain.multi_model import route_and_execute

if TYPE_CHECKING:
    from pathlib import Path

    from eta_engine.brain.avengers.base import TaskEnvelope

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class MultiModelExecutor:
    """Implements the Avengers ``Executor`` Protocol using the Force Multiplier orchestrator.

    Each task is routed to the best provider:
      * CLAUDE   (Lead Architect) — architectural, red team, code review
      * DEEPSEEK (Worker Bee)     — high-volume generation, boilerplate, grunt
      * CODEX    (Systems Expert) — debugging, test execution, security audits

    Falls back to DeepSeek API if Claude/Codex CLI is unavailable.
    """

    def __call__(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        envelope: TaskEnvelope,
    ) -> str:
        category = envelope.category
        preferred = force_provider_for(category)

        logger.info(
            "MultiModelExecutor: category=%s tier=%s provider=%s",
            category.value,
            tier.value,
            preferred.value,
        )

        resp = route_and_execute(
            category=category,
            system_prompt=system_prompt,
            user_message=user_prompt,
            max_tokens=4096 if tier == ModelTier.OPUS else 2048,
            temperature=0.3 if tier == ModelTier.OPUS else 0.7,
        )

        if resp.fallback_used:
            logger.warning(
                "MultiModelExecutor: %s task fell back from %s to %s: %s",
                category.value,
                preferred.value,
                resp.provider.value,
                resp.fallback_reason,
            )

        return resp.text


def create_multimodel_fleet(
    *,
    admin: Any = None,  # noqa: ANN401  (Fleet's admin field accepts any controller object)
    journal_path: Path | str | None = None,
) -> Any:  # noqa: ANN401  (returns Fleet — typed Any to avoid runtime import here)
    """Factory: create a Fleet wired with MultiModelExecutor and DeepSeek personas."""
    from eta_engine.brain.avengers.fleet import Fleet  # noqa: PLC0415

    return Fleet(
        admin=admin,
        executor=MultiModelExecutor(),
        journal_path=journal_path,
        deepseek_personas=_env_bool("ETA_USE_DEEPSEEK_PERSONAS", False),
    )
