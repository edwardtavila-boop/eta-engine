"""Tests for ``scripts.fetch_funding_rates_extended``.

All HTTP interactions are mocked via ``unittest.mock.patch`` against
``urllib.request.urlopen`` (the underlying transport for both the
CoinGlass and BitMEX adapters). No live network. No ``responses``
dependency required.

Coverage targets:

* CoinGlass JSON parsing → ``[(ts_seconds, rate), ...]``
* BitMEX JSON parsing → ``[(ts_seconds, rate), ...]``
* Idempotent merge: existing 33-day file + 365-day fetch → deduped
  365-day file, sorted ascending
* Empty / malformed responses are handled without raising
* SOL routed to BitMEX raises a clear ``UnsupportedSymbolError``
* ``coinglass`` source without an API key raises ``MissingApiKeyError``
* CSV output schema (header, epoch-seconds int, float rate, no
  future timestamps)
"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from eta_engine.scripts import workspace_roots
from eta_engine.scripts.fetch_funding_rates_extended import (
    MissingApiKeyError,
    UnsupportedSymbolError,
    fetch_funding_rates,
    main,
    merge_and_write,
)


@pytest.fixture(autouse=True)
def _allow_tmp_output_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", tmp_path.parent)

# ── HTTP mocking helpers ────────────────────────────────────────────


class _StubResponse:
    """Minimal stand-in for the ``http.client.HTTPResponse`` returned by urlopen."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _StubResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@contextmanager
def _mock_urlopen(payloads: list[object]):
    """Yield a patched urlopen that returns each entry from ``payloads`` in order.

    Entries can be either a Python object (JSON-encoded automatically) or
    a pre-built exception (raised on call).
    """
    iterator = iter(payloads)

    def _fake(req, timeout=None):  # noqa: ANN001, ANN201
        try:
            payload = next(iterator)
        except StopIteration as exc:
            raise AssertionError("urlopen called more times than mocked") from exc
        if isinstance(payload, BaseException):
            raise payload
        body = json.dumps(payload).encode("utf-8")
        return _StubResponse(body)

    with patch(
        "eta_engine.scripts.fetch_funding_rates_extended.urllib.request.urlopen",
        side_effect=_fake,
    ) as mock:
        yield mock


def _coinglass_payload(points: list[tuple[int, float]]) -> dict:
    """Build a CoinGlass-shaped success response."""
    return {
        "code": "0",
        "msg": "success",
        "data": [{"time": ts_ms, "close": rate} for ts_ms, rate in points],
    }


def _bitmex_payload(points: list[tuple[str, float]]) -> list[dict]:
    """Build a BitMEX-shaped success response."""
    return [{"timestamp": ts_iso, "symbol": "XBTUSD", "fundingRate": rate} for ts_iso, rate in points]


# ── CoinGlass adapter ────────────────────────────────────────────────


def test_coinglass_parses_response_to_seconds_and_floats():
    payload = _coinglass_payload(
        [
            (1_700_000_000_000, 0.0001234),
            (1_700_028_800_000, -0.0000567),
        ]
    )
    with _mock_urlopen([payload]):
        rows = fetch_funding_rates("BTC", days=2, source="coinglass", api_key="k")
    assert rows == [
        (1_700_000_000, 0.0001234),
        (1_700_028_800, -0.0000567),
    ]


def test_coinglass_missing_api_key_raises():
    with pytest.raises(MissingApiKeyError):
        fetch_funding_rates("BTC", days=2, source="coinglass", api_key=None)


def test_coinglass_empty_response_returns_empty_list():
    with _mock_urlopen([{"code": "0", "msg": "ok", "data": []}]):
        rows = fetch_funding_rates("ETH", days=2, source="coinglass", api_key="k")
    assert rows == []


def test_coinglass_api_error_code_returns_empty_list_without_raising():
    with _mock_urlopen([{"code": "30001", "msg": "rate limit", "data": []}]):
        rows = fetch_funding_rates("BTC", days=2, source="coinglass", api_key="k")
    assert rows == []


def test_coinglass_http_500_returns_empty_without_raising():
    err = urllib.error.HTTPError(
        url="https://x",
        code=500,
        msg="server error",
        hdrs=None,
        fp=io.BytesIO(b"oops"),
    )
    with _mock_urlopen([err]):
        rows = fetch_funding_rates("BTC", days=2, source="coinglass", api_key="k")
    assert rows == []


