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


def test_register_task_uses_eta_engine_pending_inbox() -> None:
    """The scheduled task must poll the same inbox the supervisor writes."""
    script = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "scripts"
        / "register_broker_router_task.ps1"
    )
    text = script.read_text(encoding="utf-8")
    assert (
        r'"ETA_BROKER_ROUTER_PENDING_DIR" = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\router\pending"'
        in text
    )
    assert (
        r'"ETA_BROKER_ROUTER_PENDING_DIR" = "C:\EvolutionaryTradingAlgo\docs\btc_live\broker_fleet"'
        not in text
    )
    assert r"docs\btc_live\broker_fleet" not in text


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
    stop_price: float | None = 24_900.0,
    target_price: float | None = 25_100.0,
    include_brackets: bool = True,
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
    if include_brackets:
        payload["stop_price"] = stop_price
        payload["target_price"] = target_price
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
    order_hold_path: Path | None = None,
) -> Any:
    """Construct a BrokerRouter. The gate_chain override is a constructor
    kwarg: ``BrokerRouter(..., gate_chain=callable)``. The router calls
    the override directly with ``open_positions``, ``new_symbol``, and
    ``new_qty`` kwargs and expects ``(allow, [GateResult-shaped, ...])``
    back, matching the production ``mnq.risk.gate_chain.build_default_chain``
    contract."""
    return broker_router.BrokerRouter(
        pending_dir=pending_dir,
        state_root=state_root,
        smart_router=smart_router,
        journal=journal,
        interval_s=interval_s,
        dry_run=dry_run,
        max_retries=max_retries,
        gate_chain=gate_chain,
        order_hold_path=order_hold_path,
    )


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
        with pytest.raises(ValueError):
            broker_router.parse_pending_file(path)

    def test_parse_pending_file_invalid_side_raises(self, tmp_path: Path) -> None:
        path = _write_pending(tmp_path, side="HOLD")
        with pytest.raises(ValueError):
            broker_router.parse_pending_file(path)

    def test_parse_pending_file_with_brackets(self, tmp_path: Path) -> None:
        """Bracket fields parse into PendingOrder.stop_price/target_price."""
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "signal_id": "sig-bracket-001",
            "side": "BUY",
            "qty": 1.0,
            "symbol": "MNQ",
            "limit_price": 18_000.0,
            "stop_price": 17_900.0,
            "target_price": 18_100.0,
        }
        path = tmp_path / "alpha.pending_order.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        order = broker_router.parse_pending_file(path)
        assert order.stop_price == 17_900.0
        assert order.target_price == 18_100.0
        # Sanity: required fields still parse correctly alongside.
        assert order.signal_id == "sig-bracket-001"
        assert order.limit_price == 18_000.0

    def test_parse_pending_file_without_brackets_returns_none(
        self, tmp_path: Path
    ) -> None:
        """Back-compat: older files without brackets parse with None brackets.

        The venue's bracket-required check (downstream of parse) is what
        actually rejects naked entries; the parser stays permissive so
        files written before the schema change still load.
        """
        path = _write_pending(tmp_path, bot_id="legacy", include_brackets=False)
        order = broker_router.parse_pending_file(path)
        assert order.stop_price is None
        assert order.target_price is None

    def test_parse_pending_file_non_numeric_bracket_raises(
        self, tmp_path: Path
    ) -> None:
        """Garbage bracket values fail parse rather than silently None-coercing."""
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "signal_id": "sig-bad-bracket",
            "side": "BUY",
            "qty": 1.0,
            "symbol": "MNQ",
            "limit_price": 18_000.0,
            "stop_price": "not-a-number",
            "target_price": 18_100.0,
        }
        path = tmp_path / "alpha.pending_order.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
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
        with pytest.raises(ValueError):
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
    def test_order_entry_hold_leaves_pending_file_unsubmitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operator hold is a fail-closed runtime brake before any venue call."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-held", bot_id="alpha")
        hold_path = tmp_path / "order_entry_hold.json"
        hold_path.write_text(
            json.dumps({"active": True, "reason": "manual_flatten"}),
            encoding="utf-8",
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
            order_hold_path=hold_path,
        )

        asyncio.run(router._process_pending_file(path))

        assert path.exists()
        assert venue.calls == []
        assert router._counts["held"] == 1

    def test_run_once_heartbeat_surfaces_order_entry_hold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-held", bot_id="alpha")
        hold_path = tmp_path / "order_entry_hold.json"
        hold_path.write_text(
            json.dumps({"active": True, "reason": "broker_incident"}),
            encoding="utf-8",
        )

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        _stub_fetch_positions(monkeypatch, {})
        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=_allow_gate_chain(),
            order_hold_path=hold_path,
        )

        asyncio.run(router.run_once())

        assert path.exists()
        assert venue.calls == []
        hb = json.loads((state_root / "broker_router_heartbeat.json").read_text())
        assert hb["order_entry_hold"]["active"] is True
        assert hb["order_entry_hold"]["reason"] == "broker_incident"

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

    def test_lifecycle_with_brackets_passes_through_to_order_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop/target on the pending file land on the OrderRequest verbatim.

        This is the closing half of the bracket-passthrough fix: the
        supervisor writes brackets into the JSON, the router parses
        them into PendingOrder, and then the OrderRequest the venue
        actually sees carries them. Without this passthrough the venue
        would reject every entry with a "missing bracket" error.
        """
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        pending_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "signal_id": "sig-bracket-passthrough",
            "side": "BUY",
            "qty": 1.0,
            "symbol": "MNQ",
            "limit_price": 18_000.0,
            "stop_price": 17_900.0,
            "target_price": 18_100.0,
        }
        path = pending_dir / "alpha.pending_order.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        venue = _FakeVenue(
            results=[
                OrderResult(
                    order_id="OID-BRK",
                    status=OrderStatus.FILLED,
                    filled_qty=1.0,
                    avg_price=18_000.0,
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
        assert req.stop_price == 17_900.0
        assert req.target_price == 18_100.0
        # Required-field passthrough still intact.
        assert req.qty == 1.0
        assert req.price == 18_000.0
        assert req.client_order_id == "sig-bracket-passthrough"

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

    def test_pending_order_sanity_blocks_smoke_without_venue_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stale/smoke broker-intent files are fail-closed before venue routing."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(
            pending_dir,
            bot_id="btc_optimized",
            signal_id="btc_optimized_smoke17",
            symbol="BTC",
            limit_price=1.0,
            include_brackets=False,
        )

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        _stub_fetch_positions(monkeypatch, {})
        router = _make_router(
            pending_dir=pending_dir,
            state_root=state_root,
            smart_router=smart_router,
            journal=journal,
            gate_chain=_allow_gate_chain(),
        )

        asyncio.run(router._process_pending_file(path))

        blocked_file = _find_under(state_root / "blocked", path.name)
        assert blocked_file is not None
        assert venue.calls == []
        meta_text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (state_root / "blocked").rglob("*_block.json")
        )
        assert "pending_order_sanity" in meta_text
        assert "smoke" in meta_text

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
        """Three REJECTs across three ticks -> file ends in failed/.

        Pinned to the new retry behavior: each REJECT writes a sidecar
        ``<file>.retry_meta.json`` and leaves the file in processing/.
        Subsequent ticks read that sidecar and re-run the lifecycle. The
        backoff is suppressed for the test by stubbing
        ``broker_router.BrokerRouter._should_backoff`` to ``False`` so
        the test doesn't have to sleep through real exponential delays.
        """
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
        # Suppress exponential backoff so the test runs in milliseconds.
        monkeypatch.setattr(
            type(router), "_should_backoff", lambda *_a, **_k: False,
        )

        async def _drive() -> None:
            # Tick 1: pending -> processing, attempts=1, leave in processing/
            await router._tick()
            # Tick 2: retry scan picks it up, attempts=2, leave in processing/
            await router._tick()
            # Tick 3: retry scan picks it up, attempts=3 == max -> failed/
            await router._tick()

        asyncio.run(_drive())

        failed = _find_under(state_root / "failed", path.name)
        assert failed is not None, "expected file under state_root/failed/"
        # File should NOT still be in processing/.
        leftover = _find_under(state_root / "processing", path.name)
        assert leftover is None, "file lingered in processing/ after max_retries"
        # Three rejections -> 2 NOTED retry events + 1 FAILED terminal.
        failed_count = sum(1 for o in journal.outcomes() if str(o).upper() == "FAILED")
        assert failed_count >= 1, (
            f"expected at least 1 FAILED event, got outcomes={journal.outcomes()!r}"
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
        """Two ticks both refresh ``state_root/broker_router_heartbeat.json``.

        Pinned to the actual API: the router exposes both ``_tick``
        (async) and ``_emit_heartbeat`` (sync). The heartbeat path is
        ``state_root / "broker_router_heartbeat.json"`` and the payload
        contains ``last_poll_ts``.
        """
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

        async def _two_ticks() -> None:
            await router._tick()
            await router._tick()

        asyncio.run(_two_ticks())

        # Heartbeat path is pinned: ``state_root/broker_router_heartbeat.json``.
        hb_path = state_root / "broker_router_heartbeat.json"
        assert hb_path.exists(), f"expected heartbeat file at {hb_path!s}"
        # And the router exposes the property too.
        assert router.heartbeat_path == hb_path
        body = json.loads(hb_path.read_text(encoding="utf-8"))
        assert "last_poll_ts" in body, (
            f"heartbeat payload missing last_poll_ts; got keys={list(body)!r}"
        )

    def test_emit_heartbeat_writes_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct ``_emit_heartbeat()`` call writes the snapshot."""
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
        router._emit_heartbeat()

        hb_path = state_root / "broker_router_heartbeat.json"
        assert hb_path.exists()
        body = json.loads(hb_path.read_text(encoding="utf-8"))
        assert "last_poll_ts" in body
        assert "counts" in body

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
        # Production gate-chain contract is {symbol: net_qty}.
        seen = first.get("open_positions") or first.get("positions")
        assert seen == {"MNQ": 2}, (
            f"gate chain received {seen!r}, expected collapsed net positions"
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
        # Pinned behavior: ``_atomic_move`` uses ``os.replace`` which is
        # a silent overwrite-on-collision on both POSIX and Windows. The
        # router does not crash and the archive entry contains the new
        # fill_result (overwriting the prior empty stub).
        asyncio.run(router._process_pending_file(path))

        archived = _find_under(state_root / "archive", path.name)
        assert archived is not None, "expected the file to land in archive/"
        # The prior stub was ``{}``; after replace it should hold the
        # supervisor JSON we wrote (signal_id is preserved).
        body = archived.read_text(encoding="utf-8")
        assert "sig-arch" in body, (
            f"expected archive entry to be the new pending payload, got {body[:80]!r}"
        )
        # Venue was called; FILLED journaled.
        assert len(venue.calls) == 1
        outcomes = journal.outcomes()
        assert any(str(o).upper() == "EXECUTED" for o in outcomes)


# ---------------------------------------------------------------------------
# Retry-meta sidecar (Issue 1: orphaned retries)
# ---------------------------------------------------------------------------


class TestRetryMetaSidecar:
    def test_rejected_writes_retry_meta_in_processing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One REJECT writes ``<file>.retry_meta.json`` in processing/."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-meta-1")

        venue = _FakeVenue(results=[
            OrderResult(order_id="OID", status=OrderStatus.REJECTED, filled_qty=0.0),
        ])
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir, state_root=state_root,
            smart_router=smart_router, journal=journal,
            gate_chain=gates, max_retries=5,
        )
        asyncio.run(router._tick())

        processing_dir = state_root / "processing"
        proc_order = processing_dir / path.name
        assert proc_order.exists(), "order file should remain in processing/ after REJECT"
        meta_path = processing_dir / (path.name + ".retry_meta.json")
        assert meta_path.exists(), "expected retry_meta sidecar in processing/"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["attempts"] == 1
        assert meta["last_attempt_ts"]
        assert "last_reject_reason" in meta

    def test_retry_path_picked_up_on_next_tick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tick 2 re-runs the lifecycle for a file already in processing/."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-meta-2")

        venue = _FakeVenue(results=[
            OrderResult(order_id="OID", status=OrderStatus.REJECTED, filled_qty=0.0),
            OrderResult(order_id="OID", status=OrderStatus.FILLED, filled_qty=1.0),
        ])
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir, state_root=state_root,
            smart_router=smart_router, journal=journal,
            gate_chain=gates, max_retries=3,
        )
        monkeypatch.setattr(
            type(router), "_should_backoff", lambda *_a, **_k: False,
        )

        async def _drive() -> None:
            await router._tick()
            await router._tick()

        asyncio.run(_drive())
        assert len(venue.calls) == 2, "expected the second tick to retry the order"
        archived = _find_under(state_root / "archive", path.name)
        assert archived is not None, "filled order should be archived"


