"""SUPERVISOR_LOCAL exit-path tests for crypto positions.

Pins the `reduce_only` round-trip across the supervisor → broker_router
→ venue contract. Crypto venues (Alpaca crypto, IBKR-PAXOS crypto) use
`bracket_style=SUPERVISOR_LOCAL`: the broker has no server-side OCO,
so the supervisor watches each bar and ships a `reduce_only=True` exit
when price pierces a planned bracket level. Without these tests, any
of the following would silently break crypto exits:

* `_write_pending_order` not serializing `reduce_only` → exit lands as
  a fresh-entry pending file, broker_router builds an OrderRequest with
  `reduce_only=False`, the venue applies the bracket-required check
  (rejects naked) OR re-attaches a bracket on the exit (which either
  rejects on Alpaca crypto or, worse, places fresh OCO siblings).
* `parse_pending_file` not reading `reduce_only` → JSON-side flag
  survives but is dropped at parse time.
* `broker_router._build_order_request` not forwarding `reduce_only`
  to the OrderRequest → JSON had it, parse had it, but the venue sees
  False on the wire. Same downstream blast radius.

The fourth test pins the existing alpaca contract so a future change
that re-enables bracket attachment for reduce_only orders can't slip
through code review.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# 1. Pending-order JSON round-trip
# ---------------------------------------------------------------------------


def test_pending_order_json_round_trip_preserves_reduce_only(
    tmp_path: Path,
) -> None:
    """A pending file written with reduce_only=True parses back as True.

    The contract is symmetric: write, read, assert. Without `reduce_only`
    on the PendingOrder dataclass and in the parse_pending_file payload
    keys, the flag would be silently dropped at parse time and every
    downstream OrderRequest would land at the venue with reduce_only=False.
    """
    from eta_engine.scripts.broker_router import (
        PendingOrder,
        parse_pending_file,
    )

    # Write a pending file mimicking what the supervisor SHOULD emit
    # for a SUPERVISOR_LOCAL exit. reduce_only=True is the load-bearing
    # field; the rest are bracket carryover the parser already handles.
    pending = tmp_path / "btc_optimized.pending_order.json"
    pending.write_text(
        json.dumps(
            {
                "ts": "2026-05-06T12:00:00+00:00",
                "signal_id": "exit-sig-001",
                "side": "SELL",
                "qty": 0.05,
                "symbol": "BTC",
                "limit_price": 81_900.0,
                "stop_price": None,
                "target_price": None,
                "reduce_only": True,
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_pending_file(pending)

    assert isinstance(parsed, PendingOrder)
    assert parsed.reduce_only is True
    # Sanity: the rest of the contract still parses.
    assert parsed.side == "SELL"
    assert parsed.qty == 0.05
    assert parsed.symbol == "BTC"
    assert parsed.bot_id == "btc_optimized"


def test_pending_order_json_default_reduce_only_is_false(
    tmp_path: Path,
) -> None:
    """Older pending files without `reduce_only` parse as entries.

    Back-compat: the field is optional, default False. Entries are the
    dominant case so the conservative default is safe — a missing flag
    on an exit file would be the bug, but a missing flag on an entry
    file is the historical behaviour we must keep working.
    """
    from eta_engine.scripts.broker_router import parse_pending_file

    pending = tmp_path / "alpha.pending_order.json"
    pending.write_text(
        json.dumps(
            {
                "ts": "2026-05-06T12:00:00+00:00",
                "signal_id": "entry-001",
                "side": "BUY",
                "qty": 1.0,
                "symbol": "MNQ",
                "limit_price": 18_000.0,
                "stop_price": 17_900.0,
                "target_price": 18_100.0,
                # reduce_only intentionally absent — older file format.
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_pending_file(pending)

    assert parsed.reduce_only is False


# ---------------------------------------------------------------------------
# 2. Supervisor exit emits reduce_only=True
# ---------------------------------------------------------------------------


def test_supervisor_exit_for_crypto_writes_reduce_only_pending(
    tmp_path: Path,
) -> None:
    """When the supervisor ships an exit through _write_pending_order,
    the resulting JSON carries reduce_only=True so broker_router can
    forward it to the venue. Without this, a SUPERVISOR_LOCAL crypto
    exit would land at Alpaca as a fresh-entry-shaped order — the
    bracket-required check would reject it, OR the venue would attach
    a bracket on a SELL with no inventory and re-open the position.

    We exercise _write_pending_order directly (it's the serialization
    boundary) with a synthetic exit FillRecord. submit_exit currently
    closes positions in-process; this test pins the JSON shape so a
    future caller that ROUTES exits through broker_router gets the
    flag for free.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        FillRecord,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="btc_optimized",
        symbol="BTC",
        strategy_kind="crypto_local",
        direction="long",
        cash=5_000.0,
    )
    # Simulate the bot holding an open BTC position with planned
    # bracket levels. _write_pending_order reads bracket_stop /
    # bracket_target off bot.open_position; clearing it here would
    # serialize them as null (still legal).
    bot.open_position = {
        "side": "BUY",
        "qty": 0.05,
        "entry_price": 80_500.0,
        "entry_ts": "2026-05-06T11:00:00+00:00",
        "signal_id": "entry-sig-001",
        "bracket_stop": 79_000.0,
        "bracket_target": 81_900.0,
    }
    # Build an EXIT FillRecord — side flipped (SELL), price at the
    # piercing target, paper=True to mirror what submit_exit produces.
    exit_rec = FillRecord(
        bot_id="btc_optimized",
        signal_id="exit-sig-001",
        side="SELL",
        symbol="BTC",
        qty=0.05,
        fill_price=81_900.0,
        fill_ts="2026-05-06T12:00:00+00:00",
        paper=True,
        note="close pnl=+70.00",
    )

    # Caller passes reduce_only=True to flag the EXIT semantics. This
    # is the boundary the supervisor MUST cross when routing exits
    # through broker_router; the test pins the kwarg.
    router._write_pending_order(bot, exit_rec, reduce_only=True)

    written = tmp_path / "btc_optimized.pending_order.json"
    assert written.exists(), "pending_order.json was not created"
    payload = json.loads(written.read_text(encoding="utf-8"))

    # The flag on the wire — load-bearing for the venue's exit handling.
    assert payload["reduce_only"] is True, f"expected reduce_only=True in pending JSON, got {payload!r}"
    # Sanity: side/qty/symbol passthrough still intact.
    assert payload["side"] == "SELL"
    assert payload["qty"] == 0.05
    assert payload["symbol"] == "BTC"