def test_coinglass_malformed_entries_are_skipped():
    payload = {
        "code": "0",
        "data": [
            {"time": 1_700_000_000_000, "close": 0.0001},
            {"time": "not-a-number", "close": 0.0002},  # bad ts
            {"time": 1_700_028_800_000, "close": "x"},  # bad rate
            {"time": 1_700_057_600_000, "close": 0.0003},  # ok
            {"missing": "fields"},
        ],
    }
    with _mock_urlopen([payload]):
        rows = fetch_funding_rates("BTC", days=2, source="coinglass", api_key="k")
    assert rows == [(1_700_000_000, 0.0001), (1_700_057_600, 0.0003)]


def test_coinglass_unsupported_symbol_raises():
    with pytest.raises(UnsupportedSymbolError, match="CoinGlass"):
        fetch_funding_rates("DOGE", days=1, source="coinglass", api_key="k")


# ── BitMEX adapter ───────────────────────────────────────────────────


def test_bitmex_parses_response_and_dedups_overlap():
    chunk_a = _bitmex_payload(
        [
            ("2026-01-01T00:00:00.000Z", 0.0001),
            ("2026-01-01T08:00:00.000Z", 0.0002),
        ]
    )
    # Pagination chunk covering the same span: BitMEX may return overlap.
    chunk_b = _bitmex_payload(
        [
            ("2026-01-01T08:00:00.000Z", 0.0002),  # duplicate ts
            ("2026-01-01T16:00:00.000Z", 0.0003),
        ]
    )
    # _fetch_bitmex paginates while cursor < end, but the cursor is force-
    # advanced to chunk_end after each iteration, so two pages are sufficient
    # for the 2-day window.
    with _mock_urlopen([chunk_a, chunk_b, []]):
        rows = fetch_funding_rates("BTC", days=2, source="bitmex")
    timestamps = [r[0] for r in rows]
    assert len(set(timestamps)) == len(timestamps), "BitMEX rows must be deduped"
    rates = dict(rows)
    expected_ts_a = int(datetime(2026, 1, 1, 0, 0, tzinfo=UTC).timestamp())
    expected_ts_b = int(datetime(2026, 1, 1, 8, 0, tzinfo=UTC).timestamp())
    assert rates[expected_ts_a] == pytest.approx(0.0001)
    assert rates[expected_ts_b] == pytest.approx(0.0002)


def test_bitmex_does_not_support_sol():
    with pytest.raises(UnsupportedSymbolError, match="BitMEX"):
        fetch_funding_rates("SOL", days=2, source="bitmex")


def test_bitmex_empty_response_returns_empty_list():
    # Any number of empty pages are OK — the cursor force-advances per iter.
    with _mock_urlopen([[]] * 10):
        rows = fetch_funding_rates("ETH", days=1, source="bitmex")
    assert rows == []


# ── merge / dedup contract ───────────────────────────────────────────


def _write_csv(path: Path, rows: list[tuple[int, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "funding_rate"])
        for ts, rate in rows:
            w.writerow([ts, f"{rate:.10f}"])


def _read_csv(path: Path) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            out.append((int(row["time"]), float(row["funding_rate"])))
    return out


def test_merge_existing_33d_plus_365d_backfill_deduplicates(tmp_path: Path):
    # Simulate the production state: 33 days × 3 fundings/day = 99 rows.
    # In practice the existing CSV has dups (199 lines incl. dups); we model
    # both clean and dup-tolerant merge.
    base_ts = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp())
    existing = [(base_ts + i * 8 * 3600, 0.0001) for i in range(99)]
    out_path = tmp_path / "BTCFUND_8h.csv"
    _write_csv(out_path, existing)

    # Fetched 365-day backfill spanning a window that fully covers existing
    # plus extends backwards. 365 days × 3 = 1095 rows.
    backfill_start = base_ts - 365 * 86400
    fetched = [(backfill_start + i * 8 * 3600, 0.00005) for i in range(1095 + 99)]
    before, added, after = merge_and_write(out_path, fetched)

    assert before == 99
    # All fetched timestamps were added EXCEPT those overlapping existing.
    overlap = sum(1 for ts, _ in fetched if any(ts == e_ts for e_ts, _ in existing))
    assert added == len(fetched) - overlap
    assert after == before + added

    # On-disk file is sorted ascending and has no duplicate timestamps.
    written = _read_csv(out_path)
    timestamps = [r[0] for r in written]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)


def test_merge_idempotent_second_run_adds_nothing(tmp_path: Path):
    out_path = tmp_path / "ETHFUND_8h.csv"
    fetched = [(1_700_000_000 + i * 8 * 3600, 0.0001 * i) for i in range(10)]
    before1, added1, after1 = merge_and_write(out_path, fetched)
    assert before1 == 0 and added1 == 10 and after1 == 10
    # Re-run with the same fetch — must add zero new rows.
    before2, added2, after2 = merge_and_write(out_path, fetched)
    assert before2 == 10 and added2 == 0 and after2 == 10