# ---------------------------------------------------------------------------
# Gate-chain import failure (Issue 2: fail-closed by default + bootstrap)
# ---------------------------------------------------------------------------


class TestGateChainImportFailure:
    def test_import_failure_blocks_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ImportError of gate_chain -> file lands in blocked/, NOT archive."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-block-imp")

        def _raise_import_error() -> Any:
            raise ImportError("simulated firm submodule missing")

        monkeypatch.setattr(
            broker_router, "_load_build_default_chain", _raise_import_error,
        )
        monkeypatch.delenv("ETA_GATE_BOOTSTRAP", raising=False)

        venue = _FakeVenue()
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir, state_root=state_root,
            smart_router=smart_router, journal=journal,
            gate_chain=None,  # production path
        )
        asyncio.run(router._process_pending_file(path))

        blocked_file = _find_under(state_root / "blocked", path.name)
        assert blocked_file is not None, "expected file under state_root/blocked/"
        assert _find_under(state_root / "archive", path.name) is None
        meta_files = list((state_root / "blocked").rglob("*_block.json"))
        assert meta_files, "expected a *_block.json sidecar"
        meta_text = meta_files[0].read_text(encoding="utf-8")
        assert "gate_chain_import_failed" in meta_text
        assert any(
            "gate_chain_import_failed" in i for i in journal.intents()
        ), f"intents={journal.intents()!r}"
        assert venue.calls == []

    def test_import_failure_with_bootstrap_allows_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ETA_GATE_BOOTSTRAP=1 -> ImportError logs ERROR but allows through."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(pending_dir, signal_id="sig-bootstrap")

        def _raise_import_error() -> Any:
            raise ImportError("simulated firm submodule missing")

        monkeypatch.setattr(
            broker_router, "_load_build_default_chain", _raise_import_error,
        )
        monkeypatch.setenv("ETA_GATE_BOOTSTRAP", "1")

        venue = _FakeVenue(results=[
            OrderResult(order_id="OID", status=OrderStatus.FILLED, filled_qty=1.0),
        ])
        smart_router = _FakeSmartRouter(venue)
        journal = _FakeJournal()
        _stub_fetch_positions(monkeypatch, {})

        router = _make_router(
            pending_dir=pending_dir, state_root=state_root,
            smart_router=smart_router, journal=journal,
            gate_chain=None,
        )
        asyncio.run(router._process_pending_file(path))

        assert len(venue.calls) == 1, "bootstrap should let the order through"
        archived = _find_under(state_root / "archive", path.name)
        assert archived is not None
        assert _find_under(state_root / "blocked", path.name) is None


