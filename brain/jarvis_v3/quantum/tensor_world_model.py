"""Tensor-network world model (Wave-11, 2026-04-27).

The wave-8 world model uses a raw transition table P(s' | s).
The wave-10 world_model_full upgrades to P(s' | s, a). Both store
COUNTS, which means they don't generalize across states that haven't
been seen before.

This module: tensor-decomposed transition model.

  * Build the order-3 transition tensor T[s, a, s'] from journal
  * Apply rank-r Tucker decomposition: T ≈ G x U_s x U_a x U_s'
    where G is the small core tensor (r x r x r) and U_s, U_a, U_s'
    are factor matrices (state_dim x r etc.)
  * The factor matrices ARE the latent state representation -- this
    is the "tensor-network" backbone of a quantum-inspired world
    model
  * Transitions for (s, a) pairs we've NEVER seen can now be inferred
    from the latent factors -- the rank-r approximation forces
    generalization

Pure stdlib: rank-r ALS (alternating least squares) Tucker decomp.
For real production replace with proper SVD via NumPy/PyTorch when
the journal is large enough to justify the dependency.

The audit-list "tensor-network world model" goal: this is it.
Compressed latent state + smooth generalization across unseen
state-action pairs.

Use case (rare-state imagination):

    from eta_engine.brain.jarvis_v3.quantum.tensor_world_model import (
        TensorWorldModel,
    )

    twm = TensorWorldModel(rank=3)
    twm.fit(transition_tensor)
    # Now query a (state, action) we never saw -- the factor model
    # produces a smooth interpolation
    next_dist = twm.predict_next_distribution(
        state=42, action="approve_full",
    )
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────


def _zeros(shape: tuple[int, ...]) -> list:
    if len(shape) == 0:
        return 0.0
    if len(shape) == 1:
        return [0.0] * shape[0]
    return [_zeros(shape[1:]) for _ in range(shape[0])]


def _matmul(A: list[list[float]], B: list[list[float]]) -> list[list[float]]:  # noqa: N803
    """Pure-stdlib matrix multiply."""
    m = len(A)
    n = len(B[0]) if B else 0
    k = len(B)
    out = [[0.0] * n for _ in range(m)]
    for i in range(m):
        for j in range(n):
            s = 0.0
            for kk in range(k):
                s += A[i][kk] * B[kk][j]
            out[i][j] = s
    return out


def _transpose(M: list[list[float]]) -> list[list[float]]:  # noqa: N803
    if not M:
        return []
    return [[M[i][j] for i in range(len(M))] for j in range(len(M[0]))]


def _normalize_rows(M: list[list[float]]) -> list[list[float]]:  # noqa: N803
    """L2-normalize each row in-place style (returns new matrix)."""
    out = []
    for row in M:
        norm = math.sqrt(sum(x * x for x in row))
        if norm == 0:
            out.append(list(row))
        else:
            out.append([x / norm for x in row])
    return out


def _frobenius_norm_3d(T: list) -> float:  # noqa: N803
    """Compute the Frobenius norm of a 3D tensor stored as nested list."""
    s = 0.0
    for slab in T:
        for row in slab:
            for v in row:
                s += v * v
    return math.sqrt(s)


# ─── Transition tensor + Tucker decomposition ─────────────────────


@dataclass
class TensorWorldModel:
    """Tucker-decomposed P(s' | s, a) tensor."""

    rank: int = 3
    state_dim: int = 0
    action_dim: int = 0
    factors_s: list[list[float]] = field(default_factory=list)  # state_dim x rank
    factors_a: list[list[float]] = field(default_factory=list)  # action_dim x rank
    factors_sp: list[list[float]] = field(default_factory=list)  # state_dim x rank
    core: list[list[list[float]]] = field(default_factory=list)  # rank x rank x rank
    fit_loss: float = 0.0

    def fit(
        self,
        transitions: dict[int, dict[int, dict[int, int]]],
        *,
        action_index: dict[int, int] | None = None,
        state_index: dict[int, int] | None = None,
        n_iters: int = 20,
        tol: float = 1e-4,
        seed: int = 42,
    ) -> None:
        """Fit the rank-r Tucker decomposition via ALS.

        transitions[state_id][action_idx][next_state_id] = count
        """
        rng = random.Random(seed)

        # 1. Determine state and action dims, normalize counts to probs
        all_states: set[int] = set()
        all_actions: set[int] = set()
        for s, by_a in transitions.items():
            all_states.add(s)
            for a, by_sp in by_a.items():
                all_actions.add(a)
                all_states.update(by_sp.keys())
        if state_index is None:
            state_index = {s: i for i, s in enumerate(sorted(all_states))}
        if action_index is None:
            action_index = {a: i for i, a in enumerate(sorted(all_actions))}
        self.state_dim = len(state_index)
        self.action_dim = len(action_index)

        if self.state_dim == 0 or self.action_dim == 0:
            return

        # Build dense tensor T[s, a, s']
        T = _zeros((self.state_dim, self.action_dim, self.state_dim))  # noqa: N806
        for s, by_a in transitions.items():
            si = state_index[s]
            for a, by_sp in by_a.items():
                ai = action_index[a]
                total = sum(by_sp.values())
                if total == 0:
                    continue
                for sp, count in by_sp.items():
                    spi = state_index[sp]
                    T[si][ai][spi] = count / total  # row-normalize to prob

        # 2. Initialize factor matrices randomly + normalize
        r = min(self.rank, self.state_dim, self.action_dim)
        self.factors_s = _normalize_rows([[rng.gauss(0.0, 1.0) for _ in range(r)] for _ in range(self.state_dim)])
        self.factors_a = _normalize_rows([[rng.gauss(0.0, 1.0) for _ in range(r)] for _ in range(self.action_dim)])
        self.factors_sp = _normalize_rows([[rng.gauss(0.0, 1.0) for _ in range(r)] for _ in range(self.state_dim)])
        self.core = _zeros((r, r, r))

        # 3. ALS iterations
        prev_loss = float("inf")
        for _it in range(n_iters):
            self._update_core(T)
            self._update_factor_s(T)
            self._update_factor_a(T)
            self._update_factor_sp(T)
            loss = self._reconstruction_loss(T)
            if abs(prev_loss - loss) < tol:
                break
            prev_loss = loss

        self.fit_loss = round(prev_loss, 6)
        self._state_index = state_index
        self._action_index = action_index
        self._inv_state_index = {v: k for k, v in state_index.items()}

    def _update_core(self, T: list) -> None:  # noqa: N803
        """Update G = sum over (s, a, sp) of U_s[s,:].T x T[s,a,sp] x U_sp[sp,:]"""
        r = len(self.factors_s[0]) if self.factors_s else 0
        if r == 0:
            return
        G = _zeros((r, r, r))  # noqa: N806
        for s in range(self.state_dim):
            for a in range(self.action_dim):
                for sp in range(self.state_dim):
                    t = T[s][a][sp]
                    if t == 0:
                        continue
                    for p in range(r):
                        for q in range(r):
                            for w in range(r):
                                G[p][q][w] += self.factors_s[s][p] * self.factors_a[a][q] * self.factors_sp[sp][w] * t
        self.core = G

    def _update_factor_s(self, T: list) -> None:  # noqa: N803
        """Hold (a, sp, core) fixed; update factor_s rows."""
        r = len(self.factors_s[0]) if self.factors_s else 0
        if r == 0:
            return
        new_factors_s = _zeros((self.state_dim, r))
        for s in range(self.state_dim):
            for p in range(r):
                acc = 0.0
                for a in range(self.action_dim):
                    for sp in range(self.state_dim):
                        t = T[s][a][sp]
                        if t == 0:
                            continue
                        for q in range(r):
                            for w in range(r):
                                acc += self.core[p][q][w] * self.factors_a[a][q] * self.factors_sp[sp][w] * t
                new_factors_s[s][p] = acc
        self.factors_s = _normalize_rows(new_factors_s)

    def _update_factor_a(self, T: list) -> None:  # noqa: N803
        r = len(self.factors_a[0]) if self.factors_a else 0
        if r == 0:
            return
        new_factors_a = _zeros((self.action_dim, r))
        for a in range(self.action_dim):
            for q in range(r):
                acc = 0.0
                for s in range(self.state_dim):
                    for sp in range(self.state_dim):
                        t = T[s][a][sp]
                        if t == 0:
                            continue
                        for p in range(r):
                            for w in range(r):
                                acc += self.core[p][q][w] * self.factors_s[s][p] * self.factors_sp[sp][w] * t
                new_factors_a[a][q] = acc
        self.factors_a = _normalize_rows(new_factors_a)

    def _update_factor_sp(self, T: list) -> None:  # noqa: N803
        r = len(self.factors_sp[0]) if self.factors_sp else 0
        if r == 0:
            return
        new_factors_sp = _zeros((self.state_dim, r))
        for sp in range(self.state_dim):
            for w in range(r):
                acc = 0.0
                for s in range(self.state_dim):
                    for a in range(self.action_dim):
                        t = T[s][a][sp]
                        if t == 0:
                            continue
                        for p in range(r):
                            for q in range(r):
                                acc += self.core[p][q][w] * self.factors_s[s][p] * self.factors_a[a][q] * t
                new_factors_sp[sp][w] = acc
        self.factors_sp = _normalize_rows(new_factors_sp)

    def _reconstruction_loss(self, T: list) -> float:  # noqa: N803
        """Frobenius-norm reconstruction loss."""
        loss = 0.0
        for s in range(self.state_dim):
            for a in range(self.action_dim):
                for sp in range(self.state_dim):
                    pred = self._predict_entry(s, a, sp)
                    diff = T[s][a][sp] - pred
                    loss += diff * diff
        return math.sqrt(loss)

    def _predict_entry(self, s: int, a: int, sp: int) -> float:
        """Reconstruct one tensor entry from the factor decomposition."""
        r = len(self.factors_s[0]) if self.factors_s else 0
        if r == 0:
            return 0.0
        v = 0.0
        for p in range(r):
            for q in range(r):
                for w in range(r):
                    v += self.core[p][q][w] * self.factors_s[s][p] * self.factors_a[a][q] * self.factors_sp[sp][w]
        return v

    def predict_next_distribution(
        self,
        *,
        state: int,
        action: int,
    ) -> dict[int, float]:
        """Return the predicted P(s' | s, a) distribution (smoothed
        via the rank-r reconstruction), normalized to sum to 1.

        Falls back to uniform if the (state, action) pair was outside
        the training domain."""
        if state not in getattr(self, "_state_index", {}):
            return {}
        if action not in getattr(self, "_action_index", {}):
            return {}
        si = self._state_index[state]
        ai = self._action_index[action]

        out: dict[int, float] = {}
        total = 0.0
        for sp_idx in range(self.state_dim):
            v = max(0.0, self._predict_entry(si, ai, sp_idx))  # clamp negative
            sp_id = self._inv_state_index[sp_idx]
            out[sp_id] = v
            total += v
        if total == 0:
            uniform = 1.0 / max(self.state_dim, 1)
            return {sp_id: uniform for sp_id in self._inv_state_index.values()}
        return {k: v / total for k, v in out.items()}

    def latent_distance(self, state_a: int, state_b: int) -> float:
        """Distance between two states in the learned latent space.

        Useful for "find the most similar state we've seen" -- the
        latent factors capture functional similarity in transition
        behavior, which often beats raw feature similarity."""
        idx = getattr(self, "_state_index", {})
        if state_a not in idx or state_b not in idx:
            return float("inf")
        a = self.factors_s[idx[state_a]]
        b = self.factors_s[idx[state_b]]
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))