def test_supervisor_exit_with_zero_broker_qty_clears_without_pending(
    tmp_path: Path,
) -> None:
    """A stale supervisor position must not ship a qty=0 reduce-only order.

    Live failure mode: Alpaca/IBKR already showed flat for a bot, but the
    supervisor still had a local open_position. submit_exit reconciled to
    broker_qty=0, then wrote ``qty: 0`` pending_order files that the broker
    router had to quarantine. The safe behavior is to clear the stale local
    position, book no close, and write no broker intent.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    cfg.state_dir = tmp_path / "state"
    pending_dir = tmp_path / "pending"
    router = ExecutionRouter(cfg=cfg, bf_dir=pending_dir)
    router._get_broker_position_qty = lambda _bot: 0.0  # type: ignore[method-assign]

    bot = BotInstance(
        bot_id="volume_profile_btc",
        symbol="BTC",
        strategy_kind="crypto_local",
        direction="long",
        cash=5_000.0,
    )
    bot.open_position = {
        "side": "BUY",
        "qty": 0.05,
        "entry_price": 80_500.0,
        "entry_ts": "2026-05-06T11:00:00+00:00",
        "signal_id": "entry-sig-001",
        "bracket_stop": 79_000.0,
        "bracket_target": 81_900.0,
    }
    router._persist_open_position(bot)

    rec = router.submit_exit(bot=bot, bar={"close": 81_900.0})

    assert rec is None
    assert bot.open_position is None
    assert bot.n_exits == 0
    assert bot.realized_pnl == 0.0
    assert not (pending_dir / "volume_profile_btc.pending_order.json").exists()
    persisted = cfg.state_dir / "open_positions" / "volume_profile_btc" / "open_position.json"
    assert not persisted.exists()


def test_supervisor_entry_pending_default_reduce_only_is_false(
    tmp_path: Path,
) -> None:
    """Entries (the default _write_pending_order call shape) emit
    reduce_only=False. Pins the historical contract so adding the new
    kwarg doesn't accidentally flip every entry on the wire.
    """
    from eta_engine.scripts.jarvis_strategy_supervisor import (
        BotInstance,
        ExecutionRouter,
        FillRecord,
        SupervisorConfig,
    )

    cfg = SupervisorConfig()
    cfg.mode = "paper_live"
    router = ExecutionRouter(cfg=cfg, bf_dir=tmp_path)
    bot = BotInstance(
        bot_id="alpha",
        symbol="MNQ",
        strategy_kind="futures",
        direction="long",
        cash=5_000.0,
    )
    bot.open_position = {
        "side": "BUY",
        "qty": 1.0,
        "entry_price": 18_000.0,
        "entry_ts": "2026-05-06T11:00:00+00:00",
        "signal_id": "entry-sig-002",
        "bracket_stop": 17_900.0,
        "bracket_target": 18_100.0,
    }
    rec = FillRecord(
        bot_id="alpha",
        signal_id="entry-sig-002",
        side="BUY",
        symbol="MNQ",
        qty=1.0,
        fill_price=18_000.0,
        fill_ts="2026-05-06T11:00:00+00:00",
        paper=True,
        note="entry",
    )

    # No reduce_only kwarg — defaults to False (entry semantics).
    router._write_pending_order(bot, rec)

    payload = json.loads((tmp_path / "alpha.pending_order.json").read_text(encoding="utf-8"))
    assert payload["reduce_only"] is False


# ---------------------------------------------------------------------------
# 3. broker_router forwards reduce_only to the venue OrderRequest
# ---------------------------------------------------------------------------


class _CapturingVenue:
    """Minimal venue stand-in. Captures place_order calls.

    Mirrors test_broker_router.py:_FakeVenue but without the ``raises``
    machinery — we only need the captured request for assertion.
    """

    name = "alpaca"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def place_order(self, request: Any) -> Any:
        from eta_engine.venues.base import OrderResult, OrderStatus

        self.calls.append(request)
        return OrderResult(
            order_id="CAPTURED",
            status=OrderStatus.FILLED,
            filled_qty=request.qty,
            avg_price=float(request.price or 0.0),
        )


class _FakeSmartRouter:
    def __init__(self, venue: _CapturingVenue) -> None:
        self._venue = venue

    def choose_venue(
        self,
        symbol: str,
        qty: float,
        urgency: str = "normal",
    ) -> _CapturingVenue:
        return self._venue


class _AllowGateChain:
    def __call__(self, **kwargs: Any) -> tuple[bool, list[Any]]:
        return (True, [])

    def evaluate(self, **kwargs: Any) -> tuple[bool, list[Any]]:
        return (True, [])


class _NoopJournal:
    def record(self, **kwargs: Any) -> None:
        return None

    def append(self, event: Any) -> Any:
        return event


def test_broker_router_forwards_reduce_only_to_venue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exit pending file with reduce_only=True must reach the venue
    as an OrderRequest with reduce_only=True. The chain is:

        pending JSON (reduce_only=True)
            -> parse_pending_file -> PendingOrder(reduce_only=True)
            -> broker_router._process_pending_file
            -> OrderRequest(reduce_only=True)
            -> venue.place_order

    Any link that drops the flag = silent double-position bug.
    """
    from eta_engine import scripts as _scripts  # noqa: F401
    from eta_engine.scripts import broker_router as _br

    pending_dir = tmp_path / "pending"
    state_root = tmp_path / "state"
    pending_dir.mkdir(parents=True, exist_ok=True)

    # Write an EXIT pending file. Use a fresh timestamp so the sanity
    # gate's stale-pending-order check (default 900s) doesn't block this
    # before broker_router has a chance to build the OrderRequest.
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "signal_id": "exit-sig-routed",
        "side": "SELL",
        "qty": 0.05,
        "symbol": "BTC",
        "limit_price": 81_900.0,
        "stop_price": None,
        "target_price": None,
        "reduce_only": True,
    }
    pending_path = pending_dir / "btc_optimized.pending_order.json"
    pending_path.write_text(json.dumps(payload), encoding="utf-8")

    venue = _CapturingVenue()
    smart_router = _FakeSmartRouter(venue)
    journal = _NoopJournal()
    gate_chain = _AllowGateChain()

    # Position-reconciler stub — broker_router queries this before
    # gate-chain evaluation; without a stub it would try to hit IBKR.
    def _stub_fetch_positions(*_a: Any, **_kw: Any) -> dict[str, dict[str, float]]:
        return {}

    monkeypatch.setattr(
        "eta_engine.obs.position_reconciler.fetch_bot_positions",
        _stub_fetch_positions,
        raising=False,
    )
    monkeypatch.setattr(
        _br,
        "fetch_bot_positions",
        _stub_fetch_positions,
        raising=False,
    )

    router = _br.BrokerRouter(
        pending_dir=pending_dir,
        state_root=state_root,
        smart_router=smart_router,
        journal=journal,
        interval_s=5,
        dry_run=False,
        max_retries=3,
        gate_chain=gate_chain,
    )

    asyncio.run(router._process_pending_file(pending_path))

    assert len(venue.calls) == 1, f"expected exactly 1 venue call, got {len(venue.calls)}"
    request = venue.calls[0]
    assert request.reduce_only is True, (
        f"expected reduce_only=True on OrderRequest, got "
        f"reduce_only={request.reduce_only!r}; broker_router dropped the flag "
        f"between pending JSON and the venue — silent double-position bug"
    )
    # Sanity: the rest of the request still made it across.
    assert request.qty == 0.05
    assert request.client_order_id == "exit-sig-routed"


