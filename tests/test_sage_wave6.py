"""Tests for wave-6 sage close-out (2026-04-27).

Covers:
  * sage_train_ml: feature builder + label semantics + insufficient-samples
  * onchain_fetcher: cache hits + unsupported symbols + offline failure
  * feature_cache: get_or_compute caching + clear_for_ctx
"""
from __future__ import annotations

# ─── ML training script: feature/label builders ──────────────────


def _bars(n: int, *, slope: float = 5.0, base: float = 21000) -> list[dict]:
    out = []
    for i in range(n):
        c = base + slope * i
        out.append({
            "open": c - 1, "high": c + 5, "low": c - 5, "close": c,
            "volume": 1000 + i * 5, "ts": f"2026-04-27T{i:02d}:00:00Z",
        })
    return out


def test_train_ml_features_shape() -> None:
    from eta_engine.scripts.sage_train_ml import _features_from_window
    bars = _bars(60)
    feats = _features_from_window(bars[-50:])
    assert len(feats) == 5
    assert all(isinstance(x, float) for x in feats)


def test_train_ml_dataset_builds_correct_labels() -> None:
    from eta_engine.scripts.sage_train_ml import _build_dataset
    bars = _bars(200, slope=10.0)  # strong uptrend
    X, y = _build_dataset(bars, window=50, forward_bars=50, min_abs_return=0.001)  # noqa: N806
    assert len(X) == len(y)
    # All-uptrend bars should give all-positive labels (forward return > 0)
    assert sum(y) == len(y)


def test_train_ml_dataset_drops_nonactionable_bars() -> None:
    from eta_engine.scripts.sage_train_ml import _build_dataset
    flat = _bars(150, slope=0.0)  # totally flat -> all forward returns ~0
    X, y = _build_dataset(flat, window=50, forward_bars=50, min_abs_return=0.001)  # noqa: N806
    # Most/all dropped because |fwd_ret| < 0.001
    assert len(X) <= 5  # nearly nothing actionable


# ─── On-chain fetcher: contract + cache ──────────────────────────


def test_onchain_fetcher_returns_empty_for_unsupported_symbol() -> None:
    from eta_engine.brain.jarvis_v3.sage.onchain_fetcher import (
        clear_cache,
        fetch_onchain,
    )
    clear_cache()
    out = fetch_onchain("DOGEUSDT")  # not BTC/ETH
    assert out == {}


def test_onchain_fetcher_cache_hits_on_repeat(monkeypatch) -> None:
    """Verify that two calls within TTL return the same dict (cached)."""
    from eta_engine.brain.jarvis_v3.sage import onchain_fetcher
    onchain_fetcher.clear_cache()

    fake_calls = [0]
    def fake_btc():
        fake_calls[0] += 1
        return {"fees_fastest_sats_vb": 50, "mempool_count": 12345}

    monkeypatch.setattr(onchain_fetcher, "_btc_onchain", fake_btc)

    a = onchain_fetcher.fetch_onchain("BTCUSDT")
    b = onchain_fetcher.fetch_onchain("BTCUSDT")
    # cache hit -> _btc_onchain only called once
    assert fake_calls[0] == 1
    assert a == b
    assert a["fees_fastest_sats_vb"] == 50


def test_onchain_fetcher_force_refresh_invalidates_cache(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3.sage import onchain_fetcher
    onchain_fetcher.clear_cache()

    fake_calls = [0]
    def fake_btc():
        fake_calls[0] += 1
        return {"fees_fastest_sats_vb": fake_calls[0]}

    monkeypatch.setattr(onchain_fetcher, "_btc_onchain", fake_btc)

    a = onchain_fetcher.fetch_onchain("BTCUSDT")
    b = onchain_fetcher.fetch_onchain("BTCUSDT", force_refresh=True)
    assert fake_calls[0] == 2
    assert a["fees_fastest_sats_vb"] == 1
    assert b["fees_fastest_sats_vb"] == 2


def test_onchain_fetcher_handles_offline_gracefully(monkeypatch) -> None:
    """All HTTP fetches return None -> output is mostly empty but not crashing."""
    from eta_engine.brain.jarvis_v3.sage import onchain_fetcher
    onchain_fetcher.clear_cache()
    monkeypatch.setattr(onchain_fetcher, "_http_json", lambda url, timeout=5.0: None)
    out = onchain_fetcher.fetch_onchain("BTCUSDT", force_refresh=True)
    # Even with no upstream data, we return source + ts metadata
    assert "_source" in out
    assert "_fetched_at" in out


# ─── Feature cache: get_or_compute ───────────────────────────────


def test_feature_cache_get_or_compute_only_computes_once() -> None:
    from eta_engine.brain.jarvis_v3.sage.base import MarketContext
    from eta_engine.brain.jarvis_v3.sage.feature_cache import (
        clear_for_ctx,
        get_or_compute,
    )

    ctx = MarketContext(bars=[{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
                        side="long")
    calls = [0]
    def expensive():
        calls[0] += 1
        return 42

    a = get_or_compute(ctx, "k", expensive)
    b = get_or_compute(ctx, "k", expensive)
    assert a == b == 42
    assert calls[0] == 1
    # cleanup
    clear_for_ctx(ctx)


def test_feature_cache_isolates_per_ctx() -> None:
    from eta_engine.brain.jarvis_v3.sage.base import MarketContext
    from eta_engine.brain.jarvis_v3.sage.feature_cache import (
        clear_for_ctx,
        get_or_compute,
    )

    ctx1 = MarketContext(bars=[], side="long")
    ctx2 = MarketContext(bars=[], side="long")
    calls = [0]
    def expensive():
        calls[0] += 1
        return calls[0]

    a = get_or_compute(ctx1, "k", expensive)
    b = get_or_compute(ctx2, "k", expensive)
    # Different ctxs -> two computes
    assert calls[0] == 2
    assert a != b
    clear_for_ctx(ctx1)
    clear_for_ctx(ctx2)


def test_feature_cache_clear_for_ctx_returns_count() -> None:
    from eta_engine.brain.jarvis_v3.sage.base import MarketContext
    from eta_engine.brain.jarvis_v3.sage.feature_cache import (
        clear_for_ctx,
        get_or_compute,
    )

    ctx = MarketContext(bars=[], side="long")
    get_or_compute(ctx, "a", lambda: 1)
    get_or_compute(ctx, "b", lambda: 2)
    n = clear_for_ctx(ctx)
    assert n == 2


# ─── CLI smoke ──────────────────────────────────────────────────


def test_cli_jarvis_sage_list_schools_runs(capsys) -> None:
    """Smoke-test that the --list-schools flag works without bars."""
    from eta_engine.scripts import jarvis_sage
    rc = jarvis_sage.main(["--list-schools"])
    captured = capsys.readouterr()
    assert rc == 0
    # Should mention several school NAMEs in the dump
    out = captured.out
    assert "dow_theory" in out
    assert "wyckoff" in out
    assert "smc_ict" in out
