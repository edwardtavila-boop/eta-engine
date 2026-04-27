"""
EVOLUTIONARY TRADING ALGO  //  strategies.bandit_store_pg
=========================================================
Postgres + pgvector backend for the strategy bandit history.

Why
---
The current bandit history is JSONL-stored in
``~/.local/state/eta_engine/bandit/<strategy>.jsonl``. Queries scan
the whole file. With ~6 strategies * 50 trades/day * 365 days, that
hits 100k+ rows per scan. pgvector indexes the regime-context vector
so similarity queries (``find the 10 most-similar past contexts to
right now``) are O(log n) instead of O(n).

Public API
----------

* :class:`BanditStorePostgres` -- async store with the same API
  surface as the JSONL-backed ``BanditStore``.
* :func:`is_pgvector_available()` -- import-time + connect-time probe.

Schema
------

::

    CREATE EXTENSION IF NOT EXISTS vector;

    CREATE TABLE IF NOT EXISTS bandit_history (
        id           BIGSERIAL  PRIMARY KEY,
        ts           TIMESTAMPTZ NOT NULL,
        strategy     TEXT       NOT NULL,
        bot          TEXT       NOT NULL,
        action       TEXT       NOT NULL,        -- ENTER / EXIT / SKIP
        reward       DOUBLE PRECISION,           -- realized PnL pts
        regime       TEXT,                       -- regime label
        context_vec  vector(8),                  -- regime context embed
        meta         JSONB
    );
    CREATE INDEX IF NOT EXISTS bandit_history_strategy_ts
        ON bandit_history(strategy, ts DESC);
    CREATE INDEX IF NOT EXISTS bandit_history_ctx_ivfflat
        ON bandit_history
        USING ivfflat (context_vec vector_cosine_ops)
        WITH (lists = 100);

The 8-D context vector mirrors the regime-context shape:
``[trend, vol, microstructure, breadth, dxy, gold_btc, funding, spread]``.

Optional dep on ``asyncpg``.  When absent, ``BanditStorePostgres``
construction raises ``BanditStorePgUnavailable``.  Callers that
want graceful fallback should ``try``-import + check
:func:`is_pgvector_available`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


class BanditStorePgError(Exception):
    """Generic Postgres bandit-store error."""


class BanditStorePgUnavailable(BanditStorePgError):  # noqa: N818 -- "Unavailable" reads better than "UnavailableError" at call sites
    """asyncpg not installed, or Postgres unreachable."""


CONTEXT_DIM = 8


# ---------------------------------------------------------------------------
# Record dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BanditTrial:
    ts:           datetime
    strategy:     str
    bot:          str
    action:       str
    reward:       float | None
    regime:       str | None
    context_vec:  tuple[float, ...]   # length CONTEXT_DIM
    meta:         dict[str, Any] = field(default_factory=dict)


def _default_dsn() -> str:
    return os.environ.get(
        "ETA_BANDIT_PG_DSN",
        "postgresql://eta:eta@127.0.0.1:5432/eta_engine",
    )


def is_pgvector_available(dsn: str | None = None) -> bool:
    """Probe whether ``asyncpg`` imports + the dsn is reachable + extension installed.

    Synchronous probe (uses an inline asyncio.run). Safe to call from
    sync context. Returns False on any failure.
    """
    try:
        import asyncio

        import asyncpg  # type: ignore[import-not-found]
    except ImportError:
        return False

    async def _probe() -> bool:
        try:
            conn = await asyncpg.connect(dsn or _default_dsn(), timeout=1.0)
        except Exception:  # noqa: BLE001
            return False
        try:
            row = await conn.fetchrow(
                "SELECT extname FROM pg_extension WHERE extname='vector'",
            )
            return row is not None
        finally:
            await conn.close()

    try:
        return asyncio.run(_probe())
    except Exception as e:  # noqa: BLE001
        log.debug("is_pgvector_available: %s", e)
        return False


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS bandit_history (
    id           BIGSERIAL  PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    strategy     TEXT       NOT NULL,
    bot          TEXT       NOT NULL,
    action       TEXT       NOT NULL,
    reward       DOUBLE PRECISION,
    regime       TEXT,
    context_vec  vector(8),
    meta         JSONB
);
CREATE INDEX IF NOT EXISTS bandit_history_strategy_ts
    ON bandit_history(strategy, ts DESC);
CREATE INDEX IF NOT EXISTS bandit_history_ctx_ivfflat
    ON bandit_history
    USING ivfflat (context_vec vector_cosine_ops)
    WITH (lists = 100);
"""


