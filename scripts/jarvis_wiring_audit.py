"""JARVIS Wiring Audit — diagnostic CLI for dark/research modules in brain/jarvis_v3.

Walks every ``.py`` module under ``eta_engine/brain/jarvis_v3/``, distinguishes
*expected-to-fire* modules (those that export the ``EXPECTED_HOOKS`` constant)
from *research-only* modules, then cross-references the live JARVIS trace
stream (``var/eta_engine/state/jarvis_trace.jsonl`` + rotated copies from the
last seven days) to compute, per module, both the empirical fire rate
(matches / total records) and the staleness in days since the most recent
mention.

The intent is operator-facing: a fast diagnostic to spot modules that the
plan promises will fire on every consult but which never actually appear in
the trace stream — a strong indicator the wiring drifted or a stream agent
silently failed.

Usage
-----
    python -m eta_engine.scripts.jarvis_wiring_audit          # markdown table
    python -m eta_engine.scripts.jarvis_wiring_audit --json   # JSON payload

In both modes the latest snapshot is also persisted to
``var/eta_engine/state/jarvis_wiring_audit.json`` so the kaizen loop /
supervisor can read it without re-running the audit.
"""

from __future__ import annotations

import argparse
import gzip
import importlib
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger("eta_engine.jarvis_wiring_audit")

# ---------------------------------------------------------------------------
# Constants — overridable for tests via monkeypatching.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_MODULE_DIR: Path = REPO_ROOT / "eta_engine" / "brain" / "jarvis_v3"
DEFAULT_PACKAGE: str = "eta_engine.brain.jarvis_v3"

DEFAULT_TRACE_PATH: Path = (
    REPO_ROOT / "var" / "eta_engine" / "state" / "jarvis_trace.jsonl"
)
AUDIT_OUTPUT_PATH: Path = (
    REPO_ROOT / "var" / "eta_engine" / "state" / "jarvis_wiring_audit.json"
)

# Trace fields scanned for module-name substring matches.
TRACE_FIELDS: tuple[str, ...] = ("schools", "clashes", "portfolio", "hot_learn", "context")

# Rotated trace files older than this many days are skipped.
TRACE_LOOKBACK_DAYS: int = 7

# Reported when an expected-to-fire module has never been seen.
NEVER_SEEN_DAYS: int = 999

EXPECTED_HOOKS: tuple[str, ...] = ("audit", "main")


@dataclass
class ModuleStatus:
    """Per-module wiring health snapshot."""

    module: str
    expected_to_fire: bool
    fires_per_consult_empirical: float
    dark_for_days: int
    notes: str = ""


# ---------------------------------------------------------------------------
# Module discovery + introspection
# ---------------------------------------------------------------------------


