"""Tests for the JARVIS wiring audit diagnostic CLI.

Each test uses ``tmp_path`` to fake the trace file or a temp module
directory so the suite never touches production state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eta_engine.scripts import jarvis_wiring_audit as jwa


def _write_trace_records(path: Path, records: list[dict]) -> None:
    """Helper: append each dict as a JSON line to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_audit_finds_modules(tmp_path: Path) -> None:
    """Running audit on the live brain/jarvis_v3 dir returns a non-empty list."""
    trace_path = tmp_path / "jarvis_trace.jsonl"
    statuses = jwa.audit(trace_path=trace_path)
    assert isinstance(statuses, list)
    assert len(statuses) > 0
    # Each entry is a ModuleStatus dataclass with the required fields.
    s = statuses[0]
    assert hasattr(s, "module")
    assert hasattr(s, "expected_to_fire")
    assert hasattr(s, "fires_per_consult_empirical")
    assert hasattr(s, "dark_for_days")
    assert hasattr(s, "notes")


def test_audit_marks_research_only(tmp_path: Path) -> None:
    """Modules without EXPECTED_HOOKS get expected_to_fire=False."""
    trace_path = tmp_path / "jarvis_trace.jsonl"
    statuses = jwa.audit(trace_path=trace_path)
    # At least one research-only module exists in the live tree
    # (e.g. firm_board, philosophy — none of these declare EXPECTED_HOOKS today).
    research_only = [s for s in statuses if not s.expected_to_fire]
    assert len(research_only) > 0