def _vec_literal(vec: tuple[float, ...]) -> str:
    """Format a vector for pgvector's text input.

    pgvector expects ``'[1.0,2.0,3.0]'`` (square brackets, no spaces).
    """
    if len(vec) != CONTEXT_DIM:
        raise BanditStorePgError(
            f"context_vec must be length {CONTEXT_DIM}, got {len(vec)}",
        )
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


class BanditStorePostgres:
    """Async Postgres+pgvector backed bandit history."""

    def __init__(self, dsn: str | None = None) -> None:
        try:
            import asyncpg  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as e:
            raise BanditStorePgUnavailable(
                "asyncpg not installed; pip install asyncpg",
            ) from e
        self._dsn = dsn or _default_dsn()
        self._pool: Any = None

    async def connect(self) -> None:
        import asyncpg  # type: ignore[import-not-found]

        if self._pool is None:
            try:
                self._pool = await asyncpg.create_pool(
                    self._dsn, min_size=1, max_size=5, timeout=2.0,
                )
            except Exception as e:  # noqa: BLE001
                raise BanditStorePgUnavailable(f"postgres connect failed: {e}") from e

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def init_schema(self) -> None:
        """Run the DDL idempotently. Caller does this once at boot."""
        await self.connect()
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def append(self, trial: BanditTrial) -> int:
        """Insert one trial; return the new row id."""
        await self.connect()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO bandit_history
                  (ts, strategy, bot, action, reward, regime, context_vec, meta)
                VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8)
                RETURNING id
                """,
                trial.ts,
                trial.strategy,
                trial.bot,
                trial.action,
                trial.reward,
                trial.regime,
                _vec_literal(trial.context_vec),
                trial.meta,
            )
            return int(row["id"])

    async def recent(
        self,
        strategy: str,
        limit: int = 100,
    ) -> list[BanditTrial]:
        """Return up to ``limit`` most-recent trials for ``strategy``."""
        await self.connect()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts, strategy, bot, action, reward, regime, context_vec, meta
                FROM bandit_history
                WHERE strategy = $1
                ORDER BY ts DESC
                LIMIT $2
                """,
                strategy, limit,
            )
            return [_row_to_trial(r) for r in rows]

    async def similar(
        self,
        context_vec: tuple[float, ...],
        strategy: str | None = None,
        limit: int = 10,
    ) -> list[BanditTrial]:
        """Return the ``limit`` rows whose context_vec is closest by cosine.

        When ``strategy`` is provided, restrict to that strategy.
        """
        await self.connect()
        vec_lit = _vec_literal(context_vec)
        async with self._pool.acquire() as conn:
            if strategy:
                rows = await conn.fetch(
                    """
                    SELECT ts, strategy, bot, action, reward, regime, context_vec, meta
                    FROM bandit_history
                    WHERE strategy = $2
                    ORDER BY context_vec <=> $1::vector
                    LIMIT $3
                    """,
                    vec_lit, strategy, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT ts, strategy, bot, action, reward, regime, context_vec, meta
                    FROM bandit_history
                    ORDER BY context_vec <=> $1::vector
                    LIMIT $2
                    """,
                    vec_lit, limit,
                )
            return [_row_to_trial(r) for r in rows]


def _row_to_trial(row: Any) -> BanditTrial:  # noqa: ANN401 -- asyncpg Record dynamic
    raw_vec = row["context_vec"]
    if isinstance(raw_vec, str):
        # asyncpg returns vectors as text by default unless pgvector
        # codec is registered; parse '[1.0, 2.0, ...]' string.
        cleaned = raw_vec.strip().lstrip("[").rstrip("]")
        vec = tuple(float(x) for x in cleaned.split(",")) if cleaned else ()
    else:
        vec = tuple(float(x) for x in raw_vec)
    ts = row["ts"]
    if not isinstance(ts, datetime):
        ts = datetime.now(UTC)
    return BanditTrial(
        ts=ts,
        strategy=row["strategy"],
        bot=row["bot"],
        action=row["action"],
        reward=row["reward"],
        regime=row["regime"],
        context_vec=vec,
        meta=row.get("meta") or {},
    )