def _discover_modules(module_dir: Path) -> list[str]:
    """List importable ``.py`` modules in ``module_dir``.

    Excludes ``__init__.py``, ``__pycache__``, files starting with ``_``,
    and anything that looks like a test (``test_*.py`` or ``*_test.py``).
    """
    if not module_dir.exists() or not module_dir.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(module_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix != ".py":
            continue
        name = entry.stem
        if name.startswith("_"):
            continue
        if name == "__init__":
            continue
        if name.startswith("test_") or name.endswith("_test"):
            continue
        out.append(name)
    return out


def _import_module_safely(package: str, module_name: str) -> ModuleType | None:
    """Attempt ``importlib.import_module(package.module_name)``; never raise."""
    full = f"{package}.{module_name}"
    try:
        return importlib.import_module(full)
    except Exception as exc:  # noqa: BLE001 -- diagnostic must survive import errors
        logger.debug("import of %s failed: %s", full, exc)
        return None


def _module_expected_to_fire(mod: ModuleType | None) -> bool:
    """Return True if the imported module exports a non-empty ``EXPECTED_HOOKS``."""
    if mod is None:
        return False
    hooks = getattr(mod, "EXPECTED_HOOKS", None)
    if hooks is None:
        return False
    try:
        return len(tuple(hooks)) > 0
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Trace stream reader
# ---------------------------------------------------------------------------


def _resolve_trace_files(trace_path: Path) -> list[Path]:
    """Return the active trace path + any rotated companions from the last 7 days.

    Rotated files are produced by Stream 2's trace_emitter and follow the
    naming convention ``<base>_<UTC-stamp>.jsonl[.gz]``.
    """
    files: list[Path] = []
    if trace_path.exists():
        files.append(trace_path)
    state_dir = trace_path.parent
    if not state_dir.exists():
        return files
    base = trace_path.stem  # 'jarvis_trace'
    cutoff = datetime.now(UTC) - timedelta(days=TRACE_LOOKBACK_DAYS)
    for sibling in state_dir.iterdir():
        if not sibling.is_file():
            continue
        if not sibling.name.startswith(f"{base}_"):
            continue
        try:
            mtime = datetime.fromtimestamp(sibling.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        if sibling not in files:
            files.append(sibling)
    return files


def _iter_trace_records(files: list[Path]) -> list[dict]:
    """Read every JSON line from ``files``; malformed lines are dropped silently."""
    records: list[dict] = []
    for path in files:
        try:
            opener: Any = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except OSError as exc:
            logger.debug("failed reading %s: %s", path, exc)
            continue
    return records


def _record_mentions(rec: dict, module_name: str) -> bool:
    """Case-insensitive substring match for ``module_name`` across the scanned fields."""
    needle = module_name.lower()
    for field in TRACE_FIELDS:
        if field not in rec:
            continue
        value = rec[field]
        if not value:
            continue
        try:
            haystack = json.dumps(value, default=str).lower()
        except (TypeError, ValueError):
            haystack = str(value).lower()
        if needle in haystack:
            return True
    return False


def _record_ts(rec: dict) -> datetime | None:
    """Parse the record's ``ts`` field as a tz-aware UTC datetime; tolerant of formats."""
    ts_raw = rec.get("ts")
    if not isinstance(ts_raw, str):
        return None
    try:
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit(
    trace_path: Path | None = None,
    *,
    module_dir: Path | None = None,
    package_name: str | None = None,
) -> list[ModuleStatus]:
    """Walk modules + scan trace stream → list[ModuleStatus]."""
    custom_target = module_dir is not None or package_name is not None
    module_dir = module_dir or DEFAULT_MODULE_DIR
    package_name = package_name or DEFAULT_PACKAGE
    trace_path = trace_path or DEFAULT_TRACE_PATH

    # Refresh the importer state so callers that mutate sys.path see new modules.
    importlib.invalidate_caches()
    if custom_target:
        # The caller pointed us at a non-default location (typically a tmp_path
        # in a test); drop stale package + sub-module bindings so the import
        # resolves against the current sys.path entries.
        for cached_name in [
            n for n in list(sys.modules)
            if n == package_name or n.startswith(f"{package_name}.")
        ]:
            del sys.modules[cached_name]

    modules = _discover_modules(module_dir)
    trace_files = _resolve_trace_files(trace_path)
    records = _iter_trace_records(trace_files)
    n_records = len(records)

    now = datetime.now(UTC)
    statuses: list[ModuleStatus] = []
    for name in modules:
        mod = _import_module_safely(package_name, name)
        expected = _module_expected_to_fire(mod)
        note_parts: list[str] = []
        if mod is None:
            note_parts.append("import_failed")

        mention_count = 0
        most_recent: datetime | None = None
        for rec in records:
            if _record_mentions(rec, name):
                mention_count += 1
                rec_ts = _record_ts(rec)
                if rec_ts is not None and (most_recent is None or rec_ts > most_recent):
                    most_recent = rec_ts

        fires_empirical = (mention_count / n_records) if n_records else 0.0

        if most_recent is None:
            dark_days = NEVER_SEEN_DAYS if expected else 0
        else:
            delta = now - most_recent
            dark_days = max(0, delta.days)

        if expected and dark_days >= TRACE_LOOKBACK_DAYS and mention_count == 0:
            note_parts.append("dark_in_lookback_window")

        statuses.append(
            ModuleStatus(
                module=name,
                expected_to_fire=expected,
                fires_per_consult_empirical=round(fires_empirical, 4),
                dark_for_days=int(dark_days),
                notes=";".join(note_parts),
            )
        )
    return statuses


def _sort_for_report(statuses: list[ModuleStatus]) -> list[ModuleStatus]:
    """Sort: dark expected-to-fire first, then healthy expected, then research-only.

    Within each group, fall back to module name for determinism.
    """

    def _bucket(s: ModuleStatus) -> int:
        if s.expected_to_fire and s.dark_for_days >= TRACE_LOOKBACK_DAYS:
            return 0
        if s.expected_to_fire:
            return 1
        return 2

    return sorted(statuses, key=lambda s: (_bucket(s), -s.dark_for_days, s.module))


def to_markdown(statuses: list[ModuleStatus]) -> str:
    """Render a sorted GitHub-flavoured markdown table; dark modules at the top."""
    ordered = _sort_for_report(statuses)
    lines: list[str] = []
    lines.append("# JARVIS Wiring Audit")
    lines.append("")
    lines.append(f"_generated at {datetime.now(UTC).isoformat()}_")
    lines.append("")
    lines.append("| module | expected | fires/consult | dark_days | notes |")
    lines.append("|---|---|---|---|---|")
    for s in ordered:
        lines.append(
            f"| {s.module} | {s.expected_to_fire} | {s.fires_per_consult_empirical:.4f} "
            f"| {s.dark_for_days} | {s.notes} |"
        )
    return "\n".join(lines) + "\n"


def to_json(statuses: list[ModuleStatus]) -> dict:
    """Render the audit as a JSON-serialisable dict."""
    ordered = _sort_for_report(statuses)
    n_dark = sum(
        1
        for s in ordered
        if s.expected_to_fire and s.dark_for_days >= TRACE_LOOKBACK_DAYS
    )
    n_total_expected = sum(1 for s in ordered if s.expected_to_fire)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "n_dark": n_dark,
        "n_total_expected": n_total_expected,
        "modules": [asdict(s) for s in ordered],
    }


def _write_audit_snapshot(payload: dict, path: Path) -> None:
    """Persist the JSON payload to ``path``; best-effort, never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not persist audit snapshot to %s: %s", path, exc)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: emit markdown (default) or JSON; always persists snapshot."""
    parser = argparse.ArgumentParser(
        description="Diagnostic: which brain/jarvis_v3 modules actually fire?",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the default markdown table.",
    )
    parser.add_argument(
        "--trace-path",
        type=Path,
        default=None,
        help="Override the active trace path (defaults to var/eta_engine/state/jarvis_trace.jsonl).",
    )
    args = parser.parse_args(argv)

    statuses = audit(trace_path=args.trace_path)
    payload = to_json(statuses)
    _write_audit_snapshot(payload, AUDIT_OUTPUT_PATH)

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(to_markdown(statuses))
    return 0


if __name__ == "__main__":  # pragma: no cover -- CLI dispatch
    sys.exit(main())
