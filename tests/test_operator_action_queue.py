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

    def test_collects_all_eighteen_op_items(self):
        items = collect_items()
        assert len(items) == 18

    def test_op_ids_are_sequential(self):
        items = collect_items()
        op_ids = [i.op_id for i in items]
        expected = [f"OP-{n}" for n in range(1, 19)]
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
        for n in range(1, 19):
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
        assert len(payload["items"]) == 18
        # summary counts must equal items count
        total = sum(payload["summary"].values())
        assert total == 18

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


class TestActiveBrokerCredentialProbes:
    def test_ibkr_probe_uses_runtime_consumed_cp_base_url(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setenv("IBKR_CP_BASE_URL", "https://127.0.0.1:5000/v1/api")
        monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU123")

        item = operator_action_queue._op3_ibkr_creds()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["ibkr_cp_base_url"] is True

    def test_tastytrade_probe_uses_runtime_consumed_tasty_keys(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setenv("TASTY_API_BASE_URL", "https://api.cert.tastyworks.com")
        monkeypatch.setenv("TASTY_ACCOUNT_NUMBER", "5WX123")
        monkeypatch.setenv("TASTY_SESSION_TOKEN", "token")

        item = operator_action_queue._op4_tastytrade_creds()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["TASTY_API_BASE_URL"] is True


class TestStrategyResearchCandidateProbe:
    def test_research_warnings_mark_op16_blocked_with_next_commands(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from eta_engine.scripts import operator_action_queue

        assignments = (
            SimpleNamespace(
                bot_id="eth_perp",
                strategy_id="eth_corb_v4",
                extras={"promotion_status": "research_candidate"},
            ),
            SimpleNamespace(
                bot_id="btc_hybrid",
                strategy_id="btc_corb_v3",
                extras={"promotion_status": "promoted"},
            ),
        )

        monkeypatch.setattr(
            "eta_engine.strategies.per_bot_registry.ASSIGNMENTS",
            assignments,
        )
        monkeypatch.setattr(
            "eta_engine.scripts.paper_live_launch_check._audit_bot",
            lambda assignment: {
                "bot_id": assignment.bot_id,
                "strategy_id": assignment.strategy_id,
                "status": "WARN",
                "warnings": [
                    "research_candidate (strict gate failed; OOS +1.929)",
                ],
                "evidence": {"candidate_agg_oos_sharpe": 1.929},
            },
        )

        item = operator_action_queue._op16_strategy_research_candidates()

        assert item.verdict == VERDICT_BLOCKED
        assert "1 research candidate bot(s)" in item.detail
        assert item.evidence["overall_severity"] == "amber"
        assert item.evidence["blocked_bots"] == ["eth_perp"]
        assert item.evidence["blockers"][0]["next_commands"] == [
            "python -m eta_engine.scripts.paper_live_launch_check --bots eth_perp --json",
        ]

    def test_no_research_warnings_marks_op16_done(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr(
            "eta_engine.strategies.per_bot_registry.ASSIGNMENTS",
            (
                SimpleNamespace(
                    bot_id="btc_hybrid",
                    strategy_id="btc_corb_v3",
                    extras={"promotion_status": "promoted"},
                ),
            ),
        )

        item = operator_action_queue._op16_strategy_research_candidates()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["overall_severity"] == "green"

    def test_research_blockers_prioritize_actionable_near_pass(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from eta_engine.scripts import operator_action_queue

        assignments = (
            SimpleNamespace(
                bot_id="mnq_futures",
                strategy_id="mnq_orb_v2",
                extras={"promotion_status": "research_candidate"},
            ),
            SimpleNamespace(
                bot_id="eth_sage_daily",
                strategy_id="eth_corb_sage_daily_v1",
                extras={"promotion_status": "research_candidate"},
            ),
        )
        payloads = {
            "mnq_futures": {
                "bot_id": "mnq_futures",
                "strategy_id": "mnq_orb_v2",
                "status": "WARN",
                "warnings": ["research_candidate (strict gate failed; OOS -2.958)"],
                "evidence": {
                    "full_history_smoke": {
                        "agg_oos_sharpe": -2.958,
                        "dsr_pass_fraction": 0.132,
                    },
                },
            },
            "eth_sage_daily": {
                "bot_id": "eth_sage_daily",
                "strategy_id": "eth_corb_sage_daily_v1",
                "status": "WARN",
                "warnings": ["research_candidate (strict gate failed; OOS +3.877)"],
                "evidence": {
                    "candidate_agg_oos_sharpe": 3.877,
                    "candidate_dsr_pass_fraction": 0.571,
                    "provider_backed": True,
                },
            },
        }
        monkeypatch.setattr(
            "eta_engine.strategies.per_bot_registry.ASSIGNMENTS",
            assignments,
        )
        monkeypatch.setattr(
            "eta_engine.scripts.paper_live_launch_check._audit_bot",
            lambda assignment: payloads[assignment.bot_id],
        )

        item = operator_action_queue._op16_strategy_research_candidates()

        assert item.evidence["blocked_bots"] == ["eth_sage_daily", "mnq_futures"]
        assert "first=eth_sage_daily" in item.detail


class TestCryptoSeedProbeUnderSyntheticState:
    def test_non_edge_exposure_readiness_marks_done(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from eta_engine.scripts import operator_action_queue

        assignment = SimpleNamespace(bot_id="crypto_seed")
        monkeypatch.setattr(
            "eta_engine.strategies.per_bot_registry.get_for_bot",
            lambda bot_id: assignment if bot_id == "crypto_seed" else None,
        )
        monkeypatch.setattr(
            "eta_engine.scripts.paper_live_launch_check._audit_bot",
            lambda _assignment: {
                "bot_id": "crypto_seed",
                "status": "READY",
                "warnings": [],
                "issues": [],
                "evidence": {"launch_role": "non_edge_exposure"},
            },
        )

        item = operator_action_queue._op15_crypto_seed()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["bot_id"] == "crypto_seed"
        assert "non-edge BTC exposure accumulator" in item.detail

    def test_missing_crypto_seed_assignment_stays_blocked(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr("eta_engine.strategies.per_bot_registry.get_for_bot", lambda _bot_id: None)

        item = operator_action_queue._op15_crypto_seed()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["missing_assignment"] is True


class TestTradovateDormancyPolicy:
    def test_tradovate_dormant_is_policy_done(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr(operator_action_queue, "_read_dormant_brokers", lambda: {"tradovate"})

        item = operator_action_queue._op10_tradovate_dormancy()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["policy"]["active_primary"] == "IBKR"
        assert item.evidence["policy"]["tradovate"] == "dormant"

    def test_tradovate_un_dormant_is_blocked_without_explicit_reactivation(
        self, monkeypatch
    ) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr(operator_action_queue, "_read_dormant_brokers", lambda: set())

        item = operator_action_queue._op10_tradovate_dormancy()

        assert item.verdict == VERDICT_BLOCKED
        assert "Tradovate appears active" in item.detail


class TestFutureScopeItems:
    def test_killverdict_synthesis_is_observed_until_live_paper_empirics(self) -> None:
        from eta_engine.scripts import operator_action_queue

        item = operator_action_queue._op11_killverdict_synthesis()

        assert item.verdict == VERDICT_OBSERVED
        assert item.evidence["launch_blocker"] is False
        assert ">=30 days" in item.detail

    def test_per_bot_drift_is_observed_until_multi_account_scope(self) -> None:
        from eta_engine.scripts import operator_action_queue

        item = operator_action_queue._op12_per_bot_drift()

        assert item.verdict == VERDICT_OBSERVED
        assert item.evidence["current_scope"] == "single_account"
        assert "not a current launch block" in item.detail


class TestVpsFailoverProbeUnderSyntheticState:
    def test_green_summary_marks_done(self, monkeypatch) -> None:
        from eta_engine.scripts import vps_failover_summary
        from eta_engine.scripts.operator_action_queue import _op18_vps_failover_readiness

        monkeypatch.setattr(
            vps_failover_summary,
            "build_summary",
            lambda **_kwargs: {
                "overall_severity": "green",
                "counts": {"red": 0, "amber": 0, "green": 8, "skip": 0},
                "blockers": [],
                "generated_at": "2026-04-29T00:00:00+00:00",
                "exit_code": 0,
            },
        )

        item = _op18_vps_failover_readiness()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["overall_severity"] == "green"

    def test_amber_summary_marks_blocked_with_next_command(self, monkeypatch) -> None:
        from eta_engine.scripts import vps_failover_summary
        from eta_engine.scripts.operator_action_queue import _op18_vps_failover_readiness

        monkeypatch.setattr(
            vps_failover_summary,
            "build_summary",
            lambda **_kwargs: {
                "overall_severity": "amber",
                "counts": {"red": 0, "amber": 1, "green": 7, "skip": 0},
                "blockers": [
                    {
                        "name": "secrets_present",
                        "summary": ".env missing",
                        "next_commands": ["cp .env.example .env && chmod 600 .env"],
                    }
                ],
                "generated_at": "2026-04-29T00:00:00+00:00",
                "exit_code": 2,
            },
        )

        item = _op18_vps_failover_readiness()

        assert item.verdict == VERDICT_BLOCKED
        assert "next: cp .env.example .env && chmod 600 .env" in item.detail
        assert item.evidence["blockers"][0]["name"] == "secrets_present"
