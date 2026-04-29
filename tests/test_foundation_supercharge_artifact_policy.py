from __future__ import annotations

from pathlib import Path

from eta_engine.scripts.run_foundation_supercharge_sweep import (
    DEFAULT_ASSETS,
    DEFAULT_OUT_JSON,
    DEFAULT_STRATEGIES,
    _resolve_out_json,
)


def test_full_default_sweep_writes_canonical_aggregate() -> None:
    assert (
        _resolve_out_json(None, list(DEFAULT_ASSETS), list(DEFAULT_STRATEGIES))
        == DEFAULT_OUT_JSON
    )


def test_scoped_sweep_uses_timestamped_artifact_by_default() -> None:
    out_json = _resolve_out_json(None, ["BTC"], ["compression"])

    assert out_json.parent == DEFAULT_OUT_JSON.parent
    assert out_json != DEFAULT_OUT_JSON
    assert out_json.name.startswith(
        "foundation_supercharge_sweep_results_btc_compression_",
    )
    assert out_json.suffix == ".json"


def test_explicit_out_json_is_respected_for_scoped_sweeps() -> None:
    explicit = Path("scratch") / "btc_only.json"

    assert _resolve_out_json(explicit, ["BTC"], ["compression"]) == explicit
