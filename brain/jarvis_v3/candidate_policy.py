"""Candidate-policy callable interface (Tier-2 #7, 2026-04-27).

Unblocks 3 separate scaffolds:
  * scripts/score_policy_candidate.py -- replay a candidate over the
    last 30 days and compare metrics to the champion
  * brain/jarvis_v3/bandit_harness.py -- multi-arm bandit between
    champion + candidate(s)
  * future kaizen-driven policy proposals -- the kaizen loop produces
    a +1 ticket whose realization is a candidate policy ready to score

Contract
--------
A candidate policy is a callable matching::

    Callable[[ActionRequest, JarvisContext], ActionResponse]

i.e. the same signature as ``jarvis_admin.evaluate_request``. Authors
register them with the registry; the score + bandit harnesses pull
them by name.

Authoring a candidate
---------------------

::

    from eta_engine.brain.candidate_policy import register_candidate
    from eta_engine.brain.jarvis_admin import (
        ActionRequest, ActionResponse, Verdict,
    )

    def evaluate_v18(req, ctx):
        # Same as champion v17 but tightens MIN_CONFLUENCE from 8.0 to 7.5
        from eta_engine.brain.jarvis_admin import evaluate_request
        resp = evaluate_request(req, ctx)
        # ... post-process (e.g. flip CONDITIONAL caps from 0.50 to 0.60)
        return resp

    register_candidate("v18", evaluate_v18, parent_version=17,
                       rationale="lower MIN_CONFLUENCE 8.0 -> 7.5 per kaizen ticket KZN-2026-04-27-...")

Scoring
-------

Once registered, ``scripts/score_policy_candidate.py --candidate v18``
loads the callable, replays the last N days of audit records through
it, and prints a side-by-side comparison vs the champion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Protocol


class _ActionRequestLike(Protocol):
    """Minimal duck-type for an ActionRequest -- lets candidate authors
    avoid importing the whole jarvis_admin module just to satisfy the
    type hint. Real ActionRequest fields are richer; this is just what
    candidates can rely on existing."""

    request_id: str
    subsystem: str
    action: str
    payload: dict[str, Any]


class _JarvisContextLike(Protocol):
    """Minimal duck-type for a JarvisContext."""

    ts: datetime
    suggestion: Any
    sizing_hint: Any
    session_phase: Any
    stress_score: Any


CandidatePolicy = Callable[[_ActionRequestLike, _JarvisContextLike], Any]


@dataclass
class _CandidateRegistration:
    name: str
    policy: CandidatePolicy
    parent_version: int = 0
    rationale: str = ""
    registered_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


# Module-level registry. Lookups are by name; iteration preserves
# registration order.
_REGISTRY: dict[str, _CandidateRegistration] = {}


def register_candidate(
    name: str,
    policy: CandidatePolicy,
    *,
    parent_version: int = 0,
    rationale: str = "",
    metadata: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> None:
    """Register a candidate-policy callable by name.

    ``parent_version`` is the policy version this candidate forks from
    (typically the current champion). ``rationale`` documents the
    intended improvement so the audit trail explains WHY this candidate
    exists -- useful when the bandit converges to it 6 months later
    and someone asks "what changed in v23?".
    """
    if name in _REGISTRY and not overwrite:
        raise ValueError(
            f"candidate '{name}' already registered (use overwrite=True to replace)"
        )
    if not callable(policy):
        raise TypeError(f"policy for '{name}' must be callable, got {type(policy)}")
    _REGISTRY[name] = _CandidateRegistration(
        name=name,
        policy=policy,
        parent_version=int(parent_version),
        rationale=rationale,
        metadata=metadata or {},
    )


def get_candidate(name: str) -> CandidatePolicy:
    """Return the callable registered under ``name``. Raises KeyError
    if no such candidate is registered."""
    if name not in _REGISTRY:
        raise KeyError(f"no candidate registered as '{name}'")
    return _REGISTRY[name].policy


def list_candidates() -> list[dict[str, Any]]:
    """Read-only snapshot of every registered candidate (no callable)."""
    return [
        {
            "name": r.name,
            "parent_version": r.parent_version,
            "rationale": r.rationale,
            "registered_at": r.registered_at.isoformat(),
            "metadata": r.metadata,
        }
        for r in _REGISTRY.values()
    ]


def clear_registry() -> None:
    """Drop all registered candidates (test fixture helper)."""
    _REGISTRY.clear()
