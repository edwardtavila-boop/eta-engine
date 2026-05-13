"""Champion policy v17 (2026-04-27).

This is the current production JARVIS evaluator. We register it as a
named candidate so the bandit harness can refer to it by name (``v17``)
and the replay scoring engine has a labelled baseline.

``evaluate_v17`` is a pure passthrough to ``jarvis_admin.evaluate_request``.
Future champions get their own version-numbered modules in this package
and update the ``CHAMPION_NAME`` in ``policies/__init__.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_admin import (
    ActionRequest,
    ActionResponse,
    evaluate_request,
)
from eta_engine.brain.jarvis_v3.candidate_policy import register_candidate

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_context import JarvisContext


def evaluate_v17(req: ActionRequest, ctx: JarvisContext) -> ActionResponse:
    """Champion policy. Pure passthrough to evaluate_request."""
    return evaluate_request(req, ctx)


register_candidate(
    "v17",
    evaluate_v17,
    parent_version=0,
    rationale="champion baseline -- the current production evaluate_request",
    overwrite=True,  # tolerate re-import in test fixtures
)
