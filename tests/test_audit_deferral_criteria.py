"""Tests for ``scripts/_audit_deferral_criteria.py``.

Process gap #2 closure (Red Team v0.1.64 review). The audit walks
production source for "v0.2.x" / "deferred to" / "TODO(vX.Y.Z)"
markers and checks whether each marker has an exit criterion in its
+/- 5-line context window.

Covered:
  * marker detection (all 6 supported phrasings)
  * criterion detection (all 9 supported pin patterns)
  * paired hits (marker + nearby criterion -> has_criterion=True)
  * lone hits (marker without criterion -> has_criterion=False)
  * exit codes (0 ok, 1 with --strict if bare markers exist)
  * exclusion of tests/, scripts/_legacy_bumps/, docs/_backups/
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts._audit_deferral_criteria import (  # noqa: E402
    _CRITERION_RE,
    _MARKER_RE,
    _scan_file,
    main,
    scan,
)


class TestMarkerRegex:
    @pytest.mark.parametrize(
        "phrasing",
        [
            "deferred to v0.2.x",
            "deferred to v1.5.0",
            "defer to v0.2.x",
            "v0.2.x scope",
            "v0.2.x deferral",
            "v0.2.x design",
            "punted to v0.2.x",
            "TODO(v0.2.x)",
        ],
    )
    def test_marker_phrasings_all_match(self, phrasing: str):
        assert _MARKER_RE.search(phrasing), f"failed: {phrasing!r}"

    def test_random_todo_does_not_match(self):
        # We deliberately do NOT match generic "TODO" without a version
        # tag -- otherwise the audit would drown in noise.
        assert _MARKER_RE.search("# TODO: refactor this") is None
        assert _MARKER_RE.search("# FIXME later") is None

    def test_v_prefix_optional(self):
        assert _MARKER_RE.search("deferred to 0.2.x")
        assert _MARKER_RE.search("deferred to v0.2.x")


class TestCriterionRegex:
    @pytest.mark.parametrize(
        "phrase",
        [
            "KZN-42",
            "test_calibrator_runs",
            "exit criterion: ...",
            "acceptance criteria",
            "lands when calibrator ships",
            "closes when KZN-42 lands",
            "closed in v0.1.64",
            "addressed in commit abc123",
            "addressed by issue #42",
            "issue #42",
            "scope ticket KZN-42",
            "see docs/runbooks/foo.md",
        ],
    )
    def test_criterion_phrases_all_match(self, phrase: str):
        assert _CRITERION_RE.search(phrase), f"failed: {phrase!r}"


class TestScanFile:
    def test_marker_with_nearby_criterion_marked_pinned(self, tmp_path: Path):
        f = tmp_path / "src.py"
        f.write_text(
            "# This feature is deferred to v0.2.x.\n# Lands when KZN-42 ships.\n",
            encoding="utf-8",
        )
        hits = _scan_file(f, root=tmp_path)
        assert len(hits) == 1
        assert hits[0].has_criterion is True

    def test_lone_marker_without_criterion_is_bare(self, tmp_path: Path):
        f = tmp_path / "src.py"
        f.write_text(
            "# This is deferred to v0.2.x.\nx = 1\n",
            encoding="utf-8",
        )
        hits = _scan_file(f, root=tmp_path)
        assert len(hits) == 1
        assert hits[0].has_criterion is False

    def test_criterion_outside_context_window_does_not_count(
        self,
        tmp_path: Path,
    ):
        # Criterion 6+ lines below the marker should NOT pin it.
        f = tmp_path / "src.py"
        body = ["# deferred to v0.2.x", *[f"# filler {i}" for i in range(6)], "# closed in v1.0.0"]
        f.write_text("\n".join(body) + "\n", encoding="utf-8")
        hits = _scan_file(f, root=tmp_path)
        assert len(hits) == 1
        assert hits[0].has_criterion is False

    def test_no_markers_no_hits(self, tmp_path: Path):
        f = tmp_path / "src.py"
        f.write_text("x = 1\nprint('hello')\n", encoding="utf-8")
        hits = _scan_file(f, root=tmp_path)
        assert hits == []

    def test_multiple_markers_in_one_file(self, tmp_path: Path):
        f = tmp_path / "src.py"
        f.write_text(
            "# A: deferred to v0.2.x\nx = 1\n# B: another v0.2.x scope item\ny = 2\n",
            encoding="utf-8",
        )
        hits = _scan_file(f, root=tmp_path)
        assert len(hits) == 2

    def test_unreadable_file_silently_skipped(self, tmp_path: Path):
        # Path that doesn't exist -- should return empty rather than raise.
        ghost = tmp_path / "ghost.py"
        hits = _scan_file(ghost, root=tmp_path)
        assert hits == []


class TestScan:
    def test_excludes_tests_dir(self, tmp_path: Path):
        # Synthetic mini-repo with src + tests dirs
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "src" / "a.py").write_text(
            "# deferred to v0.2.x\n",
            encoding="utf-8",
        )
        (tmp_path / "tests" / "test_a.py").write_text(
            "# deferred to v0.2.x in test\n",
            encoding="utf-8",
        )
        # We pass the tmp_path as the root so the production exclusion
        # applies to "tests/" prefix.
        hits = scan(tmp_path)
        # Only src/a.py should be scanned
        assert any(h.file == "src/a.py" for h in hits)
        assert not any(h.file.startswith("tests/") for h in hits)

    def test_excludes_legacy_bumps(self, tmp_path: Path):
        (tmp_path / "scripts" / "_legacy_bumps").mkdir(parents=True)
        (tmp_path / "scripts" / "_legacy_bumps" / "v1.py").write_text(
            "# deferred to v0.2.x in legacy\n",
            encoding="utf-8",
        )
        hits = scan(tmp_path)
        assert hits == []


class TestMainCLI:
    def test_strict_with_bare_returns_1(self, tmp_path: Path, capsys):
        # Stand up a synthetic repo, rebind ROOT briefly via monkeypatch
        # of the module-level variable. This is intrusive but reliable.
        (tmp_path / "core.py").write_text(
            "# deferred to v0.2.x and no criterion in sight\n",
            encoding="utf-8",
        )
        # Monkeypatch the module's ROOT to point at tmp_path
        from eta_engine.scripts import _audit_deferral_criteria as mod

        old_root = mod.ROOT
        mod.ROOT = tmp_path
        try:
            rc = main(["--strict"])
        finally:
            mod.ROOT = old_root
        assert rc == 1
        captured = capsys.readouterr()
        assert "BARE" in captured.out

    def test_default_returns_0_even_with_bare(self, tmp_path: Path, capsys):
        # Without --strict, bare markers are reported but exit code is 0.
        (tmp_path / "core.py").write_text(
            "# deferred to v0.2.x with no criterion\n",
            encoding="utf-8",
        )
        from eta_engine.scripts import _audit_deferral_criteria as mod

        old_root = mod.ROOT
        mod.ROOT = tmp_path
        try:
            rc = main([])
        finally:
            mod.ROOT = old_root
        assert rc == 0

    def test_json_output_shape(self, tmp_path: Path, capsys):
        (tmp_path / "core.py").write_text(
            "# deferred to v0.2.x. closes when test_x lands.\n",
            encoding="utf-8",
        )
        from eta_engine.scripts import _audit_deferral_criteria as mod

        old_root = mod.ROOT
        mod.ROOT = tmp_path
        try:
            rc = main(["--json"])
        finally:
            mod.ROOT = old_root
        assert rc == 0
        import json

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert "total" in payload
        assert "with_criterion" in payload
        assert payload["total"] == 1
        assert payload["with_criterion"] == 1
