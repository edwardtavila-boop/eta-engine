"""
EVOLUTIONARY TRADING ALGO  //  scripts.clear_kill_switch
=========================================================
Operator CLI to reset the persisted kill-switch latch back to ARMED.

This is the human-adjudicated half of the kill-switch latch contract
documented in ``eta_engine/core/kill_switch_latch.py``: the runtime
latches a catastrophic verdict to disk; the operator reviews it; this
CLI is what they run to clear the latch and re-enable boot.

Why a separate CLI (and not just a method call)?
------------------------------------------------
The latch is meant to refuse boot until a human has explicitly
acknowledged the trip. Wrapping :meth:`KillSwitchLatch.clear` in a
defensive CLI gives us:

* mandatory ``--confirm`` AND ``--operator`` (no accidental clears)
* a separate append-only audit log at
  ``var/eta_engine/state/kill_switch_clears.jsonl`` (one JSONL line
  per clear event) so we have an out-of-band paper trail
* refusal when the latch is NOT tripped (defensive: avoids the
  operator overwriting a fresh ARMED record on confused-runbook
  invocations)
* refusal when the latch file is OUTSIDE the workspace root (per
  CLAUDE.md hard rule #1 -- single canonical write path)
* ``--dry-run`` so the operator can inspect what would happen
  without writing anything

Exit codes
----------
``0`` cleared successfully (or dry-run on a tripped latch)
``1`` latch is not tripped (refused -- nothing to clear)
``2`` latch file missing (refused -- nothing to clear)
``3`` latch file malformed / unreadable (refused -- inspect manually)
``4`` missing required CLI arg (``--confirm`` or ``--operator``)

Usage
-----
.. code-block:: bash

    python -m eta_engine.scripts.clear_kill_switch \\
        --confirm \\
        --operator <name> \\
        [--reason "<text>"] \\
        [--latch-path <path>] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eta_engine.core.kill_switch_latch import (
    KillSwitchLatch,
    LatchRecord,
    LatchState,
    default_legacy_path,  # noqa: F401 — re-exported for tests + operator override
    default_path,
    resolve_existing_path,
)

log = logging.getLogger(__name__)


#: Workspace hard-rule root. Latch paths must live somewhere under this
#: tree; refuse otherwise (CLAUDE.md hard rule #1).
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]

#: Append-only JSONL log of every clear event. Lives under the canonical
#: var/ state dir alongside the latch itself.
AUDIT_LOG_FILENAME = "kill_switch_clears.jsonl"


# --------------------------------------------------------------------------- #
# Exit codes (kept as constants so tests can import them)
# --------------------------------------------------------------------------- #
EXIT_CLEARED: int = 0
EXIT_NOT_TRIPPED: int = 1
EXIT_FILE_MISSING: int = 2
EXIT_MALFORMED: int = 3
EXIT_BAD_ARGS: int = 4


# --------------------------------------------------------------------------- #
# CLI parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser. Factored out so tests can introspect it."""
    parser = argparse.ArgumentParser(
        prog="python -m eta_engine.scripts.clear_kill_switch",
        description=(
            "Reset the persisted kill-switch latch to ARMED after a "
            "catastrophic trip. Requires --confirm and --operator. "
            "Writes an audit-log entry to "
            "var/eta_engine/state/kill_switch_clears.jsonl."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Explicit confirmation that the operator intends to clear "
            "the latch. Required -- defensive against accidental runs."
        ),
    )
    parser.add_argument(
        "--operator",
        type=str,
        default=None,
        help=("Operator identifier to record in both the latch's cleared_by field and the clear-audit log. Required."),
    )
    parser.add_argument(
        "--reason",
        type=str,
        default=None,
        help=("Optional free-text justification for the clear. Recorded in the audit log. Strongly recommended."),
    )
    parser.add_argument(
        "--latch-path",
        type=str,
        default=None,
        help=(
            "Override the latch file path (defaults to canonical "
            "var/eta_engine/state/kill_switch_latch.json with legacy "
            "in-repo fallback). Must live under the workspace root."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=("Read the latch and print what WOULD be done; do not write the latch file or the audit log."),
    )
    return parser


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _path_under_workspace(p: Path) -> bool:
    """Return True iff ``p`` resolves to a location under WORKSPACE_ROOT.

    Uses ``Path.resolve()`` so symlink shenanigans cannot escape the
    workspace constraint. We compare on the resolved path's parents to
    avoid false negatives when the file does not yet exist.
    """
    try:
        resolved = p.resolve(strict=False)
        workspace = WORKSPACE_ROOT.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return False
    return True


def _resolve_paths(latch_path_arg: str | None) -> tuple[Path, Path]:
    """Decide which latch path to operate on and where to write the audit log.

    Returns ``(read_path, write_path)``. When the operator did not pass
    ``--latch-path``, ``read_path`` may be the legacy in-repo location
    (if that's where the file currently lives) but ``write_path`` is
    always the canonical ``var/eta_engine/state/kill_switch_latch.json``
    -- this is the migration story: read from wherever it currently
    sits, but write the cleared record to the canonical home.

    When the operator DID pass ``--latch-path`` we honor it for both
    read and write so tests + tmp-dir scenarios behave predictably.
    """
    if latch_path_arg:
        explicit = Path(latch_path_arg)
        return explicit, explicit
    return resolve_existing_path(), default_path()


def _audit_log_path_for(write_path: Path) -> Path:
    """Pick where to write the clear-audit JSONL.

    For the canonical write path we use the canonical audit log. For
    test/tmp paths (anything else under the workspace) we co-locate
    the audit log next to the latch file so tests can find it without
    polluting the canonical state dir.
    """
    canonical_latch = default_path()
    if write_path == canonical_latch:
        return canonical_latch.parent / AUDIT_LOG_FILENAME
    return write_path.parent / AUDIT_LOG_FILENAME


def _summarize_record(rec: LatchRecord) -> str:
    """One-line summary of a latch record for stdout.

    Deliberately omits the ``evidence`` payload -- the operator may be
    in a public terminal and we don't want to print the full latch
    content to stdout.
    """
    return (
        f"state={rec.state.value} "
        f"action={rec.action or '-'} "
        f"scope={rec.scope or '-'} "
        f"tripped_at_utc={rec.tripped_at_utc or '-'}"
    )


def _append_audit_entry(
    audit_path: Path,
    *,
    operator: str,
    reason: str | None,
    prior_state: dict[str, Any],
    new_state: dict[str, Any],
) -> None:
    """Append one JSONL row to the clear-audit log.

    The file is opened in append mode so concurrent writers cannot
    truncate prior entries. We do NOT use ``os.replace`` here because
    JSONL append semantics are the point: every clear must add a row,
    never overwrite one. ``flush + fsync`` keeps the durability story
    intact even on a crash mid-write.
    """
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "operator": operator,
        "reason": reason,
        "prior_state": prior_state,
        "new_state": new_state,
    }
    body = json.dumps(entry, sort_keys=True) + "\n"
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(body)
        fh.flush()
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())


