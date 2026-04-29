from __future__ import annotations

from pathlib import Path

from scripts import lint_stale_paths


def _write_sample(tmp_path: Path, line: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(f"ROOT = {line!r}\n", encoding="utf-8")
    return path


def _local_runtime_root(sep: str, *, env_style: str) -> str:
    local = "LOCAL" + "APPDATA"
    engine = "eta_" + "engine"
    if env_style == "percent":
        return "%" + local + "%" + sep + engine + sep + "state"
    if env_style == "powershell":
        return "$env:" + local + sep + engine + sep + "state"
    raise AssertionError(f"unknown env style: {env_style}")


def _c_runtime_root(name: str, sep: str) -> str:
    return "C:" + sep + name + sep + "state"


def test_scan_file_blocks_current_forbidden_runtime_roots(tmp_path: Path) -> None:
    forbidden_roots = []
    for sep in ("\\", "/"):
        forbidden_roots.extend((
            _local_runtime_root(sep, env_style="percent"),
            _local_runtime_root(sep, env_style="powershell"),
            _c_runtime_root("mnq_" + "data", sep),
            _c_runtime_root("crypto_" + "data", sep),
            _c_runtime_root("The" + "Firm", sep),
            _c_runtime_root("The_" + "Firm", sep),
        ))

    for root in forbidden_roots:
        path = _write_sample(tmp_path, root)
        violations = lint_stale_paths.scan_file(path)
        assert violations, f"expected stale-path violation for {root!r}"


def test_scan_file_allows_same_line_historical_marker(tmp_path: Path) -> None:
    root = _c_runtime_root("mnq_" + "data", "\\")
    path = tmp_path / "sample.py"
    path.write_text(
        f"ROOT = {root!r}  # {lint_stale_paths.EXEMPT_LINE_MARKER}\n",
        encoding="utf-8",
    )

    assert lint_stale_paths.scan_file(path) == []


def test_intentional_detection_fixture_files_are_exempt() -> None:
    assert lint_stale_paths.is_exempt(Path("tests") / "test_lint_stale_paths.py")
    assert lint_stale_paths.is_exempt(Path("tests") / "test_workspace_path_cleanup.py")
    assert lint_stale_paths.is_exempt(Path("tests") / "test_data_library.py")
    assert not lint_stale_paths.is_exempt(Path("scripts") / "runtime_writer.py")


def test_active_data_operator_docs_do_not_advertise_forbidden_runtime_roots() -> None:
    root = Path(__file__).resolve().parent.parent
    targets = (
        "data/requirements.py",
        "docs/JARVIS_FULL_ACTIVATION.md",
        "scripts/compare_coinbase_vs_ibkr.py",
        "scripts/extend_nq_daily_yahoo.py",
        "scripts/fetch_btc_bars.py",
        "scripts/fetch_funding_rates.py",
        "scripts/fetch_ibkr_crypto_bars.py",
        "scripts/fetch_onchain_history.py",
        "scripts/fetch_xrp_news_history.py",
        "scripts/run_walk_forward_mnq_real.py",
    )

    offenders = {}
    for target in targets:
        violations = lint_stale_paths.scan_file(root / target)
        if violations:
            offenders[target] = violations

    assert offenders == {}