# ---------------------------------------------------------------------------
# Per-bot routing config (Issue 3: scale to 52 bots without hardcoded heuristics)
# ---------------------------------------------------------------------------

_VALID_ROUTING_YAML = """\
version: 1
default:
  venue: ibkr
  symbol_overrides:
    BTC:  { ibkr: BTCUSD, tasty: BTCUSDT }
    ETH:  { ibkr: ETHUSD, tasty: ETHUSDT }
    MNQ:  { ibkr: MNQ }
    MNQ1: { ibkr: MNQ }
    NG:   { ibkr: NG }
    RTY:  { ibkr: RTY }
    GC:   { ibkr: GC }
    MGC:  { ibkr: MGC }
    CL:   { ibkr: CL }
    MCL:  { ibkr: MCL }
    "6E": { ibkr: 6E }
    M2K:  { ibkr: M2K }
    M6E:  { ibkr: M6E }
bots:
  btc_optimized: { venue: ibkr }
  btc_to_tasty:  { venue: tasty }
"""


def _write_routing_yaml(tmp_path: Path, body: str = _VALID_ROUTING_YAML) -> Path:
    """Drop a routing-config YAML file into ``tmp_path`` and return the path."""
    p = tmp_path / "routing.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestRoutingConfig:
    def test_load_from_path(self, tmp_path: Path) -> None:
        path = _write_routing_yaml(tmp_path)
        cfg = broker_router.RoutingConfig.load(path)
        assert cfg.default_venue == "ibkr"
        # Per-bot block parsed.
        assert "btc_optimized" in cfg.per_bot
        assert cfg.per_bot["btc_to_tasty"]["venue"] == "tasty"
        # Symbol overrides parsed and venue keys lower-cased.
        assert cfg.symbol_overrides["BTC"]["ibkr"] == "BTCUSD"
        assert cfg.symbol_overrides["BTC"]["tasty"] == "BTCUSDT"

    def test_missing_file_returns_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        missing = tmp_path / "does_not_exist.yaml"
        with caplog.at_level("WARNING", logger="eta_engine.broker_router"):
            cfg = broker_router.RoutingConfig.load(missing)
        # Default is the permissive ibkr-for-all config.
        assert cfg.default_venue == "ibkr"
        assert cfg.per_bot == {}
        assert cfg.symbol_overrides == {}
        # WARNING was emitted.
        assert any(
            "routing config not found" in rec.getMessage().lower()
            for rec in caplog.records
        ), f"expected WARNING about missing file, got {caplog.records!r}"

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        # Unbalanced mapping is a parse error.
        bad.write_text("default:\n  venue: ibkr\n  symbol_overrides: [", encoding="utf-8")
        with pytest.raises(ValueError):
            broker_router.RoutingConfig.load(bad)

    def test_per_bot_override_takes_precedence(self, tmp_path: Path) -> None:
        path = _write_routing_yaml(tmp_path)
        cfg = broker_router.RoutingConfig.load(path)
        # ``btc_to_tasty`` overrides default ibkr -> tasty.
        assert cfg.venue_for("btc_to_tasty") == "tasty"
        # ``btc_optimized`` is listed but matches default.
        assert cfg.venue_for("btc_optimized") == "ibkr"

    def test_default_used_when_bot_not_listed(self, tmp_path: Path) -> None:
        path = _write_routing_yaml(tmp_path)
        cfg = broker_router.RoutingConfig.load(path)
        assert cfg.venue_for("never_seen_bot") == "ibkr"

    def test_map_symbol_basic(self, tmp_path: Path) -> None:
        path = _write_routing_yaml(tmp_path)
        cfg = broker_router.RoutingConfig.load(path)
        assert cfg.map_symbol("BTC", "ibkr") == "BTCUSD"
        assert cfg.map_symbol("BTC", "tasty") == "BTCUSDT"
        assert cfg.map_symbol("MNQ1", "ibkr") == "MNQ"

    def test_map_symbol_unsupported_raises(self, tmp_path: Path) -> None:
        path = _write_routing_yaml(tmp_path)
        cfg = broker_router.RoutingConfig.load(path)
        with pytest.raises(ValueError):
            cfg.map_symbol("XYZ", "ibkr")
        # An override exists for BTC but only for ibkr+tasty -- a request
        # to map BTC to an unlisted venue should also raise.
        with pytest.raises(ValueError):
            cfg.map_symbol("BTC", "bybit")

    def test_map_symbol_no_override_returns_raw(self, tmp_path: Path) -> None:
        # A futures-style symbol not listed under symbol_overrides falls
        # through to the legacy futures-root pass-through.
        path = _write_routing_yaml(tmp_path)
        cfg = broker_router.RoutingConfig.load(path)
        assert cfg.map_symbol("ES", "ibkr") == "ES"
        # Already-normalized stable-quote pass-through.
        assert cfg.map_symbol("BTCUSDT", "tasty") == "BTCUSDT"

    def test_map_symbol_expanded_us_futures_roots(self, tmp_path: Path) -> None:
        path = _write_routing_yaml(tmp_path)
        cfg = broker_router.RoutingConfig.load(path)
        assert cfg.map_symbol("NG", "ibkr") == "NG"
        assert cfg.map_symbol("RTY", "ibkr") == "RTY"
        assert cfg.map_symbol("GC", "ibkr") == "GC"
        assert cfg.map_symbol("MGC", "ibkr") == "MGC"
        assert cfg.map_symbol("CL", "ibkr") == "CL"
        assert cfg.map_symbol("MCL", "ibkr") == "MCL"
        assert cfg.map_symbol("6E", "ibkr") == "6E"
        assert cfg.map_symbol("M2K", "ibkr") == "M2K"
        assert cfg.map_symbol("M6E", "ibkr") == "M6E"

    def test_env_var_override_picks_up_alternate_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = _VALID_ROUTING_YAML.replace("venue: ibkr", "venue: tasty", 1)
        path = tmp_path / "alt.yaml"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setenv("ETA_BROKER_ROUTING_CONFIG", str(path))
        cfg = broker_router.RoutingConfig.load()
        assert cfg.default_venue == "tasty"


