"""BOM-tolerance regression tests for kill_switch_latch."""

from __future__ import annotations

import json
from pathlib import Path


def _write_bom_json(path: Path, payload: dict) -> None:
    """Write the UTF-8 BOM shape produced by some PowerShell paths."""
    path.write_text(json.dumps(payload), encoding="utf-8-sig")


def test_latch_read_tolerates_utf8_bom(tmp_path: Path) -> None:
    """A UTF-8 BOM must not make the latch fail closed as corrupt."""
    from eta_engine.core.kill_switch_latch import KillSwitchLatch

    latch_path = tmp_path / "latch.json"
    _write_bom_json(
        latch_path,
        {
            "state": "ARMED",
            "action": "RESUME",
            "reason": "test",
            "scope": "tier_a",
            "severity": "INFO",
        },
    )

    rec = KillSwitchLatch(latch_path).read()
    assert rec.state.value == "ARMED"
    assert "corrupt" not in (rec.reason or "").lower()


def test_latch_read_still_works_without_bom(tmp_path: Path) -> None:
    """Sanity check: the encoding change preserves normal no-BOM reads."""
    from eta_engine.core.kill_switch_latch import KillSwitchLatch

    latch_path = tmp_path / "latch.json"
    latch_path.write_text(
        json.dumps(
            {
                "state": "ARMED",
                "action": "RESUME",
                "reason": "no-bom-test",
                "scope": "tier_a",
                "severity": "INFO",
            },
        ),
        encoding="utf-8",
    )

    rec = KillSwitchLatch(latch_path).read()
    assert rec.state.value == "ARMED"
    assert rec.reason == "no-bom-test"


def test_latch_read_still_fails_closed_on_truly_corrupt_data(tmp_path: Path) -> None:
    """BOM stripping must not swallow a genuinely corrupt JSON file."""
    from eta_engine.core.kill_switch_latch import KillSwitchLatch

    latch_path = tmp_path / "latch.json"
    latch_path.write_text("{not valid json at all", encoding="utf-8-sig")

    rec = KillSwitchLatch(latch_path).read()
    assert rec.state.value == "TRIPPED"
    assert "corrupt" in (rec.reason or "").lower()
