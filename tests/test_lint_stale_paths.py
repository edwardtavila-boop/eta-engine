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


def _onedrive_root(sep: str) -> str:
    return "C:" + sep + "Users" + sep + "edwar" + sep + "OneDrive" + sep + "Anything"


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
            _onedrive_root(sep),
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


# ---------------------------------------------------------------------------
# New patterns added 2026-05-04 from LEGACY_PATH_AUDIT.md (in-repo state file
# violations the existing patterns missed). Each test asserts that the
# detection regex catches the bad shape AND lets the canonical shape through.
# ---------------------------------------------------------------------------


def test_in_repo_eta_engine_state_is_blocked(tmp_path: Path) -> None:
    """Bare ``eta_engine/state/`` (in-repo) must be flagged."""
    cases = (
        'p = "eta_engine/state/foo.json"',
        'p = "eta_engine\\\\state\\\\foo.json"',
        '_DEFAULT_STATE = ROOT / "state" / "eval" / "promptfoo_results.json"  # eta_engine/state/',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert violations, f"expected stale-path violation for {line!r}"


def test_canonical_var_eta_engine_state_is_allowed(tmp_path: Path) -> None:
    """Canonical ``var/eta_engine/state/`` must NOT be flagged."""
    cases = (
        'p = "var/eta_engine/state/foo.json"',
        'p = "var\\\\eta_engine\\\\state\\\\foo.json"',
        'p = WORKSPACE_ROOT / "var" / "eta_engine" / "state"',
        'p = "C:/EvolutionaryTradingAlgo/var/eta_engine/state/promotion.json"',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert not violations, (
            f"canonical path was flagged (false positive): {line!r} -> {violations}"
        )


def test_repo_root_state_idiom_is_blocked(tmp_path: Path) -> None:
    """``_REPO_ROOT / 'state'`` -> writes to in-repo state/ (audit B1/B3)."""
    cases = (
        '_DEFAULT_STATE = _REPO_ROOT / "state"',
        "_DEFAULT_STATE = _REPO_ROOT / 'state'",
        'state_dir = _REPO_ROOT/"state"',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert violations, f"expected violation for {line!r}"


def test_repo_root_with_other_subdirs_is_allowed(tmp_path: Path) -> None:
    """Only ``_REPO_ROOT / 'state'`` is bad -- other subdirs are fine."""
    cases = (
        '_DEFAULT_DOCS = _REPO_ROOT / "docs"',
        '_DEFAULT_LOG = _REPO_ROOT / "logs"',
        '_DEFAULT_DATA = _REPO_ROOT / "data"',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert not violations, f"unexpected violation for {line!r}: {violations}"


def test_firm_eta_engine_state_is_blocked(tmp_path: Path) -> None:
    """``firm/eta_engine/state/`` must be flagged (audit B9)."""
    cases = (
        'p = "firm/eta_engine/state/hermes/seen.json"',
        'p = "firm\\\\eta_engine\\\\state\\\\kaizen\\\\bandit.json"',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert violations, f"expected violation for {line!r}"


def test_firm_eta_engine_data_is_separate_concern(tmp_path: Path) -> None:
    """``firm/eta_engine/data/`` is OUT of scope (legacy fixture data)."""
    cases = (
        'p = "firm/eta_engine/data/backtest_real_daily.json"',
        'p = "firm\\\\eta_engine\\\\data\\\\bars\\\\nq.csv"',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert not violations, (
            f"firm/eta_engine/data should be allowed (separate concern): "
            f"{line!r} -> {violations}"
        )


def test_firm_command_center_eta_engine_is_blocked(tmp_path: Path) -> None:
    """``firm_command_center\\eta_engine`` is a stale VPS-side path."""
    cases = (
        'p = "firm_command_center/eta_engine/scripts/x.py"',
        'p = r"firm_command_center\\eta_engine\\scripts\\x.py"',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert violations, f"expected violation for {line!r}"


def test_apex_predator_venv_is_blocked(tmp_path: Path) -> None:
    """``apex_predator/.venv`` references must be blocked (audit A2/A4/A5)."""
    cases = (
        'p = "C:/TheFirm/apex_predator/.venv/Scripts/python.exe"',
        'p = r"apex_predator\\.venv\\Scripts\\python.exe"',
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert violations, f"expected violation for {line!r}"


def test_eta_engine_attribute_access_is_not_flagged(tmp_path: Path) -> None:
    """``eta_engine.state`` (Python attribute access) must not match.

    The negative lookbehind ``(?<!\\.)`` blocks the false positive that
    would otherwise fire on attribute-access expressions and module imports.
    """
    cases = (
        "from eta_engine.state import foo",
        "x = obj.eta_engine.state",
    )
    for line in cases:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        # eta_engine.state should not be matched as a path
        labels = [v[1] for v in violations]
        assert (
            "in-repo eta_engine/state/ (use var/eta_engine/state/)" not in labels
        ), f"false positive on attribute access: {line!r} -> {violations}"


def test_synthetic_dual_path_file_only_flags_legacy_line(tmp_path: Path) -> None:
    """A file containing both the legacy AND canonical paths reports only
    the legacy line (not the canonical one).
    """
    path = tmp_path / "dual_paths.py"
    path.write_text(
        "BAD  = 'eta_engine/state/foo.json'\n"
        "GOOD = 'var/eta_engine/state/foo.json'\n",
        encoding="utf-8",
    )
    violations = lint_stale_paths.scan_file(path)
    assert len(violations) == 1, (
        f"expected exactly one violation (the legacy line) -- got {violations}"
    )
    assert violations[0][0] == 1  # line 1 is the legacy line


def test_allowlisted_dual_path_files_are_exempt() -> None:
    """Files in the explicit dual-path allow-list must be is_exempt()=True."""
    for entry in lint_stale_paths.ALLOWLISTED_DUAL_PATH_FILES:
        # Forward-slash variant
        assert lint_stale_paths.is_exempt(Path(entry)), (
            f"allow-list entry not exempt: {entry!r}"
        )
        # Workspace-rooted variant (suffix match)
        assert lint_stale_paths.is_exempt(
            Path("/some/workspace/root") / entry
        ), f"allow-list entry not exempt at workspace root: {entry!r}"


def test_audit_doc_is_exempt() -> None:
    """``LEGACY_PATH_AUDIT.md`` mentions every legacy path by design."""
    assert lint_stale_paths.is_exempt(Path("docs") / "LEGACY_PATH_AUDIT.md")


def test_list_violations_mode_returns_zero_when_clean(tmp_path: Path, capsys) -> None:
    """``--list-violations`` against a clean dir exits 0 with a friendly note."""
    clean = tmp_path / "clean.py"
    clean.write_text('p = "var/eta_engine/state/foo.json"\n', encoding="utf-8")
    rc = lint_stale_paths.main(
        ["lint_stale_paths.py", "--list-violations", str(clean)]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "no violations found" in captured.out


def test_list_violations_mode_returns_one_when_dirty(tmp_path: Path, capsys) -> None:
    """``--list-violations`` against a dirty file exits 1 (preserves semantic)."""
    dirty = tmp_path / "dirty.py"
    dirty.write_text('p = "eta_engine/state/foo.json"\n', encoding="utf-8")
    rc = lint_stale_paths.main(
        ["lint_stale_paths.py", "--list-violations", str(dirty)]
    )
    assert rc == 1


def test_fix_mode_is_noop_for_path_patterns(tmp_path: Path, capsys) -> None:
    """``--fix`` must not modify the file (path migrations are too risky).

    Exit code semantic stays the same as without --fix: violations -> 1.
    """
    dirty = tmp_path / "dirty.py"
    original = 'p = "eta_engine/state/foo.json"\n'
    dirty.write_text(original, encoding="utf-8")
    rc = lint_stale_paths.main(["lint_stale_paths.py", "--fix", str(dirty)])
    assert rc == 1, "--fix must still report the violation, just not auto-rewrite"
    assert dirty.read_text(encoding="utf-8") == original, (
        "--fix must NOT modify the file"
    )
    captured = capsys.readouterr()
    assert "NO-OP" in captured.err


def test_existing_legacy_patterns_still_blocked(tmp_path: Path) -> None:
    """Regression: the audit's already-blocked patterns must still trip.

    Hard-constraint: the linter's exit-code semantics for existing patterns
    must not change (operators may have automation depending on it).
    """
    legacy_lines = (
        r'p = r"C:\TheFirm\apex_predator"',
        r'p = "OneDrive\The_Firm\foo.txt"',
        r'p = "%LOCALAPPDATA%\eta_engine\state"',
    )
    for line in legacy_lines:
        path = tmp_path / "sample.py"
        path.write_text(line + "\n", encoding="utf-8")
        violations = lint_stale_paths.scan_file(path)
        assert violations, (
            f"regression: existing legacy pattern no longer blocked: {line!r}"
        )


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
