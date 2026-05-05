from __future__ import annotations

from eta_engine.scripts import _docstring_audit


def test_docstring_audit_classifies_regressions_and_improvements() -> None:
    assert _docstring_audit._classify(None, 4) == ("SEED", 4)
    assert _docstring_audit._classify(4, 3) == ("GREEN", -1)
    assert _docstring_audit._classify(4, 5) == ("YELLOW", 1)
    assert _docstring_audit._classify(4, 14) == ("RED", 10)


def test_docstring_audit_ratchets_baseline_down_only() -> None:
    current = {
        "core/cleaner.py": [{"symbol": "needs_doc", "kind": "fn", "lineno": 10}],
        "core/new_module.py": [
            {"symbol": "one", "kind": "fn", "lineno": 3},
            {"symbol": "two", "kind": "fn", "lineno": 6},
        ],
    }
    baseline = {
        "per_module": {
            "core/cleaner.py": 3,
            "core/deleted.py": 9,
        },
        "samples": 7,
    }

    diagnostics, new_baseline = _docstring_audit._evaluate(current, baseline)

    assert new_baseline["samples"] == 8
    assert new_baseline["per_module"] == {
        "core/cleaner.py": 1,
        "core/new_module.py": 2,
    }
    assert [d["level"] for d in diagnostics] == ["GREEN", "SEED"]


def test_docstring_audit_rewrites_baseline_only_when_counts_change() -> None:
    baseline = {
        "per_module": {"core/a.py": 2},
        "samples": 3,
        "last_updated": "older",
    }
    same_counts = {
        "per_module": {"core/a.py": 2},
        "samples": 4,
        "last_updated": "newer",
    }
    changed_counts = {
        "per_module": {"core/a.py": 1},
        "samples": 4,
        "last_updated": "newer",
    }

    assert not _docstring_audit._baseline_counts_changed(baseline, same_counts)
    assert _docstring_audit._baseline_counts_changed(baseline, changed_counts)
