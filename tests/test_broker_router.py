"""EVOLUTIONARY TRADING ALGO  //  tests.test_broker_router.

Comprehensive contract + lifecycle tests for the broker-router service
(``eta_engine.scripts.broker_router``). The module is being implemented
in parallel; these tests are written against the published API contract
and use mocks for the venue, smart-router, gate-chain, journal, and
position-reconciliation surfaces. The implementation may not exist yet
when collection runs — that's why the module import is wrapped in a
``pytest.importorskip`` so the rest of the suite stays green.

Design rules (per task brief)
-----------------------------
* pytest only — no pytest-asyncio. Async router methods are driven via
  ``asyncio.run(...)``.
* Every filesystem fixture goes through ``tmp_path``. We never write
  inside the real workspace.
* Every env-var manipulation goes through ``monkeypatch``.
* Venue, journal, gate-chain, smart-router, and ``fetch_bot_positions``
  are stubbed with tiny stand-ins that record their inputs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# The module is implemented in parallel — skip if it hasn't landed yet
# rather than poison collection.
broker_router = pytest.importorskip(
    "eta_engine.scripts.broker_router",
    reason="broker_router module not yet implemented",
)

from eta_engine.venues.base import (  # noqa: E402  (after importorskip)
    OrderRequest,
    OrderResult,
    OrderStatus,
)


# ---------------------------------------------------------------------------
# Tiny stand-ins
# ---------------------------------------------------------------------------


class _FakeVenue:
    """Minimal venue stand-in. Captures place_order calls + returns
    a canned sequence of ``OrderResult`` (or raises configured exceptions).
    """

    name = "fake"

    def __init__(
        self,
        results: list[OrderResult | Exception] | None = None,
    ) -> None:
        self._results: list[OrderResult | Exception] = list(results or [])
        self.calls: list[OrderRequest] = []

    async def place_order(self, request: OrderRequest) -> OrderResult:
        self.calls.append(request)
        if not self._results:
            return OrderResult(
                order_id="FAKE-DEFAULT",
                status=OrderStatus.FILLED,
                filled_qty=request.qty,
                avg_price=float(request.price or 0.0),
            )
        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _FakeSmartRouter:
    """Captures choose_venue() calls and hands back a configurable venue."""

    def __init__(self, venue: _FakeVenue) -> None:
        self._venue = venue
        self.calls: list[tuple[str, float, str]] = []

    def choose_venue(
        self,
        symbol: str,
        qty: float,
        urgency: str = "normal",
    ) -> _FakeVenue:
        self.calls.append((symbol, qty, urgency))
        return self._venue


class _FakeJournalEvent:
    """Mimics JournalEvent enough for the router to round-trip metadata."""

    def __init__(
        self,
        *,
        actor: Any = None,
        intent: str = "",
        outcome: Any = None,
        rationale: str = "",
        gate_checks: list[str] | None = None,
        links: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.actor = actor
        self.intent = intent
        self.outcome = outcome
        self.rationale = rationale
        self.gate_checks = list(gate_checks or [])
        self.links = list(links or [])
        self.metadata = dict(metadata or {})
        self.ts = datetime.now(UTC)


class _FakeJournal:
    """Collecting journal stand-in. ``record(...)`` and ``append(evt)``
    both supported because the router contract isn't pinned to one shape.
    """

    def __init__(self) -> None:
        self.events: list[_FakeJournalEvent] = []

    def record(self, **kwargs: Any) -> _FakeJournalEvent:
        evt = _FakeJournalEvent(**kwargs)
        self.events.append(evt)
        return evt

    def append(self, event: Any) -> Any:
        # Accept either a real JournalEvent or our local fake. We never
        # introspect attributes that aren't on both surfaces.
        self.events.append(event)
        return event

    # Convenience for assertions
    def outcomes(self) -> list[Any]:
        out: list[Any] = []
        for e in self.events:
            outcome = getattr(e, "outcome", None)
            out.append(getattr(outcome, "value", outcome))
        return out

    def intents(self) -> list[str]:
        return [getattr(e, "intent", "") for e in self.events]


class _FakeGateChain:
    """Callable gate-chain stand-in.

    The router contract says gate evaluation returns
    ``(allow: bool, results: list[GateResult])``. We model that with a
    callable that returns the configured pair and records what inputs it
    was given.
    """

    def __init__(
        self,
        result: tuple[bool, list[Any]],
    ) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[bool, list[Any]]:
        self.calls.append(dict(kwargs))
        return self._result

    # Some implementations may expose `.evaluate(...)` instead — provide
    # the alias so tests don't lock the router into one binding shape.
    def evaluate(self, **kwargs: Any) -> tuple[bool, list[Any]]:
        return self.__call__(**kwargs)


class _FakeGateResult:
    """Model the GateResult dataclass enough for assertions to pass.

    Real shape per contract: ``GateResult(allow=False, gate=str, reason=str)``.
    """

    def __init__(self, *, allow: bool, gate: str, reason: str = "") -> None:
        self.allow = allow
        self.gate = gate
        self.reason = reason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pending(
    pending_dir: Path,
    *,
    bot_id: str = "alpha",
    signal_id: str = "sig-001",
    side: str = "BUY",
    qty: float = 1.0,
    symbol: str = "MNQ",
    limit_price: float = 25_000.0,
    ts: str | None = None,
    raw_text: str | None = None,
    suffix: str = ".pending_order.json",
) -> Path:
    """Drop a pending-order JSON file into ``pending_dir``.

    Pass ``raw_text`` to override JSON serialization (for malformed-JSON
    tests). Pass ``suffix`` to control the filename suffix.
    """
    pending_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{bot_id}{suffix}"
    path = pending_dir / fname
    if raw_text is not None:
        path.write_text(raw_text, encoding="utf-8")
        return path
    payload = {
        "ts": ts or datetime.now(UTC).isoformat(),
        "signal_id": signal_id,
        "side": side,
        "qty": qty,
        "symbol": symbol,
        "limit_price": limit_price,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_router(
    *,
    pending_dir: Path,
    state_root: Path,
    smart_router: _FakeSmartRouter,
    journal: _FakeJournal,
    gate_chain: _FakeGateChain | None = None,
    dry_run: bool = False,
    max_retries: int = 3,
    interval_s: int = 5,
) -> Any:
    """Construct a BrokerRouter. Tries the documented kwargs first; if
    the implementation tightens the constructor we surface a clean error."""
    kwargs: dict[str, Any] = {
        "pending_dir": pending_dir,
        "state_root": state_root,
        "smart_router": smart_router,
        "journal": journal,
        "interval_s": interval_s,
        "dry_run": dry_run,
        "max_retries": max_retries,
    }
    if gate_chain is not None:
        kwargs["gate_chain"] = gate_chain
    try:
        return broker_router.BrokerRouter(**kwargs)
    except TypeError:
        # Fallback: implementation may inject gate_chain via attribute
        # rather than a constructor kwarg. Build then patch.
        kwargs.pop("gate_chain", None)
        router = broker_router.BrokerRouter(**kwargs)
        if gate_chain is not None:
            router.gate_chain = gate_chain  # type: ignore[attr-defined]
        return router


def _today_archive_dir(state_root: Path) -> Path:
    """Best-effort guess for the day's archive directory."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return state_root / "archive" / today


