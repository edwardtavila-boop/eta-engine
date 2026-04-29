from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.scripts import run_research_grid, workspace_roots


def _cell() -> run_research_grid.ResearchCell:
    return run_research_grid.ResearchCell(
        label="test",
        symbol="BTC",
        timeframe="1h",
        scorer_name="btc",
        threshold=0.0,
        block_regimes=None,
        window_days=30,
        step_days=15,
        min_trades_per_window=5,
    )


def _result(*, pass_gate: bool = False, n_windows: int = 0, note: str = "NO_DATA: BTC/1h"):
    return run_research_grid.CellResult(
        cell=_cell(),
        n_windows=n_windows,
        n_positive_oos=0,
        agg_is_sharpe=0.0,
        agg_oos_sharpe=0.0,
        avg_oos_degradation=0.0,
        deflated_sharpe=0.0,
        fold_dsr_median=0.0,
        fold_dsr_pass_fraction=0.0,
        pass_gate=pass_gate,
        note=note,
    )


def test_research_grid_classifies_promotable_runs_for_docs() -> None:
    artifact_class = run_research_grid.classify_research_results([_result(pass_gate=True, n_windows=3)])

    assert artifact_class == "promotable"
    assert run_research_grid.resolve_report_dir(artifact_class=artifact_class) == (
        run_research_grid.ROOT / "docs" / "research_log"
    )


def test_research_grid_routes_no_data_runs_to_runtime_state() -> None:
    artifact_class = run_research_grid.classify_research_results([_result()])

    assert artifact_class == "no_data"
    assert run_research_grid.resolve_report_dir(artifact_class=artifact_class) == (
        workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR
    )


def test_research_grid_routes_low_signal_runs_to_runtime_state() -> None:
    artifact_class = run_research_grid.classify_research_results(
        [_result(n_windows=0, note="1864 bars / 9d")]
    )

    assert artifact_class == "low_signal"
    assert run_research_grid.resolve_report_dir(artifact_class=artifact_class) == (
        workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR
    )


def test_research_grid_report_records_artifact_class() -> None:
    table = run_research_grid.render_table([_result()])
    report = run_research_grid.render_report(
        matrix=[_cell()],
        results=[_result()],
        table=table,
        generated_at=datetime(2026, 4, 29, tzinfo=UTC),
        artifact_class="no_data",
    )

    assert "Artifact class: `no_data`" in report
    assert "| test | BTC/1h |" in report


def test_research_grid_report_path_uses_microseconds(tmp_path) -> None:
    path = run_research_grid.build_report_path(
        tmp_path,
        datetime(2026, 4, 29, 16, 2, 29, 123456, tzinfo=UTC),
    )

    assert path.name == "research_grid_20260429_160229_123456.md"


def test_daily_sage_provider_wrapper_attaches_provider(monkeypatch) -> None:
    provider = object()
    calls = []

    def fake_provider(symbol: str, instrument_class: str = "crypto") -> object:
        calls.append((symbol, instrument_class))
        return provider

    class FakeStrategy:
        attached_provider: object | None = None

        def attach_daily_verdict_provider(self, value: object) -> None:
            self.attached_provider = value

    monkeypatch.setattr(run_research_grid, "_get_daily_sage_provider", fake_provider)

    factory = run_research_grid._with_daily_sage_provider(
        FakeStrategy,
        symbol="ETH",
        instrument_class="crypto",
    )
    strategy = factory()

    assert isinstance(strategy, FakeStrategy)
    assert strategy.attached_provider is provider
    assert calls == [("ETH", "crypto")]


def test_daily_sage_provider_wrapper_allows_strategies_without_hook(monkeypatch) -> None:
    provider = object()
    strategy = object()

    monkeypatch.setattr(
        run_research_grid,
        "_get_daily_sage_provider",
        lambda symbol, instrument_class="crypto": provider,
    )

    factory = run_research_grid._with_daily_sage_provider(
        lambda: strategy,
        symbol="BTC",
    )

    assert factory() is strategy


def test_parse_bot_filter_returns_clean_set() -> None:
    assert run_research_grid._parse_bot_filter("mnq_futures, btc_hybrid ,,") == {
        "mnq_futures",
        "btc_hybrid",
    }
    assert run_research_grid._parse_bot_filter(None) is None
    assert run_research_grid._parse_bot_filter(" , ") is None


def test_limit_matrix_filters_and_caps_cells() -> None:
    cells = [
        run_research_grid.ResearchCell(
            label=label,
            symbol="BTC",
            timeframe="1h",
            scorer_name="btc",
            threshold=0.0,
            block_regimes=None,
            window_days=30,
            step_days=15,
            min_trades_per_window=5,
        )
        for label in ("a", "b", "c")
    ]

    out = run_research_grid._limit_matrix(
        cells,
        bots={"a", "c"},
        max_cells=1,
    )

    assert [cell.label for cell in out] == ["a"]


def test_registry_matrix_skips_deactivated_by_default() -> None:
    labels = {cell.label for cell in run_research_grid._matrix_from_registry()}
    labels_with_muted = {
        cell.label
        for cell in run_research_grid._matrix_from_registry(include_deactivated=True)
    }

    assert "xrp_perp" not in labels
    assert "xrp_perp" in labels_with_muted