def _read_raw_latch(path: Path) -> tuple[str, dict[str, Any] | None]:
    """Classify the latch file at ``path`` without invoking fail-closed.

    Returns one of:
      * ``("missing", None)`` -- file does not exist
      * ``("malformed", None)`` -- file exists but JSON is invalid
        OR the root is not an object
      * ``("ok", raw_dict)`` -- file parses successfully

    We deliberately do NOT use :meth:`KillSwitchLatch.read` here because
    that synthesizes a fail-closed TRIPPED record on corrupt files, which
    would mask the malformed case from the CLI's exit-code contract.
    """
    if not path.exists():
        return "missing", None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "malformed", None
    if not isinstance(raw, dict):
        return "malformed", None
    return "ok", raw


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    """Entrypoint. Returns an integer exit code.

    Kept as a function (rather than module-level work) so importing
    this module has no side effects -- tests can ``from
    eta_engine.scripts.clear_kill_switch import main`` and call it
    safely.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # --- Required-arg checks (exit 4) --------------------------------------- #
    if not args.confirm:
        sys.stderr.write(
            "ERROR: --confirm is required. Refusing to clear without explicit confirmation.\n",
        )
        return EXIT_BAD_ARGS

    operator = (args.operator or "").strip()
    if not operator:
        sys.stderr.write(
            "ERROR: --operator <name> is required for audit attribution.\n",
        )
        return EXIT_BAD_ARGS

    # --- Path resolution + workspace hard rule ------------------------------ #
    read_path, write_path = _resolve_paths(args.latch_path)
    if not _path_under_workspace(read_path) or not _path_under_workspace(write_path):
        sys.stderr.write(
            f"ERROR: latch path is outside the workspace root "
            f"({WORKSPACE_ROOT}). Refusing per CLAUDE.md hard rule #1.\n",
        )
        return EXIT_BAD_ARGS

    # --- Read + classify ---------------------------------------------------- #
    classification, raw = _read_raw_latch(read_path)
    if classification == "missing":
        sys.stderr.write(
            f"ERROR: no latch file at {read_path}. There is nothing to "
            f"clear. (If this is first-boot, just start the runtime "
            f"-- a missing latch reads as ARMED.)\n",
        )
        return EXIT_FILE_MISSING
    if classification == "malformed":
        sys.stderr.write(
            f"ERROR: latch file at {read_path} is malformed JSON. Inspect manually before clearing.\n",
        )
        return EXIT_MALFORMED

    assert raw is not None  # for type-checker; "ok" implies non-None
    prior_record = LatchRecord.from_dict(raw)

    if not prior_record.is_tripped():
        sys.stderr.write(
            f"ERROR: latch at {read_path} is not TRIPPED (state={prior_record.state.value}). Nothing to clear.\n",
        )
        return EXIT_NOT_TRIPPED

    # --- Dry run ------------------------------------------------------------ #
    if args.dry_run:
        sys.stdout.write(
            f"[dry-run] would clear latch at {write_path}\n"
            f"[dry-run] prior: {_summarize_record(prior_record)}\n"
            f"[dry-run] operator={operator} "
            f"reason={args.reason or '(none)'}\n"
            f"[dry-run] no files written.\n",
        )
        return EXIT_CLEARED

    # --- Real clear --------------------------------------------------------- #
    # We do NOT just call latch.clear() and let it write where the latch
    # was constructed -- when we read from the legacy fallback we still
    # want the cleared record to land at the canonical write path. So we
    # construct the latch on the WRITE path. The .clear() helper preserves
    # the prior trip's audit trail by reading whatever is currently there;
    # to make sure that prior audit trail comes from the actual prior
    # latch (which may live at the legacy path), we first copy the prior
    # record's payload to the canonical write path before calling clear().
    if read_path != write_path:
        write_path.parent.mkdir(parents=True, exist_ok=True)
        # Snapshot the prior raw payload at the canonical path so the
        # subsequent .clear() preserves the audit trail correctly. We
        # use the same atomic temp-then-replace pattern the latch uses.
        tmp = write_path.with_suffix(write_path.suffix + ".tmp")
        body = json.dumps(prior_record.to_dict(), indent=2, sort_keys=True) + "\n"
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, write_path)

    latch = KillSwitchLatch(write_path)
    new_record = latch.clear(cleared_by=operator, reason=args.reason)

    # --- Audit-log append --------------------------------------------------- #
    audit_path = _audit_log_path_for(write_path)
    _append_audit_entry(
        audit_path,
        operator=operator,
        reason=args.reason,
        prior_state=prior_record.to_dict(),
        new_state=new_record.to_dict(),
    )

    # --- Confirmation summary (no full latch dump) -------------------------- #
    sys.stdout.write(
        f"kill-switch latch CLEARED.\n"
        f"  path:     {write_path}\n"
        f"  operator: {operator}\n"
        f"  prior:    {_summarize_record(prior_record)}\n"
        f"  now:      {_summarize_record(new_record)}\n"
        f"  audit:    {audit_path}\n",
    )
    # Sanity: the post-clear record must be ARMED. If it isn't, fail
    # loudly -- this would indicate a bug in KillSwitchLatch.clear().
    if new_record.state is not LatchState.ARMED:
        sys.stderr.write(
            f"ERROR: post-clear record is not ARMED (state={new_record.state.value}). Inspect immediately.\n",
        )
        return EXIT_MALFORMED

    return EXIT_CLEARED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