def _find_under(root: Path, name: str) -> Path | None:
    """Search ``root`` recursively for the first file with this name."""
    if not root.exists():
        return None
    matches = list(root.rglob(name))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestParsePendingFile:
    def test_parse_pending_file_valid(self, tmp_path: Path) -> None:
        path = _write_pending(tmp_path, bot_id="alpha")
        order = broker_router.parse_pending_file(path)
        assert order.signal_id == "sig-001"
        assert order.side == "BUY"
        assert order.qty == 1.0
        assert order.symbol == "MNQ"
        assert order.limit_price == 25_000.0

    def test_parse_pending_file_extracts_bot_id_from_filename(
        self, tmp_path: Path
    ) -> None:
        path = _write_pending(tmp_path, bot_id="btc_optimized")
        order = broker_router.parse_pending_file(path)
        assert order.bot_id == "btc_optimized"

    def test_parse_pending_file_malformed_json_raises_value_error(
        self, tmp_path: Path
    ) -> None:
        path = _write_pending(tmp_path, raw_text="{not json")
        with pytest.raises((ValueError, json.JSONDecodeError)):
            broker_router.parse_pending_file(path)

    def test_parse_pending_file_missing_required_field_raises(
        self, tmp_path: Path
    ) -> None:
        # Drop signal_id -> implementation should refuse the row
        bad = {
            "ts": datetime.now(UTC).isoformat(),
            "side": "BUY",
            "qty": 1.0,
            "symbol": "MNQ",
            "limit_price": 25_000.0,
        }
        path = tmp_path / "alpha.pending_order.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(Exception):
            broker_router.parse_pending_file(path)

    def test_parse_pending_file_invalid_side_raises(self, tmp_path: Path) -> None:
        path = _write_pending(tmp_path, side="HOLD")
        with pytest.raises(Exception):
            broker_router.parse_pending_file(path)


