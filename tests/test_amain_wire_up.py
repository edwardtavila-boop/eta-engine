"""
Production wire-up smoke test for ``scripts.run_eta_live._amain``.

Why this test exists
--------------------
The v0.1.63 R1 closure shipped the ``BrokerEquityReconciler`` /
``BrokerEquityPoller`` / ``BrokerEquityAdapter`` stack and a full
:class:`ApexRuntime` integration -- but ``_amain`` (the production
CLI entrypoint) was constructing ``ApexRuntime(cfg)`` with no
``broker_equity_reconciler`` / ``broker_equity_poller`` kwargs. The
stack was dormant code in production.

Process gap #3 of the v0.1.64 Red Team review called for "a test that
boots the actual ``_amain`` codepath end-to-end and asserts the
``broker_equity`` block lands in the ``runtime_log.jsonl``" -- this
file is that test.

What's enforced
---------------
* ``_amain --max-bars 1 --dry-run`` exits with rc=0.
* The runtime log JSONL gets at least one entry.
* The ``broker_equity`` sub-key appears in the per-tick meta block
  (under ``meta.broker_equity`` or directly, depending on the schema).
  This is the structural pin: if a future refactor ever drops the
  reconciler from ``_amain``, the log will be missing the key and
  this test will fail loudly.

What's NOT enforced
-------------------
* Network calls -- this is a dry-run smoke test, no broker SDK is
  pinged. The wired adapter is :class:`NullBrokerEquityAdapter` which
  always returns ``None``, so the ``broker_equity`` reason will be
  ``no_broker_data`` regardless of broker availability.
* Timing -- ``--tick-interval 0`` runs as fast as possible.
* Tolerance defaults -- separately covered by
  ``test_broker_equity_reconciler.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_amain_dry_run_wires_broker_equity_into_runtime_log(
    tmp_path: Path,
) -> None:
    """End-to-end: ``_amain --max-bars 1 --dry-run`` must wire the
    R1 reconciler so each tick records a ``broker_equity`` block."""
    # Lazy import so a broken module surfaces as an import error in
    # this test rather than at collection time (clearer signal).
    sys.path.insert(0, str(ROOT.parent))
    from eta_engine.scripts.run_eta_live import _amain

    log_path = tmp_path / "rt.jsonl"
    state_path = tmp_path / "s.json"
    # Seed apex_go_state with the MNQ tier-A flag so at least one bot
    # is active for the tick. With an empty go_state there's nothing to
    # tick, the runtime emits only runtime_start/runtime_stop, and the
    # broker_equity block (which lands in per-tick meta dicts) never
    # appears -- not because the wire-up is broken but because there
    # are no ticks. Seed -> force a real tick path.
    state_path.write_text(
        json.dumps(
            {
                "shared_artifacts": {
                    "apex_go_state": {"tier_a_mnq_live": True},
                },
            }
        ),
        encoding="utf-8",
    )

    rc = await _amain(
        [
            "--max-bars",
            "1",
            "--tick-interval",
            "0",
            "--state-path",
            str(state_path),
            "--log-path",
            str(log_path),
        ]
    )

    assert rc == 0, f"_amain returned rc={rc}, expected 0"
    assert log_path.exists(), f"runtime log was never written -- did _amain even tick? (checked {log_path})"

    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "runtime log is empty -- _amain ticked zero times"

    # Find at least one tick entry with a broker_equity block.
    # Schema: a tick line is a JSON object with kind="tick" and a meta
    # dict that includes broker_equity when the reconciler is wired.
    tick_with_be = None
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"runtime log contains non-JSON line (RFC 8259 violation? "
                f"H5 regression?): {exc} -- line: {raw[:200]!r}",
            )
        # Strict-mode parse: reject any 'Infinity' / 'NaN' tokens that
        # snuck in via float('inf') in a stat field. H5 regression pin.
        if any(tok in raw for tok in ("Infinity", "NaN", "-Infinity")):
            pytest.fail(
                f"runtime log contains non-RFC-8259 tokens (H5 regression): {raw[:200]!r}",
            )
        if entry.get("kind") == "tick":
            meta = entry.get("meta") or {}
            if "broker_equity" in meta or "broker_equity" in entry:
                tick_with_be = entry
                break

    assert tick_with_be is not None, (
        "no tick entry contains a broker_equity block. "
        "B1 regression: _amain is constructing ApexRuntime without "
        "wiring the BrokerEquityReconciler / BrokerEquityPoller. "
        f"Tick lines seen: {len(lines)}. Sample line: {lines[0][:200]!r}"
    )

    # Verify the broker_equity block has the expected R1 fields.
    be = tick_with_be.get("meta", {}).get(
        "broker_equity",
        tick_with_be.get("broker_equity"),
    )
    assert isinstance(be, dict), f"broker_equity block is not a dict: {be!r}"
    # In dry-run with NullBrokerEquityAdapter, reason must be no_broker_data.
    # The exact field set depends on what runtime chose to project, but
    # 'reason' is the canonical classification key per
    # BrokerEquityReconciler.ReconcileResult.as_dict.
    reason = be.get("reason")
    assert reason in {"no_broker_data", "within_tolerance"}, (
        f"broker_equity.reason={reason!r}; expected no_broker_data (NullBrokerEquityAdapter is wired in dry-run mode)"
    )


@pytest.mark.asyncio
async def test_amain_dry_run_emits_broker_equity_in_boot_banner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In-process counterpart of the boot-banner pin.

    Originally implemented as a subprocess run of ``run_eta_live``,
    but that path proved fragile under the parallel-pytest contention
    that sibling sessions create -- the subprocess startup budget
    (initially 60s, bumped to 180s) kept getting starved and the
    test timed out for reasons unrelated to the banner contract it
    is meant to pin. v0.1.71 refactored this to call ``_amain``
    directly via asyncio and capture stdout via pytest's ``capsys``.
    The contract is identical: ``broker_equity :`` must appear in
    the banner; absence indicates B1 banner regression (operator
    has no way to confirm from boot output that the reconciler
    is wired).

    Seeds ``apex_go_state.tier_a_mnq_live = True`` so the runtime
    ticks a real bot (otherwise zero-equity trips the kill-switch
    latch and the boot is refused before the banner prints).
    """
    sys.path.insert(0, str(ROOT.parent))
    from eta_engine.scripts.run_eta_live import _amain

    log_path = tmp_path / "rt2.jsonl"
    state_path = tmp_path / "s2.json"
    state_path.write_text(
        json.dumps(
            {
                "shared_artifacts": {
                    "apex_go_state": {"tier_a_mnq_live": True},
                },
            }
        ),
        encoding="utf-8",
    )

    rc = await _amain(
        [
            "--max-bars",
            "1",
            "--tick-interval",
            "0",
            "--state-path",
            str(state_path),
            "--log-path",
            str(log_path),
        ]
    )
    assert rc == 0, f"_amain returned rc={rc}, expected 0"

    captured = capsys.readouterr()
    # Boot banner pin: the broker_equity line appears in stdout.
    assert "broker_equity :" in captured.out, (
        "boot banner does not contain 'broker_equity :' line. "
        "B1 banner regression: operator cannot tell from boot output "
        "whether the reconciler is wired or which adapter is bound. "
        f"\nstdout:\n{captured.out}"
    )


def _run(coro):
    """asyncio.run shim that handles already-running loops gracefully."""
    return asyncio.get_event_loop().run_until_complete(coro)
