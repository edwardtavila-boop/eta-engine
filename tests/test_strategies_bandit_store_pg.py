"""Tests for ``eta_engine.strategies.bandit_store_pg``.

Most tests target the pure helpers (``_vec_literal``,
``_row_to_trial``) since exercising the real Postgres path requires a
running cluster + pgvector extension. The integration test is gated
behind ``is_pgvector_available()``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from eta_engine.strategies.bandit_store_pg import (
    CONTEXT_DIM,
    BanditStorePgError,
    BanditTrial,
    _row_to_trial,
    _vec_literal,
    is_pgvector_available,
)


def test_vec_literal_basic() -> None:
    s = _vec_literal((1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0))
    assert s == "[1.000000,2.000000,3.000000,4.000000,5.000000,6.000000,7.000000,8.000000]"


def test_vec_literal_rejects_wrong_dim() -> None:
    with pytest.raises(BanditStorePgError, match="length 8"):
        _vec_literal((1.0, 2.0))


def test_context_dim_constant() -> None:
    assert CONTEXT_DIM == 8


def test_row_to_trial_with_string_vector() -> None:
    row = {
        "ts": datetime(2026, 4, 27, tzinfo=UTC),
        "strategy": "alpha",
        "bot": "mnq",
        "action": "ENTER",
        "reward": 1.5,
        "regime": "trending_up",
        "context_vec": "[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]",
        "meta": {"k": "v"},
    }
    trial = _row_to_trial(row)
    assert trial.strategy == "alpha"
    assert trial.context_vec == (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
    assert trial.reward == 1.5
    assert trial.meta == {"k": "v"}


def test_row_to_trial_with_list_vector() -> None:
    row = {
        "ts": datetime(2026, 4, 27, tzinfo=UTC),
        "strategy": "beta", "bot": "btc", "action": "EXIT",
        "reward": None, "regime": None,
        "context_vec": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        "meta": None,
    }
    trial = _row_to_trial(row)
    assert trial.context_vec == (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
    assert trial.meta == {}
    assert trial.reward is None


def test_row_to_trial_handles_empty_vec() -> None:
    row = {
        "ts": datetime(2026, 4, 27, tzinfo=UTC),
        "strategy": "x", "bot": "y", "action": "SKIP",
        "reward": None, "regime": None,
        "context_vec": "[]",
        "meta": {},
    }
    trial = _row_to_trial(row)
    assert trial.context_vec == ()


def test_is_pgvector_available_returns_bool() -> None:
    assert isinstance(is_pgvector_available(), bool)


def test_store_construction_raises_when_asyncpg_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    # Reimport so the fresh sys.modules state is honored.
    from importlib import reload

    import eta_engine.strategies.bandit_store_pg as mod
    reload(mod)
    # NOTE: after reload, the exception class identity is the freshly
    # loaded one, not the symbol we imported at module top.
    with pytest.raises(mod.BanditStorePgUnavailable):
        mod.BanditStorePostgres()
    # Restore module state for any subsequent tests in the same session.
    monkeypatch.delitem(sys.modules, "asyncpg", raising=False)
    reload(mod)


def test_bandit_trial_dataclass_fields() -> None:
    t = BanditTrial(
        ts=datetime(2026, 4, 27, tzinfo=UTC),
        strategy="s", bot="b", action="ENTER",
        reward=2.0, regime="r",
        context_vec=tuple([0.0] * 8),
    )
    assert t.strategy == "s"
    assert t.meta == {}