# ---------------------------------------------------------------------------
# Symbol normalization
# ---------------------------------------------------------------------------


class TestNormalizeSymbol:
    def test_normalize_symbol_btc_to_ibkr(self) -> None:
        assert broker_router.normalize_symbol("BTC", "ibkr") == "BTCUSD"

    def test_normalize_symbol_btc_to_tastytrade(self) -> None:
        assert broker_router.normalize_symbol("BTC", "tasty") == "BTCUSDT"

    def test_normalize_symbol_eth_to_ibkr(self) -> None:
        assert broker_router.normalize_symbol("ETH", "ibkr") == "ETHUSD"

    def test_normalize_symbol_sol_to_ibkr(self) -> None:
        assert broker_router.normalize_symbol("SOL", "ibkr") == "SOLUSD"

    def test_normalize_symbol_mnq_to_ibkr(self) -> None:
        # Futures roots pass through unchanged.
        assert broker_router.normalize_symbol("MNQ", "ibkr") == "MNQ"

    def test_normalize_symbol_unknown_pair_raises(self) -> None:
        with pytest.raises(Exception):
            broker_router.normalize_symbol("XYZ", "ibkr")


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


def _stub_fetch_positions(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, dict[str, float]] | Exception,
) -> None:
    """Patch every plausible binding of fetch_bot_positions."""

    def _impl(*_args: Any, **_kwargs: Any) -> dict[str, dict[str, float]]:
        if isinstance(payload, Exception):
            raise payload
        return payload

    # Patch the original location
    monkeypatch.setattr(
        "eta_engine.obs.position_reconciler.fetch_bot_positions",
        _impl,
        raising=False,
    )
    # Patch any re-export the router may have grabbed
    monkeypatch.setattr(
        broker_router,
        "fetch_bot_positions",
        _impl,
        raising=False,
    )


def _allow_gate_chain() -> _FakeGateChain:
    return _FakeGateChain(result=(True, []))


def _block_gate_chain(
    *, gate: str = "heartbeat", reason: str = "heartbeat_stale"
) -> _FakeGateChain:
    return _FakeGateChain(
        result=(False, [_FakeGateResult(allow=False, gate=gate, reason=reason)])
    )