def test_broker_router_entry_default_reduce_only_is_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry pending file (no reduce_only field) lands at the venue
    with reduce_only=False. Pins the historical contract — adding the
    new field must not flip entries to exits.
    """
    from eta_engine.scripts import broker_router as _br

    pending_dir = tmp_path / "pending"
    state_root = tmp_path / "state"
    pending_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "signal_id": "entry-sig-routed",
        "side": "BUY",
        "qty": 1.0,
        "symbol": "MNQ",
        "limit_price": 18_000.0,
        "stop_price": 17_900.0,
        "target_price": 18_100.0,
        # reduce_only intentionally absent.
    }
    pending_path = pending_dir / "alpha.pending_order.json"
    pending_path.write_text(json.dumps(payload), encoding="utf-8")

    venue = _CapturingVenue()
    smart_router = _FakeSmartRouter(venue)
    journal = _NoopJournal()
    gate_chain = _AllowGateChain()

    def _stub_fetch_positions(*_a: Any, **_kw: Any) -> dict[str, dict[str, float]]:
        return {}

    monkeypatch.setattr(
        "eta_engine.obs.position_reconciler.fetch_bot_positions",
        _stub_fetch_positions,
        raising=False,
    )
    monkeypatch.setattr(
        _br,
        "fetch_bot_positions",
        _stub_fetch_positions,
        raising=False,
    )

    router = _br.BrokerRouter(
        pending_dir=pending_dir,
        state_root=state_root,
        smart_router=smart_router,
        journal=journal,
        interval_s=5,
        dry_run=False,
        max_retries=3,
        gate_chain=gate_chain,
    )

    asyncio.run(router._process_pending_file(pending_path))

    assert len(venue.calls) == 1
    assert venue.calls[0].reduce_only is False


# ---------------------------------------------------------------------------
# 4. Alpaca venue: reduce_only crypto orders skip the bracket
# ---------------------------------------------------------------------------


def test_alpaca_reduce_only_crypto_order_skips_bracket() -> None:
    """Pin the existing alpaca-crypto contract: a reduce_only=True
    crypto order MUST NOT carry an order_class=bracket payload.

    This invariant lives at TWO points in alpaca.py:
      1. `if not request.reduce_only:` skips the naked-entry rejection
         (exits don't need brackets — they bypass the check).
      2. The bracket-attach block is gated on
         `not request.reduce_only and ... and not is_crypto`.

    Both branches must hold for SUPERVISOR_LOCAL crypto exits to work.
    Already enforced by test_alpaca_venue.py:test_build_payload_no_bracket_for_reduce_only_exit
    for SELL exits; we add a BUY-side cover here so a SHORT-position
    BUY-back exit also stays bracket-free.
    """
    from eta_engine.venues import (
        AlpacaConfig,
        AlpacaVenue,
        OrderRequest,
        OrderType,
        Side,
    )

    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    # BUY-back exit on a short BTC position. Stop/target pre-populated
    # on the request to verify the bracket-skip is not a side effect of
    # missing fields — even with full bracket geometry, reduce_only
    # MUST drop the bracket attach.
    req = OrderRequest(
        symbol="BTC",
        side=Side.BUY,
        qty=0.001,
        order_type=OrderType.MARKET,
        stop_price=82_000.0,
        target_price=79_000.0,
        reduce_only=True,
        client_order_id="exit-buyback",
    )

    payload = venue.build_order_payload(req)

    assert "order_class" not in payload, f"reduce_only crypto BUY-back must not attach order_class; got {payload!r}"
    assert "take_profit" not in payload
    assert "stop_loss" not in payload
    # Sanity: side / qty / symbol survive untouched.
    assert payload["side"] == "buy"
    assert payload["symbol"] == "BTC/USD"
