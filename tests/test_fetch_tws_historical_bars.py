"""Tests for ``eta_engine.scripts.fetch_tws_historical_bars``.

Mocks the ``ib_insync.IB()`` surface so no live TWS connection is needed.
Verifies:

* CLI parsing + defaults
* ``--dry-run`` prints chunk plan without connecting
* Chunking math (``plan_chunks`` cursor walk + canonical chunk count)
* Connection fallback ordering (4002 -> 7497 -> 4001)
* CSV merge dedupes by timestamp
* End-to-end ``run()`` with a mocked IB returning synthetic bars
* Single-chunk failure does not crash the loop
"""
from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eta_engine.scripts import fetch_tws_historical_bars as mod


# --- Mock IB ---
class _MockBar:
    """Stand-in for ib_insync.BarData. Only fields the fetcher reads."""

    def __init__(self, dt: datetime, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.date = dt
        self.open = o
        self.high = h
        self.low = lo
        self.close = c
        self.volume = v


class _MockQualifiedContract:
    def __init__(self, symbol: str, exchange: str = "CME", expiry: str = "20260619") -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.lastTradeDateOrContractMonth = expiry


class _MockIB:
    """Records calls + returns synthetic bars for each chunk."""

    def __init__(
        self,
        *,
        connect_success_ports: list[int] | None = None,
        synthetic_bars_per_chunk: int = 5,
        fail_chunk_indices: set[int] | None = None,
        pacing_violation_indices: set[int] | None = None,
    ) -> None:
        # `[]` (no port open) and `None` (default to 4002) are distinct --
        # use a sentinel check rather than truthiness so an empty list is
        # honored as "all ports closed".
        self.connect_success_ports = (
            [4002] if connect_success_ports is None else connect_success_ports
        )
        self.synthetic_bars_per_chunk = synthetic_bars_per_chunk
        self.fail_chunk_indices = fail_chunk_indices or set()
        self.pacing_violation_indices = pacing_violation_indices or set()

        self.connect_calls: list[tuple[str, int, int]] = []
        self.disconnect_called = False
        self.qualify_calls: list[Any] = []
        self.history_calls: list[dict[str, Any]] = []
        self._connected = False

    def connect(self, host: str, port: int, clientId: int, timeout: float) -> None:  # noqa: N803, ARG002
        self.connect_calls.append((host, port, clientId))
        if port not in self.connect_success_ports:
            raise ConnectionError(f"mock: port {port} closed")
        self._connected = True

    def disconnect(self) -> None:
        self.disconnect_called = True
        self._connected = False

    def isConnected(self) -> bool:
        return self._connected

    def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
        self.qualify_calls.extend(contracts)
        # Return a fake qualified contract for each input.
        return [
            _MockQualifiedContract(symbol=getattr(c, "symbol", "?"))
            for c in contracts
        ]

    def reqContractDetails(self, contract: Any) -> list[Any]:  # noqa: N802
        # Default mock: shouldn't be called when qualifyContracts succeeds.
        # Subclasses override this to test the front-month-fallback path.
        return []

    def reqHistoricalData(
        self,
        contract: Any,  # noqa: ARG002
        endDateTime: str,  # noqa: N803
        durationStr: str,  # noqa: N803
        barSizeSetting: str,  # noqa: N803
        whatToShow: str,  # noqa: N803
        useRTH: bool,  # noqa: N803
        formatDate: int,  # noqa: N803, ARG002
    ) -> list[_MockBar]:
        idx = len(self.history_calls)
        self.history_calls.append({
            "endDateTime": endDateTime,
            "durationStr": durationStr,
            "barSizeSetting": barSizeSetting,
            "whatToShow": whatToShow,
            "useRTH": useRTH,
        })
        if idx in self.pacing_violation_indices:
            raise RuntimeError("Historical Market Data Service error message:Pacing violation")
        if idx in self.fail_chunk_indices:
            raise RuntimeError(f"mock chunk {idx} failed")
        # Build N synthetic 5-min bars ending at the requested end time.
        # endDateTime arrives as "YYYYMMDD HH:MM:SS"; parse back to a datetime.
        end_dt = datetime.strptime(endDateTime, "%Y%m%d %H:%M:%S").replace(tzinfo=UTC)
        out: list[_MockBar] = []
        # Salt timestamps with chunk index to guarantee uniqueness across chunks
        # (otherwise a 540dx5m run can repeat the same end-of-chunk timestamp
        # and the dedupe step trims our totals before assertion).
        salt = idx * 31  # 31 prime, so chunks don't collide on 5m boundaries.
        for i in range(self.synthetic_bars_per_chunk):
            ts = end_dt - timedelta(minutes=5 * (i + 1) + salt)
            out.append(_MockBar(
                dt=ts, o=50000.0 + idx, h=50100.0 + idx,
                lo=49900.0 + idx, c=50050.0 + idx, v=10.0,
            ))
        return out


# --- Smoke + parser ---
def test_module_imports_cleanly() -> None:
    assert hasattr(mod, "build_parser")
    assert hasattr(mod, "run")
    assert hasattr(mod, "plan_chunks")
    assert hasattr(mod, "fetch_chunks")
    assert hasattr(mod, "canonical_bar_path")
    assert hasattr(mod, "merge_with_existing")


def test_parser_defaults_match_spec() -> None:
    parser = mod.build_parser()
    args = parser.parse_args([])
    assert args.symbols == ["MBT", "MET"]
    assert args.days == 540
    assert args.timeframe == "5m"
    assert args.port == 4002
    assert args.client_id == 11
    assert args.dry_run is False


def test_parser_accepts_overrides() -> None:
    parser = mod.build_parser()
    args = parser.parse_args([
        "--symbols", "MNQ", "ES",
        "--days", "30",
        "--timeframe", "1m",
        "--port", "7497",
        "--client-id", "42",
    ])
    assert args.symbols == ["MNQ", "ES"]
    assert args.days == 30
    assert args.timeframe == "1m"
    assert args.port == 7497
    assert args.client_id == 42


# --- plan_chunks math ---
def test_plan_chunks_walks_window_backwards() -> None:
    end = datetime(2026, 5, 1, tzinfo=UTC)
    plan = mod.plan_chunks(timeframe="5m", days=60, end=end)
    # 60 days / 30-day chunks -> 2 chunks.
    assert len(plan) == 2
    assert plan[0].end_dt == end
    # Cursors strictly decreasing.
    assert plan[1].end_dt < plan[0].end_dt


def test_plan_chunks_540_days_5m_yields_18_chunks() -> None:
    """Operator-runbook scenario: 540d x 5m -> 18 chunks/symbol.

    This is the core chunking-math claim that the runbook surfaces;
    breaking it would silently change pacing-budget arithmetic.
    """
    end = datetime(2026, 5, 1, tzinfo=UTC)
    plan = mod.plan_chunks(timeframe="5m", days=540, end=end)
    # 540 / 30 = 18.
    assert len(plan) == 18
    # All chunks request 30 D.
    assert all(c.duration_str == "30 D" for c in plan)
    assert all(c.bar_size == "5 mins" for c in plan)


def test_plan_chunks_1m_uses_1day_chunks() -> None:
    end = datetime(2026, 5, 1, tzinfo=UTC)
    plan = mod.plan_chunks(timeframe="1m", days=5, end=end)
    assert len(plan) == 5
    assert all(c.duration_str == "1 D" for c in plan)


def test_plan_chunks_rejects_unknown_timeframe() -> None:
    with pytest.raises(ValueError, match="unknown timeframe"):
        mod.plan_chunks(timeframe="42m", days=10, end=datetime.now(UTC))


# --- Bar conversion ---
def test_bar_to_row_handles_datetime() -> None:
    bar = _MockBar(
        dt=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        o=100.0, h=101.0, lo=99.0, c=100.5, v=42.0,
    )
    row = mod._bar_to_row(bar)
    assert row is not None
    assert row["time"] == int(datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp())
    assert row["close"] == 100.5
    assert row["volume"] == 42.0


def test_bar_to_row_handles_naive_datetime_as_utc() -> None:
    bar = _MockBar(
        dt=datetime(2026, 5, 1, 12, 0),  # naive
        o=1, h=1, lo=1, c=1, v=1,
    )
    row = mod._bar_to_row(bar)
    assert row is not None
    expected = int(datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp())
    assert row["time"] == expected


def test_bar_to_row_returns_none_on_missing_date() -> None:
    bar = _MockBar(dt=None, o=0, h=0, lo=0, c=0, v=0)  # type: ignore[arg-type]
    assert mod._bar_to_row(bar) is None


# --- Connect with fallback ---
def test_connect_with_fallback_uses_primary_when_open() -> None:
    ib = _MockIB(connect_success_ports=[4002])
    landed = mod._connect_with_fallback(
        ib, host="127.0.0.1", primary_port=4002, client_id=11,
    )
    assert landed == 4002
    assert ib.connect_calls == [("127.0.0.1", 4002, 11)]


def test_connect_with_fallback_walks_to_secondary() -> None:
    ib = _MockIB(connect_success_ports=[7497])  # only paper TWS open
    landed = mod._connect_with_fallback(
        ib, host="127.0.0.1", primary_port=4002, client_id=11,
    )
    assert landed == 7497
    # Tried 4002 first, then 7497.
    assert [call[1] for call in ib.connect_calls] == [4002, 7497]


def test_connect_with_fallback_raises_when_all_closed() -> None:
    ib = _MockIB(connect_success_ports=[])
    with pytest.raises(ConnectionError):
        mod._connect_with_fallback(
            ib, host="127.0.0.1", primary_port=4002, client_id=11,
        )


# --- Merge / write ---
def test_merge_with_existing_dedupes_by_time(tmp_path: Path) -> None:
    out_path = tmp_path / "MBT1_5m.csv"
    existing = [
        {"time": 1000, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0},
        {"time": 1300, "open": 1.05, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 12.0},
    ]
    mod.write_csv(out_path, existing)

    new = [
        # Duplicate timestamp -- should be skipped.
        {"time": 1000, "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "volume": 0.0},
        {"time": 1600, "open": 1.15, "high": 1.3, "low": 1.1, "close": 1.25, "volume": 14.0},
    ]
    merged, n_existing, n_new = mod.merge_with_existing(out_path, new)
    assert n_existing == 2
    assert n_new == 1
    assert [r["time"] for r in merged] == [1000, 1300, 1600]
    # Original row preserved (not overwritten by duplicate).
    assert merged[0]["high"] == pytest.approx(1.1)


def test_canonical_bar_path_matches_lab_harness_pattern() -> None:
    p = mod.canonical_bar_path("MBT", "5m")
    assert p.name == "MBT1_5m.csv"
    assert p.parent.name == "history"

    # Daily timeframe normalizes to 'D'.
    p = mod.canonical_bar_path("MNQ", "1d")
    assert p.name == "MNQ1_D.csv"


def test_canonical_bar_path_respects_root_override(tmp_path: Path) -> None:
    p = mod.canonical_bar_path("ES", "5m", root=tmp_path)
    assert p == tmp_path / "ES1_5m.csv"


def test_csv_format_matches_load_ohlcv_expectations(tmp_path: Path) -> None:
    out_path = tmp_path / "MBT1_5m.csv"
    rows = [
        {"time": 1_700_000_000, "open": 50000.0, "high": 50100.0,
         "low": 49900.0, "close": 50050.0, "volume": 12.5},
        {"time": 1_700_000_300, "open": 50050.0, "high": 50200.0,
         "low": 50000.0, "close": 50150.0, "volume": 10.0},
    ]
    mod.write_csv(out_path, rows)

    with out_path.open() as f:
        header = next(csv.reader(f))
    assert header == ["time", "open", "high", "low", "close", "volume"]


# --- Dry run ---
def test_run_dry_run_does_not_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """--dry-run must print plan and never instantiate ib_insync.IB()."""
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("dry-run must not import or call ib_insync")

    # If the script tries to import IB(), this would fire -- _connect_with_fallback
    # is the primary entry, so guard against accidental connect.
    monkeypatch.setattr(mod, "_connect_with_fallback", boom)

    rc = mod.run([
        "--symbols", "MBT", "MET",
        "--days", "30",
        "--root", str(tmp_path),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "MBT" in out
    assert "MET" in out
    # No CSV created.
    assert not list(tmp_path.glob("*.csv"))


def test_dry_run_chunk_total_for_540d_two_symbols(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    """The runbook total is 540d x 5m x 2 sym = 36 chunks."""
    rc = mod.run([
        "--symbols", "MBT", "MET",
        "--days", "540",
        "--root", str(tmp_path),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total chunks across symbols: 36" in out


# --- End-to-end ---
def test_run_writes_canonical_csv_with_mocked_ib(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ib = _MockIB(synthetic_bars_per_chunk=5)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "60",  # 2 chunks
            "--end", "2026-05-01",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 0

    # CSV at canonical filename pattern.
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    # 2 chunks x 5 bars = 10 bars (no overlap thanks to the salt offset).
    assert len(rows) == 10

    # Header matches downstream expectations.
    assert rows[0].keys() >= {"time", "open", "high", "low", "close", "volume"}

    # Connect + disconnect happened.
    assert ib.connect_calls
    assert ib.disconnect_called

    # qualifyContracts called for the futures contract.
    assert len(ib.qualify_calls) == 1

    # 2 chunks executed.
    assert len(ib.history_calls) == 2
    # Each chunk uses the right bar size + duration.
    assert all(call["barSizeSetting"] == "5 mins" for call in ib.history_calls)
    assert all(call["durationStr"] == "30 D" for call in ib.history_calls)


def test_run_continues_past_a_failed_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A single bad chunk must log + continue, not abort the symbol."""
    ib = _MockIB(synthetic_bars_per_chunk=5, fail_chunk_indices={0})
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "60",
            "--end", "2026-05-01",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 0
    # 1 chunk failed but the second succeeded -- 5 bars written.
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 5


def test_run_handles_pacing_violation_with_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A pacing violation triggers the back-off branch, not the generic-fail one."""
    sleep_calls: list[float] = []

    def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    ib = _MockIB(synthetic_bars_per_chunk=3, pacing_violation_indices={0})
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "60",
            "--end", "2026-05-01",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 0
    # The pacing-violation back-off (60s) should appear in sleep calls.
    assert mod._PACING_VIOLATION_BACKOFF_S in sleep_calls


def test_run_returns_1_when_no_rows_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty fetch should fail loudly so the operator notices."""
    ib = _MockIB(synthetic_bars_per_chunk=0)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "30",
            "--end", "2026-05-01",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 1


def test_run_skips_unsupported_symbol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown symbol logs a warning + is dropped from the work list."""
    ib = _MockIB(synthetic_bars_per_chunk=5)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    with caplog.at_level("WARNING"):
        rc = mod.run(
            [
                "--symbols", "MBT", "ZZZ_NOT_A_FUTURES_SYMBOL",
                "--days", "30",
                "--end", "2026-05-01",
                "--root", str(tmp_path),
                "--pacing-sleep", "0",
            ],
            ib=ib,
        )
    assert rc == 0
    # Only MBT got fetched.
    assert (tmp_path / "MBT1_5m.csv").exists()
    assert not (tmp_path / "ZZZ_NOT_A_FUTURES_SYMBOL1_5m.csv").exists()
    # And we logged about it.
    assert any("ZZZ" in rec.message for rec in caplog.records)


def test_run_returns_1_on_connect_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ib = _MockIB(connect_success_ports=[])  # all ports closed
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "30",
            "--end", "2026-05-01",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 1


# --- Front-month fallback (reqContractDetails when qualifyContracts ambiguous) ---


class _MockContractDetails:
    """Wraps a Contract — mirrors ib_insync.ContractDetails interface."""

    def __init__(self, expiry: str, symbol: str = "MBT") -> None:
        self.contract = _MockQualifiedContract(symbol=symbol, expiry=expiry)
        self.contractMonth = expiry[:6] if len(expiry) >= 6 else expiry


class _AmbiguousIB(_MockIB):
    """Mock that returns [] from qualifyContracts, simulating ambiguity
    (CME crypto micros have 11+ active months) and exposes
    reqContractDetails for the front-month fallback."""

    def __init__(self, candidate_expiries: list[str], **kw: Any) -> None:
        super().__init__(**kw)
        self._candidate_expiries = candidate_expiries
        self.contract_details_calls: list[Any] = []

    def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
        self.qualify_calls.extend(contracts)
        return []  # ambiguous

    def reqContractDetails(self, contract: Any) -> list[Any]:  # noqa: N802
        self.contract_details_calls.append(contract)
        return [_MockContractDetails(e) for e in self._candidate_expiries]


def test_front_month_fallback_picks_soonest_non_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When qualifyContracts returns [] (ambiguous), the fetcher must fall
    back to reqContractDetails and pick the SOONEST non-expired expiration.
    """
    # Today=20260507 in test; soonest non-expired = 20260627.
    ib = _AmbiguousIB(
        candidate_expiries=[
            "20240619",  # already expired
            "20260919",  # later
            "20260627",  # soonest non-expired (winner)
            "20261219",  # latest
        ],
        synthetic_bars_per_chunk=5,
    )
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "30",
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 0
    # Fallback path was hit (qualifyContracts returned [] then
    # reqContractDetails was called).
    assert len(ib.qualify_calls) == 1
    assert len(ib.contract_details_calls) == 1
    # CSV was produced — front-month resolution succeeded.
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()
    # And the historical-bars call used the resolved contract.
    assert len(ib.history_calls) >= 1


def test_front_month_fallback_aborts_when_all_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """All candidates expired → fetcher logs + skips the symbol cleanly."""
    ib = _AmbiguousIB(
        candidate_expiries=["20240619", "20240919", "20241219"],
        synthetic_bars_per_chunk=5,
    )
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "30",
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    # rc=1 because zero rows fetched globally (single symbol failed).
    # That's correct behavior — fail-loud at exit code level even though
    # the fetcher itself didn't crash.
    assert rc == 1
    # The fetcher aborts before issuing any reqHistoricalData call.
    assert len(ib.history_calls) == 0
    # Fallback was attempted (reqContractDetails called) and recognized
    # all candidates as expired.
    assert len(ib.contract_details_calls) == 1


# --- Gap report ---
def test_report_gaps_flags_skipped_bars() -> None:
    rows = [
        {"time": 1000},
        {"time": 1300},  # +5min, OK
        {"time": 2500},  # +20min, gap
        {"time": 2800},  # +5min, OK
    ]
    gaps = mod.report_gaps(rows, "5m")
    assert len(gaps) == 1
    assert gaps[0] == (1300, 2500)


# --- Commodity 5m fetch coverage (GC / CL / NG / ZN / 6E) ---
#
# The 2026-05-07 fleet audit found the 1h timeframe is too coarse for
# commodities — most edges live at 15m or 5m intraday. These tests verify
# the fetcher's _FUTURES_MAP knows the 5 commodities at the IBKR-correct
# exchange + currency + multiplier and that the dry-run / front-month
# paths work for them too.
#
# IBKR contract-resolution truth (mirrors venues/ibkr_live.py:FUTURES_MAP):
#   GC, MGC -> COMEX (CME Group's metals child exchange)
#   CL, MCL, NG -> NYMEX (CME Group's energy child exchange)
#   ZN, ZB -> CBOT  (CME Group's rates child exchange)
#   6E (indexed at IB as "EUR"), M6E -> CME (CME proper for FX)
#
# Wrong-exchange strings cause qualifyContracts to return [] silently
# even on otherwise valid Future objects — caught in production by the
# 2026-05-05 NYMEX CL/MCL/NG smoke-harness regression.
_COMMODITY_SYMBOLS: tuple[str, ...] = ("GC", "CL", "NG", "ZN", "6E")


def test_futures_map_covers_all_5_commodities() -> None:
    """All 5 commodity contracts in the 2026-05-07 audit must be present."""
    for sym in _COMMODITY_SYMBOLS:
        assert sym in mod._FUTURES_MAP, (
            f"commodity symbol {sym} missing from _FUTURES_MAP -- the "
            "2026-05-07 fleet audit needs 15m/5m bars for this contract"
        )


def test_commodity_exchanges_match_ibkr_contract_resolution() -> None:
    """Verify each commodity has the IBKR-correct exchange string.

    IBKR's contract-resolution layer treats CME / COMEX / NYMEX / CBOT as
    distinct venues even though they share a parent (CME Group). Using
    the wrong child exchange (e.g. "CME" for GC instead of "COMEX") makes
    qualifyContracts silently return [] -- the same failure mode that bit
    NYMEX CL/MCL/NG in the 2026-05-05 smoke-harness sweep.
    """
    expected: dict[str, tuple[str, str]] = {
        # symbol: (root, exchange) — must match venues.ibkr_live.FUTURES_MAP
        "GC":  ("GC",  "COMEX"),
        "MGC": ("MGC", "COMEX"),
        "CL":  ("CL",  "NYMEX"),
        "MCL": ("MCL", "NYMEX"),
        "NG":  ("NG",  "NYMEX"),
        "ZN":  ("ZN",  "CBOT"),
        "ZB":  ("ZB",  "CBOT"),
        # 6E: IB indexes Euro FX under "EUR" trading code, NOT "6E".
        # See venues.ibkr_live._build_contract for the same translation.
        "6E":  ("EUR", "CME"),
        "M6E": ("M6E", "CME"),
    }
    for sym, (want_root, want_exchange) in expected.items():
        spec = mod._FUTURES_MAP.get(sym)
        assert spec is not None, f"{sym} missing from _FUTURES_MAP"
        root, exchange, currency, _mult = spec
        assert root == want_root, (
            f"{sym}: root={root!r} but IBKR indexes it as {want_root!r} "
            f"(see venues/ibkr_live.py)"
        )
        assert exchange == want_exchange, (
            f"{sym}: exchange={exchange!r} but IBKR routes it on "
            f"{want_exchange!r}; using the wrong child of CME Group makes "
            "qualifyContracts return [] silently"
        )
        assert currency == "USD", f"{sym}: expected USD, got {currency!r}"


def test_commodity_multipliers_match_instrument_specs() -> None:
    """Multipliers must match feeds/instrument_specs.py point_value.

    Wrong multiplier => catastrophic sizing bugs. The 2026-05-05 sweep
    saw $-866K loss on 8 6E trades in 90d before the spec fix; the
    fetcher's multiplier feeds the same downstream sizing path.
    """
    # (symbol, expected_multiplier_as_str)
    expected_multipliers: dict[str, str] = {
        "GC":  "100",      # USD per 1.0 of price (gold: $100/oz)
        "MGC": "10",       # micro gold: 1/10th
        "CL":  "1000",     # USD per $1.00 of crude (1000 bbl)
        "MCL": "100",      # micro crude: 1/10th
        "NG":  "10000",    # USD per 1.0 of nat gas price (10000 MMBtu)
        "ZN":  "1000",     # USD per 1.0 of price (10y note)
        "ZB":  "1000",     # USD per 1.0 of price (30y bond)
        "6E":  "125000",   # full-size Euro FX: EUR 125,000
        "M6E": "12500",    # micro Euro FX: 1/10th
    }
    for sym, want_mult in expected_multipliers.items():
        spec = mod._FUTURES_MAP.get(sym)
        assert spec is not None
        _root, _exchange, _ccy, mult = spec
        assert mult == want_mult, (
            f"{sym}: multiplier={mult!r} but expected {want_mult!r}; "
            "wrong multiplier => sizing-math errors downstream"
        )


@pytest.mark.parametrize("symbol", list(_COMMODITY_SYMBOLS))
def test_commodity_dry_run_emits_chunk_plan(
    symbol: str, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    """--dry-run must produce a chunk plan for each commodity at 5m.

    540d / 30d-per-chunk = 18 chunks/symbol (the runbook number). This
    is the same chunking math MBT/MET use, so any commodity that fails
    here would point to a regression in plan_chunks, not the symbol.
    """
    rc = mod.run([
        "--symbols", symbol,
        "--days", "540",
        "--timeframe", "5m",
        "--root", str(tmp_path),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert symbol in out
    assert "18 chunks" in out, f"{symbol}: expected 18 chunks at 540d x 5m"
    # No CSV created in dry-run.
    assert not list(tmp_path.glob("*.csv"))


def test_commodity_dry_run_total_for_5_symbols(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    """The 5-commodity 540d 5m fleet fetch is 5 x 18 = 90 chunks."""
    rc = mod.run([
        "--symbols", *_COMMODITY_SYMBOLS,
        "--days", "540",
        "--timeframe", "5m",
        "--root", str(tmp_path),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total chunks across symbols: 90" in out


@pytest.mark.parametrize("symbol", list(_COMMODITY_SYMBOLS))
def test_commodity_end_to_end_with_mocked_ib(
    symbol: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Each commodity runs end-to-end against a mocked IB and writes its CSV.

    Verifies the symbol resolves through _build_future + qualifyContracts
    against a synthetic IB. No live TWS connection needed.
    """
    ib = _MockIB(synthetic_bars_per_chunk=5)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", symbol,
            "--days", "60",  # 2 chunks
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 0
    csv_path = tmp_path / f"{symbol}1_5m.csv"
    assert csv_path.exists(), f"{symbol}: canonical CSV not written"
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 10, f"{symbol}: expected 10 bars (2 chunks x 5)"


@pytest.mark.parametrize("symbol", list(_COMMODITY_SYMBOLS))
def test_commodity_front_month_fallback_resolves(
    symbol: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The same qualifyContracts -> reqContractDetails fallback used by
    MBT/MET must work for commodities, which also have multiple active
    expirations (especially CL/NG with monthly listings 12+ months out).
    """
    ib = _AmbiguousIB(
        candidate_expiries=[
            "20240619",  # expired
            "20260619",  # soonest non-expired (winner for 2026-05-07)
            "20260919",
            "20261219",
        ],
        synthetic_bars_per_chunk=5,
    )
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", symbol,
            "--days", "30",
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
        ],
        ib=ib,
    )
    assert rc == 0, f"{symbol}: front-month fallback failed"
    # Fallback path was used.
    assert len(ib.qualify_calls) == 1
    assert len(ib.contract_details_calls) == 1
    # CSV produced.
    assert (tmp_path / f"{symbol}1_5m.csv").exists()


# --- Back-fetch mode (multi-contract stitch) ---
#
# The 2026-05-07 fetcher run on the VPS exposed that TWS HMDS only has
# data for the SPECIFIC current front-month contract -- monthly-roll
# futures (MBT, MET, CL, MCL, NG) cap at ~70 days because the prior
# months' contracts didn't exist before their listing date. The
# `--back-fetch` mode enumerates each historical contract that was
# front-month during the requested window and stitches them.


def test_enumerate_back_fetch_contracts_540d_mbt_monthly() -> None:
    """540d back from 2026-05-07 -> ~18-19 monthly contracts for MBT.

    Range: 2024-11 through 2026-05 inclusive = 19 (Nov, Dec, Jan, Feb,
    Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec, Jan, Feb, Mar,
    Apr, May = 19 calendar months). The exact count is determined by
    how the 540-day earliest cursor falls inside November 2024.
    """
    end = datetime(2026, 5, 7, tzinfo=UTC)
    contracts = mod.enumerate_back_fetch_contracts(
        symbol="MBT", days=540, end=end,
    )
    # 540d window crosses 18-19 calendar-month boundaries.
    assert 18 <= len(contracts) <= 19
    # Newest contract is the current front-month (or end-of-window).
    last_year, last_month = contracts[-1]
    assert (last_year, last_month) == (2026, 5)
    # All entries unique, sorted ascending.
    assert contracts == sorted(set(contracts))


def test_enumerate_back_fetch_contracts_quarterly_mnq() -> None:
    """For quarterly contracts (MNQ), 540d -> ~6 quarterly listings."""
    end = datetime(2026, 5, 7, tzinfo=UTC)
    contracts = mod.enumerate_back_fetch_contracts(
        symbol="MNQ", days=540, end=end,
    )
    # All months are quarterly: 3, 6, 9, or 12.
    for _y, m in contracts:
        assert m in (3, 6, 9, 12), f"MNQ {m} is not a quarterly month"
    # 540d / 90d-per-quarter = 6 quarterly contracts (give or take 1).
    assert 5 <= len(contracts) <= 8


def test_enumerate_back_fetch_contracts_monthly_cl() -> None:
    """CL has monthly listings; 90d back from 2026-05-07 -> 4 contracts.

    Range covers Feb, Mar, Apr, May 2026.
    """
    end = datetime(2026, 5, 7, tzinfo=UTC)
    contracts = mod.enumerate_back_fetch_contracts(
        symbol="CL", days=90, end=end,
    )
    assert (2026, 5) in contracts
    assert (2026, 4) in contracts
    assert (2026, 3) in contracts
    # Should NOT include 2025 -- 90d back from 2026-05-07 is 2026-02-06.
    assert all(y == 2026 for y, _m in contracts)


def test_last_business_day_of_month_skips_weekend() -> None:
    """Sat 31 May 2025 -> Fri 30 May 2025."""
    # May 2025: 31 = Saturday, so last business day = 30 (Friday).
    bd = mod._last_business_day_of_month(2025, 5)
    assert bd.year == 2025 and bd.month == 5
    assert bd.weekday() < 5


def test_last_business_day_of_month_december_rollover() -> None:
    """December rolls into next year's January-first calculation."""
    bd = mod._last_business_day_of_month(2026, 12)
    assert bd.year == 2026 and bd.month == 12
    # Last weekday of Dec 2026: Dec 31 = Thursday.
    assert bd.weekday() < 5


def test_build_specific_future_pins_year_month() -> None:
    """_build_specific_future encodes lastTradeDateOrContractMonth=YYYYMM."""
    contract = mod._build_specific_future("MBT", 2026, 4)
    assert contract.symbol == "MBT"
    assert contract.lastTradeDateOrContractMonth == "202604"
    assert contract.exchange == "CME"
    assert contract.currency == "USD"
    # includeExpired is required for fetching expired contracts.
    assert contract.includeExpired is True


def test_build_specific_future_rejects_unknown_symbol() -> None:
    with pytest.raises(ValueError, match="unknown futures symbol"):
        mod._build_specific_future("ZZZ_NOT_A_SYMBOL", 2026, 4)


def test_plan_back_fetch_chunks_monthly_one_chunk() -> None:
    """A monthly contract's ~30-day window should be ~1 chunk at 5m."""
    plan = mod.plan_back_fetch_chunks(
        symbol="MBT", year=2026, month=4, timeframe="5m",
    )
    # 30-day window / 30-day chunks = 1 chunk.
    assert len(plan) == 1
    assert plan[0].duration_str == "30 D"


def test_plan_back_fetch_chunks_quarterly_three_chunks() -> None:
    """A quarterly contract's ~90-day window -> 3 chunks at 5m."""
    plan = mod.plan_back_fetch_chunks(
        symbol="MNQ", year=2026, month=6, timeframe="5m",
    )
    # ~90-day window / 30-day chunks -> 3 chunks.
    assert 2 <= len(plan) <= 4


# --- Stitch (continuous + adjust) ---
def test_stitch_continuous_no_adjust_concatenates_chronologically() -> None:
    """Without --adjust, stitched output is the raw concatenation."""
    rows_old = [
        {"time": 1000, "open": 50000.0, "high": 50100.0, "low": 49900.0,
         "close": 50050.0, "volume": 10.0},
        {"time": 1300, "open": 50050.0, "high": 50200.0, "low": 50000.0,
         "close": 50150.0, "volume": 11.0},
    ]
    rows_new = [
        # Newer contract starts at higher price (e.g. roll premium of $200).
        {"time": 1600, "open": 50350.0, "high": 50450.0, "low": 50250.0,
         "close": 50400.0, "volume": 12.0},
        {"time": 1900, "open": 50400.0, "high": 50500.0, "low": 50300.0,
         "close": 50450.0, "volume": 13.0},
    ]
    out = mod._stitch_continuous(
        [((2026, 4), rows_old), ((2026, 5), rows_new)],
        adjust=False,
    )
    assert [r["time"] for r in out] == [1000, 1300, 1600, 1900]
    # Old contract's close stays at 50150, raw -- discontinuity at roll.
    assert out[1]["close"] == pytest.approx(50150.0)
    assert out[2]["close"] == pytest.approx(50400.0)


def test_stitch_continuous_with_adjust_makes_price_continuous() -> None:
    """With --adjust, the older contract's OHLC is shifted by the
    delta = first(new).close - last(old).close so the roll is seamless.
    """
    rows_old = [
        {"time": 1000, "open": 50000.0, "high": 50100.0, "low": 49900.0,
         "close": 50050.0, "volume": 10.0},
        {"time": 1300, "open": 50050.0, "high": 50200.0, "low": 50000.0,
         "close": 50150.0, "volume": 11.0},
    ]
    rows_new = [
        {"time": 1600, "open": 50350.0, "high": 50450.0, "low": 50250.0,
         "close": 50400.0, "volume": 12.0},
    ]
    out = mod._stitch_continuous(
        [((2026, 4), rows_old), ((2026, 5), rows_new)],
        adjust=True,
    )
    # Delta = 50400 (first close of new) - 50150 (last close of old) = +250.
    # Older contract's OHLC must be shifted +250 so the boundary is seamless.
    assert out[0]["close"] == pytest.approx(50050.0 + 250.0)
    assert out[1]["close"] == pytest.approx(50150.0 + 250.0)
    # Newer contract is unchanged.
    assert out[2]["close"] == pytest.approx(50400.0)
    # Volume is NOT shifted.
    assert out[0]["volume"] == pytest.approx(10.0)


def test_stitch_continuous_dedupes_overlapping_timestamps() -> None:
    """If two contracts both report a bar at the same timestamp during the
    roll overlap, only the older contract's value is kept (first-wins).
    """
    rows_old = [
        {"time": 1000, "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "volume": 1.0},
        {"time": 2000, "open": 2.0, "high": 2.0, "low": 2.0,
         "close": 2.0, "volume": 2.0},  # overlap with rows_new[0]
    ]
    rows_new = [
        {"time": 2000, "open": 99.0, "high": 99.0, "low": 99.0,
         "close": 99.0, "volume": 99.0},
        {"time": 3000, "open": 3.0, "high": 3.0, "low": 3.0,
         "close": 3.0, "volume": 3.0},
    ]
    out = mod._stitch_continuous(
        [((2026, 4), rows_old), ((2026, 5), rows_new)],
        adjust=False,
    )
    assert [r["time"] for r in out] == [1000, 2000, 3000]
    # Old contract wins for the overlapping ts (first-wins dedupe).
    assert out[1]["close"] == pytest.approx(2.0)


def test_stitch_continuous_empty_input() -> None:
    assert mod._stitch_continuous([]) == []


def test_stitch_continuous_single_contract_no_adjust_or_change() -> None:
    """Single contract: stitch is a passthrough (deduped, sorted)."""
    rows = [
        {"time": 1000, "open": 1, "high": 1, "low": 1,
         "close": 1, "volume": 1},
        {"time": 500, "open": 0.5, "high": 0.5, "low": 0.5,
         "close": 0.5, "volume": 0.5},  # out-of-order
    ]
    out = mod._stitch_continuous([((2026, 5), rows)], adjust=True)
    assert [r["time"] for r in out] == [500, 1000]


# --- Back-fetch end-to-end with mocked IB ---


class _BackFetchIB(_MockIB):
    """Mock that handles per-contract qualifyContracts (no ambiguity)
    and returns synthetic bars per chunk -- exercises the back-fetch
    path which uses lastTradeDateOrContractMonth=YYYYMM.
    """

    def __init__(
        self,
        *,
        synthetic_bars_per_chunk: int = 5,
        unlisted_months: set[tuple[int, int]] | None = None,
    ) -> None:
        super().__init__(synthetic_bars_per_chunk=synthetic_bars_per_chunk)
        self.unlisted_months = unlisted_months or set()
        self.qualified_yyyymm: list[str] = []

    def qualifyContracts(self, *contracts: Any) -> list[Any]:  # noqa: N802
        self.qualify_calls.extend(contracts)
        out: list[Any] = []
        for c in contracts:
            yyyymm = getattr(c, "lastTradeDateOrContractMonth", "")
            self.qualified_yyyymm.append(yyyymm)
            if len(yyyymm) >= 6:
                year = int(yyyymm[:4])
                month = int(yyyymm[4:6])
                if (year, month) in self.unlisted_months:
                    continue
            out.append(_MockQualifiedContract(
                symbol=getattr(c, "symbol", "?"), expiry=yyyymm,
            ))
        return out


def test_back_fetch_mode_writes_csv_with_mocked_ib(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: --back-fetch enumerates monthly contracts, qualifies
    each by YYYYMM, fetches per-contract chunks, stitches and writes CSV.
    """
    ib = _BackFetchIB(synthetic_bars_per_chunk=5)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "90",  # ~4 monthly contracts
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
            "--back-fetch",
        ],
        ib=ib,
    )
    assert rc == 0
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()

    # qualifyContracts called per contract (not just once).
    assert len(ib.qualify_calls) >= 3
    # Each call had a YYYYMM-pinned contract.
    assert all(len(yyyymm) == 6 for yyyymm in ib.qualified_yyyymm)
    # Every qualified contract had includeExpired=True (back-fetch needs
    # expired contracts).
    for c in ib.qualify_calls:
        assert getattr(c, "includeExpired", False) is True

    # Bars written.
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 5  # at least one contract's bars made it in


def test_back_fetch_skips_unlisted_contract_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If a historical contract isn't listed by IBKR (e.g. very old or
    not yet listed), the back-fetcher logs and continues.
    """
    # Pretend Feb 2026 isn't listed -- everything else is.
    ib = _BackFetchIB(
        synthetic_bars_per_chunk=5,
        unlisted_months={(2026, 2)},
    )
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "90",
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
            "--back-fetch",
        ],
        ib=ib,
    )
    assert rc == 0
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()


def test_back_fetch_with_adjust_produces_continuous_csv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--back-fetch --adjust produces a price-continuous series.

    Each contract's chunk reports prices offset by chunk_index, so
    rolls produce price discontinuities by construction. With --adjust,
    the older contract's OHLC must be shifted to make the price series
    continuous.
    """
    ib = _BackFetchIB(synthetic_bars_per_chunk=3)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "90",
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
            "--back-fetch",
            "--adjust",
        ],
        ib=ib,
    )
    assert rc == 0
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()

    # Output is still canonical CSV format (load_ohlcv expectations).
    with csv_path.open() as f:
        header = next(csv.reader(f))
    assert header == ["time", "open", "high", "low", "close", "volume"]


def test_back_fetch_dry_run_prints_per_contract_plan(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    """--dry-run --back-fetch prints the per-contract enumeration
    instead of the legacy single-front-month plan.
    """
    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "540",
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--dry-run",
            "--back-fetch",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "back-fetch" in out
    assert "MBT" in out
    assert "monthly" in out
    # 540d / 1 contract per month -> 18-19 contracts listed.
    assert any(f"{y}" in out for y in (2024, 2025, 2026))


def test_back_fetch_dry_run_quarterly_symbol(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    """Quarterly symbols (MNQ) enumerate fewer contracts in --back-fetch
    dry-run -- 540d / quarterly = ~6 contracts vs ~18 for monthly.
    """
    rc = mod.run(
        [
            "--symbols", "MNQ",
            "--days", "540",
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--dry-run",
            "--back-fetch",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "quarterly" in out


def test_legacy_front_month_only_still_works_no_regression(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--back-fetch is opt-in; default behavior unchanged.

    This is the regression guard: the legacy front-month-only flow that
    fetch_chunks() implements must still produce a CSV when --back-fetch
    is NOT passed.
    """
    ib = _MockIB(synthetic_bars_per_chunk=5)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    rc = mod.run(
        [
            "--symbols", "MBT",
            "--days", "60",  # 2 chunks -- legacy mode
            "--end", "2026-05-07",
            "--root", str(tmp_path),
            "--pacing-sleep", "0",
            # NO --back-fetch -- this exercises the unchanged legacy path.
        ],
        ib=ib,
    )
    assert rc == 0
    csv_path = tmp_path / "MBT1_5m.csv"
    assert csv_path.exists()
    # qualifyContracts was called exactly ONCE (legacy front-month only).
    assert len(ib.qualify_calls) == 1


def test_back_fetch_csv_format_unchanged() -> None:
    """The canonical CSV header must remain time,open,high,low,close,volume
    so the lab harness signals_*  adapters (load_ohlcv) keep working.
    """
    # Write an in-memory back-fetch CSV via merge_with_existing -> write_csv.
    rows = [
        {"time": 1000, "open": 50000.0, "high": 50100.0, "low": 49900.0,
         "close": 50050.0, "volume": 10.0},
    ]
    import io  # noqa: PLC0415 -- test-local
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "open", "high", "low", "close", "volume"])
    for r in rows:
        w.writerow([r["time"], r["open"], r["high"], r["low"],
                    r["close"], r["volume"]])
    buf.seek(0)
    header = next(csv.reader(buf))
    assert header == ["time", "open", "high", "low", "close", "volume"]


def test_parser_accepts_back_fetch_and_adjust_flags() -> None:
    parser = mod.build_parser()
    args = parser.parse_args(["--back-fetch"])
    assert args.back_fetch is True
    assert args.adjust is False

    args = parser.parse_args(["--back-fetch", "--adjust"])
    assert args.back_fetch is True
    assert args.adjust is True

    # Default: both off (regression guard).
    args = parser.parse_args([])
    assert args.back_fetch is False
    assert args.adjust is False