def test_merge_drops_future_timestamps(tmp_path: Path):
    out_path = tmp_path / "BTCFUND_8h.csv"
    far_future = int((datetime.now(tz=UTC) + timedelta(days=365)).timestamp())
    past = int((datetime.now(tz=UTC) - timedelta(days=1)).timestamp())
    fetched = [(past, 0.0001), (far_future, 0.0002)]
    _, added, after = merge_and_write(out_path, fetched)
    # Future ts is dropped; only the past row is written.
    assert added == 1
    assert after == 1
    written = _read_csv(out_path)
    assert all(ts <= int(datetime.now(tz=UTC).timestamp()) for ts, _ in written)


def test_merge_drops_nan_rates(tmp_path: Path):
    out_path = tmp_path / "BTCFUND_8h.csv"
    nan = float("nan")
    fetched = [(1_700_000_000, 0.0001), (1_700_028_800, nan)]
    _, added, _ = merge_and_write(out_path, fetched)
    assert added == 1
    written = _read_csv(out_path)
    assert len(written) == 1
    assert written[0][0] == 1_700_000_000


# ── unsupported source ───────────────────────────────────────────────


def test_unknown_source_raises():
    with pytest.raises(ValueError, match="Unknown source"):
        fetch_funding_rates("BTC", days=1, source="binance", api_key="k")


# ── CLI smoke ────────────────────────────────────────────────────────


def test_cli_main_writes_expected_csvs(tmp_path: Path):
    """End-to-end: main() reads CoinGlass payloads, merges, writes valid CSVs."""
    btc_payload = _coinglass_payload(
        [
            (1_700_000_000_000, 0.0001),
            (1_700_028_800_000, 0.0002),
        ]
    )
    eth_payload = _coinglass_payload(
        [
            (1_700_000_000_000, -0.0001),
        ]
    )
    sol_payload = _coinglass_payload([])  # empty for SOL — exit code 1

    with _mock_urlopen([btc_payload, eth_payload, sol_payload]):
        rc = main(
            [
                "--symbols",
                "BTC",
                "ETH",
                "SOL",
                "--days",
                "30",
                "--source",
                "coinglass",
                "--api-key",
                "fake-key",
                "--root",
                str(tmp_path),
            ]
        )
    # Empty response counts as a soft failure: rc >= 1.
    assert rc >= 1
    btc_csv = tmp_path / "BTCFUND_8h.csv"
    eth_csv = tmp_path / "ETHFUND_8h.csv"
    assert btc_csv.exists()
    assert eth_csv.exists()
    btc_rows = _read_csv(btc_csv)
    assert btc_rows[0] == (1_700_000_000, pytest.approx(0.0001))
    assert btc_rows[1] == (1_700_028_800, pytest.approx(0.0002))


def test_cli_main_rejects_output_root_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)

    with pytest.raises(SystemExit) as exc:
        main(["--symbols", "BTC", "--root", str(outside_workspace)])

    assert exc.value.code == 2


def test_cli_main_aborts_when_coinglass_without_api_key(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CRYPTO_FUNDING_API_KEY", raising=False)
    rc = main(
        [
            "--symbols",
            "BTC",
            "--days",
            "30",
            "--source",
            "coinglass",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 3


def test_cli_main_with_bitmex_skips_sol_with_clear_error(tmp_path: Path, caplog):
    btc_chunk = _bitmex_payload([("2026-04-01T00:00:00.000Z", 0.0001)])
    eth_chunk = _bitmex_payload([("2026-04-01T00:00:00.000Z", 0.0002)])
    # For a 1-day window the BitMEX loop fires exactly once per symbol
    # (chunk_end immediately reaches end_dt because chunk_days=150). One
    # mock response per symbol is sufficient.
    with _mock_urlopen([btc_chunk, eth_chunk]):
        rc = main(
            [
                "--symbols",
                "BTC",
                "ETH",
                "SOL",
                "--days",
                "1",
                "--source",
                "bitmex",
                "--root",
                str(tmp_path),
            ]
        )
    # SOL is unsupported on BitMEX → rc=2 (continued past it without aborting).
    assert rc == 2
    assert (tmp_path / "BTCFUND_8h.csv").exists()
    assert (tmp_path / "ETHFUND_8h.csv").exists()
    assert not (tmp_path / "SOLFUND_8h.csv").exists()
