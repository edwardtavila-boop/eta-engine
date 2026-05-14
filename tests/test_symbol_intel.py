from pathlib import Path

from eta_engine.scripts import workspace_roots


def test_symbol_intelligence_paths_stay_under_workspace():
    root = workspace_roots.WORKSPACE_ROOT.resolve()
    data_lake = workspace_roots.ETA_DATA_LAKE_ROOT.resolve()
    snapshot = workspace_roots.ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH.resolve()

    assert str(data_lake).startswith(str(root))
    assert str(snapshot).startswith(str(root))
    assert data_lake == root / "var" / "eta_engine" / "data_lake"
    assert snapshot == root / "var" / "eta_engine" / "state" / "symbol_intelligence_latest.json"
