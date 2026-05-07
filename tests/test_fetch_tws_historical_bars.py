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

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        self.qualify_calls.extend(contracts)
        # Return a fake qualified contract for each input.
        return [
            _MockQualifiedContract(symbol=getattr(c, "symbol", "?"))
            for c in contracts
        ]

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