def test_audit_marks_expected_to_fire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A temp module declaring EXPECTED_HOOKS gets expected_to_fire=True."""
    temp_pkg = tmp_path / "fake_jarvis_v3"
    temp_pkg.mkdir()
    (temp_pkg / "__init__.py").write_text("", encoding="utf-8")
    (temp_pkg / "fires_module.py").write_text(
        'EXPECTED_HOOKS = ("consult",)\n',
        encoding="utf-8",
    )
    (temp_pkg / "research_module.py").write_text(
        '"""no hooks here"""\n',
        encoding="utf-8",
    )
    # Add tmp_path to sys.path so importlib finds our package
    monkeypatch.syspath_prepend(str(tmp_path))

    trace_path = tmp_path / "jarvis_trace.jsonl"
    statuses = jwa.audit(
        trace_path=trace_path,
        module_dir=temp_pkg,
        package_name="fake_jarvis_v3",
    )
    by_module = {s.module: s for s in statuses}
    assert "fires_module" in by_module
    assert by_module["fires_module"].expected_to_fire is True
    assert "research_module" in by_module
    assert by_module["research_module"].expected_to_fire is False


def test_empirical_count_from_trace(tmp_path: Path) -> None:
    """Trace records mentioning a module raise its fires_per_consult_empirical."""
    trace_path = tmp_path / "jarvis_trace.jsonl"
    # Write 10 records — all mention portfolio_brain inside `portfolio`.
    records = []
    for i in range(10):
        records.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "bot_id": f"bot_{i}",
                "consult_id": f"cid_{i}",
                "portfolio": {"source": "portfolio_brain", "size_modifier": 1.0},
                "schools": {},
                "clashes": [],
                "hot_learn": {},
                "context": {},
            }
        )
    _write_trace_records(trace_path, records)

    statuses = jwa.audit(trace_path=trace_path)
    pb = next(s for s in statuses if s.module == "portfolio_brain")
    assert pb.fires_per_consult_empirical == pytest.approx(1.0)


def test_empirical_count_from_wiring_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit trace wiring metadata is enough to prove a module fired."""
    temp_pkg = tmp_path / "fake_jarvis_v3"
    temp_pkg.mkdir()
    (temp_pkg / "__init__.py").write_text("", encoding="utf-8")
    (temp_pkg / "core_module.py").write_text(
        'EXPECTED_HOOKS = ("run",)\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    trace_path = tmp_path / "jarvis_trace.jsonl"
    _write_trace_records(
        trace_path,
        [
            {
                "ts": datetime.now(UTC).isoformat(),
                "bot_id": "live_bot",
                "wiring": {"modules": ["core_module"], "hooks": {"core_module": "run"}},
            }
        ],
    )

    statuses = jwa.audit(
        trace_path=trace_path,
        module_dir=temp_pkg,
        package_name="fake_jarvis_v3",
    )

    core = next(s for s in statuses if s.module == "core_module")
    assert core.fires_per_consult_empirical == pytest.approx(1.0)
    assert core.dark_for_days == 0


def test_legacy_trace_fields_imply_core_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-wiring-field trace rows still prove core hot-path modules fired."""
    temp_pkg = tmp_path / "fake_jarvis_v3"
    temp_pkg.mkdir()
    (temp_pkg / "__init__.py").write_text("", encoding="utf-8")
    for module in (
        "jarvis_conductor",
        "trace_emitter",
        "context_enricher",
        "portfolio_brain",
        "hot_learner",
        "hermes_overrides",
    ):
        (temp_pkg / f"{module}.py").write_text('EXPECTED_HOOKS = ("run",)\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    trace_path = tmp_path / "jarvis_trace.jsonl"
    _write_trace_records(
        trace_path,
        [
            {
                "ts": datetime.now(UTC).isoformat(),
                "bot_id": "live_bot",
                "consult_id": "abc123",
                "elapsed_ms": 12.3,
                "final_size": 0.0,
                "schema_version": 2,
                "context": {"session": "NY_AM"},
                "portfolio": {"size_modifier": 1.0},
                "hot_learn": {"weights": {}},
                "overrides_snapshot": {"size_modifier": None, "school_weights": {}},
            }
        ],
    )

    statuses = jwa.audit(
        trace_path=trace_path,
        module_dir=temp_pkg,
        package_name="fake_jarvis_v3",
    )

    assert {s.module for s in statuses if s.dark_for_days == 0} == {
        "jarvis_conductor",
        "trace_emitter",
        "context_enricher",
        "portfolio_brain",
        "hot_learner",
        "hermes_overrides",
    }


def test_iter_trace_records_ignores_magicmock_pollution(tmp_path: Path) -> None:
    """Synthetic pytest consults should not count as live JARVIS evidence."""
    trace_path = tmp_path / "jarvis_trace.jsonl"
    _write_trace_records(
        trace_path,
        [
            {
                "ts": datetime.now(UTC).isoformat(),
                "bot_id": "<MagicMock name='mock.bot_id'>",
                "wiring": {"modules": ["portfolio_brain"]},
            },
            {
                "ts": datetime.now(UTC).isoformat(),
                "bot_id": "mnq_anchor_sweep",
                "wiring": {"modules": ["portfolio_brain"]},
            },
        ],
    )

    records = jwa._iter_trace_records([trace_path])

    assert len(records) == 1
    assert records[0]["bot_id"] == "mnq_anchor_sweep"


def test_dark_module_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expected-to-fire module never mentioned in trace → dark_for_days >= 7."""
    # Build a temp module dir with one expected-to-fire module so the test is
    # deterministic regardless of which live modules carry EXPECTED_HOOKS.
    temp_pkg = tmp_path / "fake_jarvis_v3"
    temp_pkg.mkdir()
    (temp_pkg / "__init__.py").write_text("", encoding="utf-8")
    (temp_pkg / "lonely_module.py").write_text(
        'EXPECTED_HOOKS = ("consult",)\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    trace_path = tmp_path / "jarvis_trace.jsonl"  # does not exist
    statuses = jwa.audit(
        trace_path=trace_path,
        module_dir=temp_pkg,
        package_name="fake_jarvis_v3",
    )
    lonely = next(s for s in statuses if s.module == "lonely_module")
    assert lonely.expected_to_fire is True
    assert lonely.dark_for_days >= 7


def test_to_markdown_includes_dark_first(tmp_path: Path) -> None:
    """The markdown table places dark modules ahead of healthy ones."""
    statuses = [
        jwa.ModuleStatus(
            module="healthy_mod",
            expected_to_fire=True,
            fires_per_consult_empirical=0.8,
            dark_for_days=0,
            notes="",
        ),
        jwa.ModuleStatus(
            module="dark_mod",
            expected_to_fire=True,
            fires_per_consult_empirical=0.0,
            dark_for_days=10,
            notes="",
        ),
        jwa.ModuleStatus(
            module="research_mod",
            expected_to_fire=False,
            fires_per_consult_empirical=0.0,
            dark_for_days=999,
            notes="",
        ),
    ]
    md = jwa.to_markdown(statuses)
    idx_dark = md.find("dark_mod")
    idx_healthy = md.find("healthy_mod")
    assert idx_dark != -1
    assert idx_healthy != -1
    assert idx_dark < idx_healthy


def test_to_json_has_required_fields() -> None:
    """to_json output carries generated_at, modules, n_dark, n_total_expected."""
    statuses = [
        jwa.ModuleStatus(
            module="m1",
            expected_to_fire=True,
            fires_per_consult_empirical=0.5,
            dark_for_days=0,
            notes="",
        ),
        jwa.ModuleStatus(
            module="m2",
            expected_to_fire=True,
            fires_per_consult_empirical=0.0,
            dark_for_days=8,
            notes="",
        ),
        jwa.ModuleStatus(
            module="m3",
            expected_to_fire=False,
            fires_per_consult_empirical=0.0,
            dark_for_days=999,
            notes="",
        ),
    ]
    payload = jwa.to_json(statuses)
    assert "generated_at" in payload
    assert "modules" in payload
    assert "n_dark" in payload
    assert "n_total_expected" in payload
    assert payload["n_dark"] == 1
    assert payload["n_total_expected"] == 2
    assert isinstance(payload["modules"], list)
    assert len(payload["modules"]) == 3


def test_main_writes_audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main([]) runs without crashing and writes the audit file."""
    audit_out = tmp_path / "jarvis_wiring_audit.json"
    trace_path = tmp_path / "jarvis_trace.jsonl"
    monkeypatch.setattr(jwa, "AUDIT_OUTPUT_PATH", audit_out)
    monkeypatch.setattr(jwa, "DEFAULT_TRACE_PATH", trace_path)

    rc = jwa.main(["--json"])
    assert rc == 0
    assert audit_out.exists()
    data = json.loads(audit_out.read_text(encoding="utf-8"))
    assert "modules" in data
    assert "generated_at" in data