class TestLifecycle:
    def test_happy_path_filled_archives_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-001", bot_id="alpha")

        venue = _FakeVenue(
            results=[
                OrderResult(
                    order_id="OID-001",
                    status=OrderStatus.FILLED,
                    filled_qty=1.0,
                    avg_price=25_000.0,
                ),
            ]
        )
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )

        asyncio.run(router._process_pending_file(path))

        # File no longer in pending
        assert not path.exists()
        # File ended up in today's archive
        archived = _find_under(state_root / "archive", path.name)
        assert archived is not None, (
            f"expected archived file under {state_root / 'archive'!s}"
        )
        # Sidecar fill_result exists
        sidecar = _find_under(state_root, "sig-001_result.json")
        assert sidecar is not None
        # Venue called exactly once
        assert len(venue.calls) == 1
        req = venue.calls[0]
        assert req.symbol  # populated
        assert req.qty == 1.0
        # Journal got an EXECUTED-class event
        outcomes = journal.outcomes()
        assert any(str(o).upper() == "EXECUTED" for o in outcomes), (
            f"expected EXECUTED in journal outcomes, got {outcomes!r}"
        )

    def test_gate_blocked_moves_to_blocked_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _block_gate_chain(gate="heartbeat", reason="heartbeat_stale")
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        # File ended up in blocked/
        blocked_dir = state_root / "blocked"
        blocked_file = _find_under(blocked_dir, path.name)
        assert blocked_file is not None
        # Block-meta sidecar exists with gate + reason
        sidecar_files = list(blocked_dir.rglob("*"))
        meta_text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sidecar_files
            if p.is_file() and p.suffix == ".json"
        )
        assert "heartbeat" in meta_text
        assert "heartbeat_stale" in meta_text
        # Journal got a BLOCKED-class event
        outcomes = journal.outcomes()
        assert any(str(o).upper() == "BLOCKED" for o in outcomes), (
            f"expected BLOCKED in journal outcomes, got {outcomes!r}"
        )
        # Venue NEVER called
        assert venue.calls == []

    def test_malformed_json_quarantined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, raw_text="{not json}")

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        # File ended up in quarantine/
        quarantined = _find_under(state_root / "quarantine", path.name)
        assert quarantined is not None
        # Journal got a NOTED event whose intent flags quarantine
        intents = journal.intents()
        assert any("quarantine" in i.lower() for i in intents), (
            f"expected quarantine-flagged intent, got {intents!r}"
        )
        outcomes = journal.outcomes()
        assert any(str(o).upper() == "NOTED" for o in outcomes), (
            f"expected NOTED in journal outcomes, got {outcomes!r}"
        )
        # Venue NEVER called
        assert venue.calls == []

    def test_venue_rejected_retries_then_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        # Three rejections in a row -> exhausts max_retries=3
        rejected = OrderResult(
            order_id="OID-REJ",
            status=OrderStatus.REJECTED,
            filled_qty=0.0,
        )
        venue = _FakeVenue(results=[rejected, rejected, rejected])
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
            max_retries=3,
        )
        asyncio.run(router._process_pending_file(path))

        failed = _find_under(state_root / "failed", path.name)
        assert failed is not None
        # Journal got 3 FAILED events
        failed_count = sum(1 for o in journal.outcomes() if str(o).upper() == "FAILED")
        assert failed_count >= 3, (
            f"expected >=3 FAILED events, got {failed_count} (outcomes={journal.outcomes()!r})"
        )

    def test_dry_run_does_not_move_or_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
            dry_run=True,
        )
        asyncio.run(router._process_pending_file(path))

        # File is STILL in pending_dir
        assert path.exists(), "dry_run should leave pending file in place"
        # No archive/blocked dirs created (or they exist but are empty)
        for d in ("archive", "blocked", "failed", "quarantine"):
            sub = state_root / d
            if sub.exists():
                contents = list(sub.rglob("*"))
                files = [p for p in contents if p.is_file()]
                assert files == [], (
                    f"dry_run should not produce files under {sub!s}; found {files!r}"
                )
        # Venue NEVER called
        assert venue.calls == []

    def test_partial_fill_archives_with_status_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-partial")

        venue = _FakeVenue(
            results=[
                OrderResult(
                    order_id="OID-PARTIAL",
                    status=OrderStatus.PARTIAL,
                    filled_qty=0.5,
                    avg_price=25_000.0,
                ),
            ]
        )
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        archived = _find_under(state_root / "archive", path.name)
        assert archived is not None
        sidecar = _find_under(state_root, "sig-partial_result.json")
        assert sidecar is not None
        body = sidecar.read_text(encoding="utf-8")
        assert "PARTIAL" in body

    def test_open_status_archives_with_status_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-open")

        venue = _FakeVenue(
            results=[
                OrderResult(
                    order_id="OID-OPEN",
                    status=OrderStatus.OPEN,
                    filled_qty=0.0,
                ),
            ]
        )
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        archived = _find_under(state_root / "archive", path.name)
        assert archived is not None
        sidecar = _find_under(state_root, "sig-open_result.json")
        assert sidecar is not None
        body = sidecar.read_text(encoding="utf-8")
        assert "OPEN" in body

    def test_atomic_move_collision_skips_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, bot_id="alpha")

        # Pre-create the destination in processing/
        processing_dir = state_root / "processing"
        processing_dir.mkdir(parents=True, exist_ok=True)
        (processing_dir / path.name).write_text(
            "{}", encoding="utf-8"
        )

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        # Should NOT raise — router should detect collision and skip
        try:
            asyncio.run(router._process_pending_file(path))
        except Exception as exc:  # pragma: no cover — explicit-fail msg
            pytest.fail(
                f"router crashed on processing/ filename collision: {exc!r}"
            )

    def test_heartbeat_emitted_each_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two tick-iterations should both refresh the heartbeat file."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )

        # Drive two loop iterations directly via private _tick if exposed,
        # otherwise fall back to invoking the documented heartbeat writer.
        async def _two_ticks() -> None:
            tick_fn = getattr(router, "_tick", None) or getattr(
                router, "_run_once", None
            )
            heartbeat_fn = getattr(router, "_emit_heartbeat", None) or getattr(
                router, "_write_heartbeat", None
            )
            if tick_fn is not None:
                # Documented: each iteration scans pending + emits heartbeat.
                if asyncio.iscoroutinefunction(tick_fn):
                    await tick_fn()
                    await tick_fn()
                else:
                    tick_fn()
                    tick_fn()
            elif heartbeat_fn is not None:
                if asyncio.iscoroutinefunction(heartbeat_fn):
                    await heartbeat_fn()
                    await heartbeat_fn()
                else:
                    heartbeat_fn()
                    heartbeat_fn()
            else:  # pragma: no cover  — flagged in coverage report
                pytest.skip(
                    "router exposes neither _tick/_run_once nor _emit_heartbeat; "
                    "implementation must clarify heartbeat hook for this test"
                )

        asyncio.run(_two_ticks())

        hb_path = _find_under(state_root, "broker_router_heartbeat.json")
        assert hb_path is not None, (
            f"expected broker_router_heartbeat.json under {state_root!s}"
        )
        body = json.loads(hb_path.read_text(encoding="utf-8"))
        assert "last_poll_ts" in body, (
            f"heartbeat payload missing last_poll_ts; got keys={list(body)!r}"
        )

    def test_unrecoverable_processing_exception_does_not_kill_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        # Force parse_pending_file to raise an unexpected error.
        def _boom(_p: Path) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(broker_router, "parse_pending_file", _boom)

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        # Must NOT raise out of _process_pending_file
        try:
            asyncio.run(router._process_pending_file(path))
        except Exception as exc:  # pragma: no cover
            pytest.fail(
                f"_process_pending_file leaked unexpected RuntimeError: {exc!r}"
            )


