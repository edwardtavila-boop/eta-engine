"""Regression pin: the tracked audit-log files keep their hash across
a test run.

PR #2 (broker_connections pollution) and PR #4 (alerts_log pollution)
each fixed a runtime test that was silently appending to a tracked
audit log on every test invocation. This module is the structural
pin that catches a third recurrence at CI time.

How it works
------------
A pytest ``conftest.py``-style hook would be cleaner, but adding one
risks affecting the rest of the suite. Instead, this module:

  1. At collection time, snapshots the SHA-256 of every pinned
     audit-log file into ``_BASELINE_DIGESTS``.
  2. Two tests guard the invariant:
     * ``test_audit_logs_present_at_collection`` -- sanity that the
       pinned files exist; if they get gitignored later, the pin
       list needs maintenance.
     * ``test_audit_logs_unchanged_at_module_run_time`` -- recompute
       the digest at test time and assert it matches the collection-
       time baseline. Won't catch tests that run AFTER this module,
       but the suite ordering puts test_no_* fairly late so most of
       the polluting-prone runtime tests have already executed.

What this test does NOT enforce
-------------------------------
  * Per-test pollution. The ``test_amain_wire_up.py`` and
    ``test_preflight.py`` tests already redirect via env vars +
    monkeypatch, so a green CI run keeps the audit logs untouched.
  * Files outside the pin list (decision journals, btc_live, etc.).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PINS: tuple[Path, ...] = (
    _ROOT / "docs" / "alerts_log.jsonl",
)


def _digest(p: Path) -> str | None:
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


# Snapshot at module collection time. Recompute later to detect drift.
_BASELINE_DIGESTS: dict[Path, str | None] = {p: _digest(p) for p in _PINS}


def test_audit_logs_present_at_collection() -> None:
    """Sanity: every pinned file exists at module-import. If a file
    gets gitignored later, remove it from the _PINS tuple to keep
    this test honest."""
    for p in _PINS:
        assert p.exists(), (
            f"tracked audit-log file missing: {p.relative_to(_ROOT)}. "
            f"If this is intentional, drop it from _PINS in "
            f"tests/test_no_test_pollution.py."
        )


def test_audit_logs_unchanged_at_module_run_time() -> None:
    """Recompute every pinned file's digest at test time. Drift means
    a test that ran between collection and now appended to the file."""
    for p, baseline in _BASELINE_DIGESTS.items():
        current = _digest(p)
        assert current == baseline, (
            f"audit log {p.relative_to(_ROOT)} drifted during the "
            f"test sweep -- a test must not append to a tracked audit "
            f"trail.\n"
            f"  baseline (at collection): {baseline!r}\n"
            f"  current  (at test run):   {current!r}\n"
            f"Likely fix: monkeypatch the relevant runtime path env "
            f"var (APEX_ALERTS_LOG_PATH for alerts_log, "
            f"VENUE_CONNECTION_REPORT_DIR for broker_connections) to "
            f"tmp_path. See tests/test_preflight.py + "
            f"tests/test_amain_wire_up.py for working patterns."
        )
