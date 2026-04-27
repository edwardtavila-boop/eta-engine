"""Tests for :mod:`eta_engine.scripts.operator_action_queue`.

Pins the OP-list shape, the verdict glyph table, the JSON contract,
and the per-probe behaviour against synthetic state. The script
itself is read-only and pure-stdlib so the tests run fast (< 1s).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from eta_engine.scripts.operator_action_queue import (
    VERDICT_BLOCKED,
    VERDICT_DONE,
    VERDICT_OBSERVED,
    VERDICT_UNKNOWN,
    OpItem,
    collect_items,
    main,
    render_text,
)

if TYPE_CHECKING:
    import pytest


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


class TestOpListShape:
    """The list size + each item's required fields."""

    def test_collects_all_seventeen_op_items(self):
        items = collect_items()
        assert len(items) == 17

    def test_op_ids_are_sequential(self):
        items = collect_items()
        op_ids = [i.op_id for i in items]
        expected = [f"OP-{n}" for n in range(1, 18)]
        assert op_ids == expected

    def test_every_item_has_a_title(self):
        items = collect_items()
        for item in items:
            assert item.title, f"{item.op_id} missing title"

    def test_every_item_has_a_verdict_in_known_set(self):
        items = collect_items()
        known = {VERDICT_DONE, VERDICT_BLOCKED, VERDICT_OBSERVED, VERDICT_UNKNOWN}
        for item in items:
            assert item.verdict in known, f"{item.op_id} verdict={item.verdict!r} not in {known}"


# ---------------------------------------------------------------------------
# OpItem dataclass
# ---------------------------------------------------------------------------


class TestOpItemSerialisation:
    def test_as_dict_contains_canonical_keys(self):
        item = OpItem(
            op_id="OP-99",
            title="test item",
            verdict=VERDICT_BLOCKED,
            detail="why",
            where="here",
            evidence={"k": 1},
        )
        d = item.as_dict()
        assert set(d.keys()) >= {
            "op_id",
            "title",
            "verdict",
            "detail",
            "where",
            "evidence",
        }
        assert d["evidence"] == {"k": 1}

    def test_default_verdict_is_unknown(self):
        item = OpItem(op_id="OP-99", title="t")
        assert item.verdict == VERDICT_UNKNOWN
        assert item.evidence == {}


# ---------------------------------------------------------------------------
# Text render
# ---------------------------------------------------------------------------


class TestRenderText:
    def test_renders_summary_line(self):
        items = collect_items()
        text = render_text(items)
        assert "Summary:" in text
        assert "DONE:" in text
        assert "BLOCKED:" in text
        assert "OBSERVED:" in text
        assert "UNKNOWN:" in text

    def test_renders_glyph_legend(self):
        items = collect_items()
        text = render_text(items)
        assert "[OK]" in text
        assert "[!!]" in text
        assert "[~~]" in text
        assert "[??]" in text

    def test_verbose_includes_evidence_block(self):
        items = [
            OpItem(
                op_id="OP-99",
                title="t",
                verdict=VERDICT_DONE,
                evidence={"foo": "bar"},
            ),
        ]
        terse = render_text(items, verbose=False)
        assert "evidence" not in terse
        verbose = render_text(items, verbose=True)
        assert "evidence" in verbose
        assert "foo" in verbose

    def test_renders_each_op_id(self):
        items = collect_items()
        text = render_text(items)
        for n in range(1, 18):
            assert f"OP-{n}" in text


# ---------------------------------------------------------------------------
# CLI: --json
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_json_payload_round_trips(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--json"])
        assert rc == 0
        captured = capsys.readouterr().out
        payload = json.loads(captured)
        assert "items" in payload
        assert "summary" in payload
        assert len(payload["items"]) == 17
        # summary counts must equal items count
        total = sum(payload["summary"].values())
        assert total == 17

    def test_json_summary_has_all_four_verdicts(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--json"])
        assert rc == 0
        captured = capsys.readouterr().out
        payload = json.loads(captured)
        assert set(payload["summary"].keys()) == {
            "DONE",
            "BLOCKED",
            "OBSERVED",
            "UNKNOWN",
        }


# ---------------------------------------------------------------------------
# CLI: text mode + verbose
# ---------------------------------------------------------------------------


class TestCliTextMode:
    def test_default_text_render_succeeds(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "operator action queue" in out
        assert "Summary:" in out

    def test_verbose_flag_includes_evidence(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--verbose"])
        assert rc == 0
        out = capsys.readouterr().out
        # Evidence block fires when an item has non-empty evidence.
        # OP-3 (IBKR creds) always has evidence, regardless of state.
        assert "evidence" in out


# ---------------------------------------------------------------------------
# Probe behaviour under synthetic state
# ---------------------------------------------------------------------------


class TestMcpOauthProbeUnderSyntheticState:
    """The mcp_status reader is the only probe with a clean fake-state path
    (the others depend on env vars / config files / live router state)."""

    def test_status_ok_marks_done(self, monkeypatch) -> None:
        from eta_engine.scripts.operator_action_queue import (
            _op6_op7_op8_mcp_oauth,
        )

        roadmap: dict[str, Any] = {
            "shared_artifacts": {
                "mcp_status": {
                    "jotform": "ok",
                    "amplitude": "ok",
                    "coupler": "ok",
                },
            },
        }
        items = _op6_op7_op8_mcp_oauth(roadmap)
        assert len(items) == 3
        assert all(i.verdict == VERDICT_DONE for i in items)

    def test_status_needs_auth_marks_blocked(self, monkeypatch) -> None:
        from eta_engine.scripts.operator_action_queue import (
            _op6_op7_op8_mcp_oauth,
        )

        roadmap = {
            "shared_artifacts": {
                "mcp_status": {
                    "jotform": "needs_auth",
                    "amplitude": "needs_auth",
                    "coupler": "needs_auth",
                },
            },
        }
        items = _op6_op7_op8_mcp_oauth(roadmap)
        assert all(i.verdict == VERDICT_BLOCKED for i in items)

    def test_status_missing_marks_unknown(self) -> None:
        from eta_engine.scripts.operator_action_queue import (
            _op6_op7_op8_mcp_oauth,
        )

        roadmap: dict[str, Any] = {}
        items = _op6_op7_op8_mcp_oauth(roadmap)
        assert all(i.verdict == VERDICT_UNKNOWN for i in items)