# ---------------------------------------------------------------------------
# Position-reconciliation interaction
# ---------------------------------------------------------------------------


class TestPositionReconciliation:
    def test_open_positions_from_fetch_bot_positions_used_for_gate_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        positions: dict[str, dict[str, float]] = {"MNQ": {"alpha": 2}}
        _stub_fetch_positions(monkeypatch, positions)

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        assert gates.calls, "gate-chain was never invoked"
        first = gates.calls[0]
        # Implementation may name it `open_positions` or `positions`; accept either.
        seen = first.get("open_positions") or first.get("positions")
        assert seen == positions, (
            f"gate chain received {seen!r}, expected {positions!r}"
        )

    def test_reconcile_disabled_falls_through_to_empty_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        monkeypatch.setenv("ETA_RECONCILE_DISABLED", "1")
        # Even if positions exist, the disabled flag forces {}
        _stub_fetch_positions(monkeypatch, {"MNQ": {"alpha": 2}})

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        assert gates.calls, "gate chain not called"
        seen = (
            gates.calls[0].get("open_positions")
            or gates.calls[0].get("positions")
            or {}
        )
        assert seen == {}, (
            f"with ETA_RECONCILE_DISABLED=1 expected empty positions; got {seen!r}"
        )

    def test_not_implemented_with_allow_empty_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        # NotImplementedError + allow-empty-state -> proceed with {}
        _stub_fetch_positions(monkeypatch, NotImplementedError("not impl"))
        monkeypatch.setenv("ETA_RECONCILE_ALLOW_EMPTY_STATE", "1")

        venue = _FakeVenue(
            results=[
                OrderResult(
                    order_id="OID-OK",
                    status=OrderStatus.FILLED,
                    filled_qty=1.0,
                ),
            ]
        )
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        # Should have proceeded, gate received {}
        assert gates.calls, "gate chain not called"
        seen = (
            gates.calls[0].get("open_positions")
            or gates.calls[0].get("positions")
            or {}
        )
        assert seen == {}
        # Venue did get called (no abort)
        assert len(venue.calls) == 1

    def test_not_implemented_without_allow_empty_state_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir)

        _stub_fetch_positions(monkeypatch, NotImplementedError("not impl"))
        monkeypatch.delenv("ETA_RECONCILE_ALLOW_EMPTY_STATE", raising=False)
        monkeypatch.delenv("ETA_RECONCILE_DISABLED", raising=False)

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        # Process should not crash, but should record a FAILED journal entry
        asyncio.run(router._process_pending_file(path))

        outcomes = journal.outcomes()
        assert any(str(o).upper() == "FAILED" for o in outcomes), (
            f"expected FAILED outcome when reconciliation aborts; got {outcomes!r}"
        )
        # Venue NEVER reached
        assert venue.calls == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_signal_id_passed_as_client_order_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-CLIENT-001")

        venue = _FakeVenue(
            results=[
                OrderResult(
                    order_id="OID",
                    status=OrderStatus.FILLED,
                    filled_qty=1.0,
                ),
            ]
        )
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        asyncio.run(router._process_pending_file(path))

        assert len(venue.calls) == 1
        req = venue.calls[0]
        assert req.client_order_id == "sig-CLIENT-001", (
            f"expected client_order_id == signal_id; got {req.client_order_id!r}"
        )

    def test_already_archived_file_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, bot_id="alpha", signal_id="sig-arch")

        # Pre-create the archive entry for today so the destination collides.
        archive_today = _today_archive_dir(state_root)
        archive_today.mkdir(parents=True, exist_ok=True)
        prior = archive_today / path.name
        prior.write_text("{}", encoding="utf-8")

        venue = _FakeVenue(
            results=[
                OrderResult(
                    order_id="OID-ARCH",
                    status=OrderStatus.FILLED,
                    filled_qty=1.0,
                ),
            ]
        )
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=gates,
        )
        # Should not crash; outcome (skip vs collision-rename) is up to
        # the implementation, but the loop must remain alive.
        try:
            asyncio.run(router._process_pending_file(path))
        except Exception as exc:  # pragma: no cover
            pytest.fail(
                f"router crashed on pre-existing archive collision: {exc!r}"
            )