# ---------------------------------------------------------------------------
# Lifecycle integration: routing config drives venue selection
# ---------------------------------------------------------------------------


class _FakeMultiVenueRouter:
    """SmartRouter stand-in with venue-by-name lookup, for routing-config tests."""

    def __init__(self, venues_by_name: dict[str, _FakeVenue]) -> None:
        self._venue_map = dict(venues_by_name)
        self.choose_venue_calls: list[tuple[str, float, str]] = []
        self.lookups: list[str] = []

    def choose_venue(
        self, symbol: str, qty: float, urgency: str = "normal",
    ) -> _FakeVenue:
        # The routing-config path should bypass this; the test asserts it.
        self.choose_venue_calls.append((symbol, qty, urgency))
        # Pick something sensible to avoid breaking unrelated callers.
        return next(iter(self._venue_map.values()))

    def _venue_by_name(self, name: str) -> _FakeVenue | None:
        self.lookups.append(name)
        return self._venue_map.get(name)


class TestLifecycleRoutingConfig:
    def test_unsupported_routing_pair_quarantined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pending file with unmapped symbol -> quarantine + venue NEVER called."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        # Symbol "ZZZ" is intentionally unmapped under all venues.
        path = _write_pending(pending_dir, bot_id="alpha", symbol="ZZZ")

        venue = _FakeVenue()
        smart_router = _FakeMultiVenueRouter({"ibkr": venue, "tasty": venue})
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        cfg = broker_router.RoutingConfig(
            default_venue="ibkr",
            symbol_overrides={"BTC": {"ibkr": "BTCUSD"}},
            per_bot={},
        )
        router = broker_router.BrokerRouter(
            pending_dir=pending_dir, state_root=state_root,
            smart_router=smart_router, journal=journal,
            gate_chain=gates, routing_config=cfg,
        )
        asyncio.run(router._process_pending_file(path))

        # File ended up in quarantine/.
        quarantined = _find_under(state_root / "quarantine", path.name)
        assert quarantined is not None, (
            f"expected file quarantined under {state_root / 'quarantine'!s}"
        )
        # Venue was NEVER called.
        assert venue.calls == [], "venue must not be called for an unmapped pair"
        # Journal recorded a NOTED quarantine event with the right reason.
        outcomes = journal.outcomes()
        assert any(str(o).upper() == "NOTED" for o in outcomes), (
            f"expected NOTED outcome, got {outcomes!r}"
        )
        intents = journal.intents()
        assert any("quarantine" in i.lower() for i in intents)
        # Reason field flagged as routing_config_unsupported_pair.
        meta_reasons: list[str] = []
        for evt in journal.events:
            md = getattr(evt, "metadata", {}) or {}
            if md.get("reason"):
                meta_reasons.append(str(md["reason"]))
        assert any("routing_config_unsupported_pair" in r for r in meta_reasons), (
            f"expected reason routing_config_unsupported_pair; got {meta_reasons!r}"
        )

    def test_per_bot_routing_picks_correct_venue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per-bot venue=tasty causes the tasty adapter to receive place_order."""
        pending_dir = tmp_path / "pending"
        state_root = tmp_path / "state"
        path = _write_pending(
            pending_dir, bot_id="btc_to_tasty", signal_id="sig-tasty",
            symbol="BTC",
        )

        ibkr_venue = _FakeVenue(
            results=[OrderResult(
                order_id="OID-IBKR", status=OrderStatus.FILLED, filled_qty=1.0,
            )],
        )
        ibkr_venue.name = "ibkr"
        tasty_venue = _FakeVenue(
            results=[OrderResult(
                order_id="OID-TASTY", status=OrderStatus.FILLED, filled_qty=1.0,
            )],
        )
        tasty_venue.name = "tasty"
        smart_router = _FakeMultiVenueRouter({
            "ibkr": ibkr_venue, "tasty": tasty_venue,
        })
        journal = _FakeJournal()
        gates = _allow_gate_chain()
        _stub_fetch_positions(monkeypatch, {})

        cfg = broker_router.RoutingConfig(
            default_venue="ibkr",
            symbol_overrides={"BTC": {"ibkr": "BTCUSD", "tasty": "BTCUSDT"}},
            per_bot={"btc_to_tasty": {"venue": "tasty"}},
        )
        router = broker_router.BrokerRouter(
            pending_dir=pending_dir, state_root=state_root,
            smart_router=smart_router, journal=journal,
            gate_chain=gates, routing_config=cfg,
        )
        asyncio.run(router._process_pending_file(path))

        # The tasty adapter -- and ONLY the tasty adapter -- got the order.
        assert len(tasty_venue.calls) == 1, (
            f"expected the tasty venue to receive 1 order; got {len(tasty_venue.calls)}"
        )
        assert ibkr_venue.calls == [], (
            "ibkr venue should not have been called for btc_to_tasty"
        )
        # Symbol was mapped to BTCUSDT for the tasty adapter.
        assert tasty_venue.calls[0].symbol == "BTCUSDT", (
            f"expected BTCUSDT; got {tasty_venue.calls[0].symbol!r}"
        )
        # The router used the venue-by-name lookup, not choose_venue.
        assert "tasty" in smart_router.lookups, (
            f"expected venue-by-name lookup for 'tasty'; got {smart_router.lookups!r}"
        )
        assert smart_router.choose_venue_calls == [], (
            "choose_venue should be bypassed when routing config + venue map cover the route"
        )
