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

    def test_collects_all_twenty_op_items(self):
        items = collect_items()
        assert len(items) == 20

    def test_op_ids_are_sequential(self):
        items = collect_items()
        op_ids = [i.op_id for i in items]
        expected = [f"OP-{n}" for n in range(1, 21)]
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
        for n in range(1, 21):
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
        assert len(payload["items"]) == 20
        # summary counts must equal items count
        total = sum(payload["summary"].values())
        assert total == 20

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

    def test_status_needs_auth_marks_observed_not_launch_blocking(self, monkeypatch) -> None:
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
        assert all(i.verdict == VERDICT_OBSERVED for i in items)
        assert all(i.evidence["launch_blocker"] is False for i in items)
        assert all("not blocking trading launch readiness" in i.detail for i in items)

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

    def test_ibkr_probe_reads_canonical_eta_engine_env_file(self, monkeypatch, tmp_path) -> None:
        from eta_engine.core.secrets import SecretsManager
        from eta_engine.scripts import operator_action_queue

        monkeypatch.delenv("IBKR_CP_BASE_URL", raising=False)
        monkeypatch.delenv("IBKR_ACCOUNT_ID", raising=False)
        monkeypatch.setattr(SecretsManager, "_try_keyring", lambda _self, _key: None)
        monkeypatch.setattr(operator_action_queue, "ROOT", tmp_path)
        (tmp_path / ".env").write_text(
            "\n".join(
                [
                    "IBKR_CP_BASE_URL=https://127.0.0.1:5000/v1/api",
                    "IBKR_ACCOUNT_ID=DU123",
                ]
            ),
            encoding="utf-8",
        )

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

    def test_missing_tastytrade_creds_are_observed_not_launch_blocking(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr(operator_action_queue, "_env_key_present", lambda _name: False)

        item = operator_action_queue._op4_tastytrade_creds()

        assert item.verdict == VERDICT_OBSERVED
        assert item.evidence["launch_blocker"] is False
        assert "not blocking first live tick" in item.detail


class TestGatewayRuntimeProbe:
    def test_gateway_exe_present_accepts_ibc_renamed_binary(self, tmp_path) -> None:
        from eta_engine.scripts import operator_action_queue

        gateway_dir = tmp_path / "1046"
        gateway_dir.mkdir()
        (gateway_dir / "ibgateway1.exe").write_text("", encoding="utf-8")

        assert operator_action_queue._gateway_exe_present(gateway_dir) is True

    def test_unfunded_tastytrade_fallback_is_observed_not_launch_blocking(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr(operator_action_queue, "_env_key_present", lambda _name: False)

        item = operator_action_queue._op2_fund_tastytrade()

        assert item.verdict == VERDICT_OBSERVED
        assert item.evidence["role"] == "secondary_fallback"
        assert item.evidence["launch_blocker"] is False

    def test_missing_telegram_creds_are_blocked_but_not_launch_blocking(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr(operator_action_queue, "_env_key_present", lambda _name: False)

        item = operator_action_queue._op5_telegram_creds()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["launch_blocker"] is False
        assert item.evidence["role"] == "alerts_transport"
        assert "does not block the paper_live trading path" in item.detail


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
                "evidence": {
                    "baseline_present": False,
                    "candidate_agg_oos_sharpe": 1.929,
                },
            },
        )

        item = operator_action_queue._op16_strategy_research_candidates()

        assert item.verdict == VERDICT_BLOCKED
        assert "1 research candidate bot(s)" in item.detail
        assert item.evidence["overall_severity"] == "amber"
        assert item.evidence["launch_blocker"] is False
        assert item.evidence["launch_role"] == "strategy_optimization_backlog"
        assert item.evidence["blocked_bots"] == ["eth_perp"]
        assert item.evidence["blockers"][0]["next_commands"] == [
            "python -m eta_engine.scripts.run_research_grid --source registry --bots eth_perp --report-policy runtime",
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

    def test_deactivated_non_edge_assignment_marks_done_when_audit_is_clean(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from eta_engine.scripts import operator_action_queue

        assignment = SimpleNamespace(
            bot_id="crypto_seed",
            extras={
                "promotion_status": "non_edge_strategy",
                "non_edge_reason": "DCA accumulator, not alpha edge.",
                "deactivated": True,
            },
        )
        monkeypatch.setattr(
            "eta_engine.strategies.per_bot_registry.get_for_bot",
            lambda bot_id: assignment if bot_id == "crypto_seed" else None,
        )
        monkeypatch.setattr(
            "eta_engine.scripts.paper_live_launch_check._audit_bot",
            lambda _assignment: {
                "bot_id": "crypto_seed",
                "status": "READY",
                "promotion_status": "deactivated",
                "warnings": [],
                "issues": [],
            },
        )

        item = operator_action_queue._op15_crypto_seed()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["evidence"]["launch_role"] == "non_edge_exposure"
        assert item.evidence["evidence"]["registry_deactivated"] is True

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

    def test_tradovate_un_dormant_is_blocked_without_explicit_reactivation(self, monkeypatch) -> None:
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

    def test_amber_summary_marks_observed_non_launch_warning(self, monkeypatch) -> None:
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

        assert item.verdict == VERDICT_OBSERVED
        assert item.evidence["launch_blocker"] is False
        assert "AMBER failover warning" in item.detail
        assert "next: cp .env.example .env && chmod 600 .env" in item.detail
        assert item.evidence["blockers"][0]["name"] == "secrets_present"

    def test_red_summary_marks_launch_blocker_with_next_command(self, monkeypatch) -> None:
        from eta_engine.scripts import vps_failover_summary
        from eta_engine.scripts.operator_action_queue import _op18_vps_failover_readiness

        monkeypatch.setattr(
            vps_failover_summary,
            "build_summary",
            lambda **_kwargs: {
                "overall_severity": "red",
                "counts": {"red": 1, "amber": 0, "green": 7, "skip": 0},
                "blockers": [
                    {
                        "name": "deploy_files_present",
                        "summary": "required deploy scripts missing",
                        "next_commands": ["python -m eta_engine.scripts.vps_failover_summary --json"],
                    }
                ],
                "generated_at": "2026-04-29T00:00:00+00:00",
                "exit_code": 2,
            },
        )

        item = _op18_vps_failover_readiness()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["launch_blocker"] is True
        assert "RED failover blocker" in item.detail
        assert "next: python -m eta_engine.scripts.vps_failover_summary --json" in item.detail


class TestIbGateway1046RuntimeProbe:
    def test_gateway_runtime_blocks_local_desktop_recovery_when_host_is_not_gateway_authority(
        self, monkeypatch
    ) -> None:
        from eta_engine.scripts import operator_action_queue

        states = {
            "ibgateway_install.json": {"installed": True},
            "ibgateway_repair.json": {
                "single_source": {
                    "gateway_task_canonical": False,
                    "task_states": {"ETA-IBGateway": "Disabled"},
                },
            },
            "ibgateway_reauth.json": {
                "status": "non_authoritative_gateway_host",
                "gateway_authority": {
                    "allowed": False,
                    "computer_name": "ETA",
                    "reason": "authority_marker_missing",
                },
            },
            "tws_watchdog.json": {
                "healthy": False,
                "details": {"handshake_ok": False},
            },
        }
        monkeypatch.setattr(operator_action_queue, "_read_runtime_state", lambda name: states.get(name, {}))
        monkeypatch.setattr(operator_action_queue, "_gateway_exe_present", lambda: True)

        item = operator_action_queue._op19_ibgateway_1046_runtime()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["overall_severity"] == "red"
        assert item.evidence["non_authoritative_gateway_host"] is True
        assert item.evidence["gateway_authority"]["allowed"] is False
        assert "not the VPS Gateway authority" in item.detail
        next_commands = item.evidence["blockers"][0]["next_commands"]
        assert all(command.startswith("On the VPS only:") for command in next_commands)
        assert not any("repair_ibgateway_vps.ps1" in command for command in next_commands)

    def test_gateway_runtime_prioritizes_missing_ibc_credentials(self, monkeypatch, tmp_path) -> None:
        from eta_engine.scripts import operator_action_queue

        gateway_dir = tmp_path / "1037"
        gateway_dir.mkdir()
        (gateway_dir / "ibgateway1.exe").write_text("", encoding="utf-8")
        states = {
            "ibgateway_install.json": {},
            "ibgateway_repair.json": {
                "gateway_dir": str(gateway_dir),
                "single_source": {
                    "gateway_task_canonical": True,
                    "task_states": {"ETA-IBGateway": "Ready"},
                },
            },
            "ibgateway_reauth.json": {
                "status": "missing_ibc_credentials",
                "operator_action": "Seed IBC credentials with set_ibc_credentials.ps1 -PromptForPassword.",
                "operator_action_required": True,
                "credential_status": {
                    "ready": False,
                    "has_user_id": True,
                    "has_password": False,
                    "password_file_placeholder": True,
                },
            },
            "tws_watchdog.json": {"healthy": False, "details": {"handshake_ok": False}},
        }
        monkeypatch.setattr(operator_action_queue, "_read_runtime_state", lambda name: states.get(name, {}))
        monkeypatch.setattr(
            operator_action_queue,
            "_gateway_exe_present",
            lambda path=None: path == gateway_dir,
        )

        item = operator_action_queue._op19_ibgateway_1046_runtime()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["gateway_exe_present"] is True
        assert item.evidence["overall_severity"] == "red"
        assert "IBC credentials" in item.detail
        next_commands = item.evidence["blockers"][0]["next_commands"]
        assert "set_ibc_credentials.ps1 -PromptForPassword" in next_commands[0]

    def test_missing_gateway_install_is_red_blocker_with_guarded_install_command(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        states = {
            "ibgateway_install.json": {
                "authenticode_status": "NotSigned",
                "installer_sha256": "ABC123",
            },
            "ibgateway_repair.json": {
                "single_source": {
                    "gateway_task_canonical": False,
                    "task_states": {"ETA-IBGateway": "Missing"},
                },
            },
            "ibgateway_reauth.json": {"status": "missing_recovery_task"},
            "tws_watchdog.json": {"healthy": False},
        }
        monkeypatch.setattr(operator_action_queue, "_read_runtime_state", lambda name: states.get(name, {}))
        monkeypatch.setattr(operator_action_queue, "_gateway_exe_present", lambda: False)

        item = operator_action_queue._op19_ibgateway_1046_runtime()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["overall_severity"] == "red"
        assert item.evidence["gateway_exe_present"] is False
        assert "IB Gateway 10.46 is not installed" in item.detail
        assert "NotSigned" in item.detail
        next_commands = item.evidence["blockers"][0]["next_commands"]
        assert "install_ibgateway_1046.ps1" in next_commands[0]
        assert "-Install -RepairAfterInstall" in next_commands[0]
        assert "-AllowUnsignedInstaller" not in next_commands[0]
        assert item.evidence["allow_unsigned_requires_source_confirmation"] is True
        assert item.evidence["blockers"][0]["evidence"]["allow_unsigned_requires_source_confirmation"] is True

    def test_gateway_runtime_marks_done_when_api_handshake_is_healthy(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        states = {
            "ibgateway_install.json": {"installed": True},
            "ibgateway_repair.json": {
                "single_source": {
                    "gateway_task_canonical": True,
                    "task_states": {"ETA-IBGateway": "Ready"},
                },
            },
            "ibgateway_reauth.json": {"status": "healthy"},
            "tws_watchdog.json": {
                "healthy": True,
                "details": {"handshake_ok": True},
            },
        }
        monkeypatch.setattr(operator_action_queue, "_read_runtime_state", lambda name: states.get(name, {}))
        monkeypatch.setattr(operator_action_queue, "_gateway_exe_present", lambda: True)

        item = operator_action_queue._op19_ibgateway_1046_runtime()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["overall_severity"] == "green"
        assert item.evidence["task_canonical"] is True
        assert item.evidence["handshake_ok"] is True

    def test_gateway_runtime_reports_task_drift_when_live_api_is_healthy(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        states = {
            "ibgateway_install.json": {"installed": True},
            "ibgateway_repair.json": {
                "single_source": {
                    "gateway_task_canonical": False,
                    "task_states": {"ETA-IBGateway": "Ready"},
                },
                "tasks": {
                    "ETA-IBGateway": "failed: The user name or password is incorrect.",
                },
            },
            "ibgateway_reauth.json": {"status": "healthy"},
            "tws_watchdog.json": {
                "healthy": True,
                "details": {"handshake_ok": True},
            },
        }
        monkeypatch.setattr(operator_action_queue, "_read_runtime_state", lambda name: states.get(name, {}))
        monkeypatch.setattr(operator_action_queue, "_gateway_exe_present", lambda: True)

        item = operator_action_queue._op19_ibgateway_1046_runtime()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["overall_severity"] == "red"
        assert item.evidence["task_canonical"] is False
        assert item.evidence["handshake_ok"] is True
        assert "TWS API 4002 are healthy" in item.detail
        assert "The user name or password is incorrect" in item.detail
        next_commands = item.evidence["blockers"][0]["next_commands"]
        assert "-UseIbc" in next_commands[0]


class TestSupervisorBrokerReconcileProbe:
    def test_mismatch_blocks_launch_with_human_next_action(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        def fake_read(path):
            if path == operator_action_queue.workspace_roots.ETA_VPS_OPS_HARDENING_AUDIT_PATH:
                return {
                    "next_actions": [
                        (
                            "Do not unlock new entries: reconcile broker/supervisor positions "
                            "(broker-only: MCL, MYM; divergent: MNQ) before clearing the supervisor entry halt"
                        )
                    ],
                    "safety_gates": {
                        "supervisor_reconcile": {
                            "ready": False,
                            "status": "BLOCKED_BROKER_SUPERVISOR_RECONCILE",
                            "source": "supervisor_broker_reconcile_heartbeat",
                            "mismatch_count": 3,
                            "broker_only_symbols": ["MCL", "MYM"],
                            "supervisor_only_symbols": [],
                            "divergent_symbols": ["MNQ"],
                        }
                    },
                }
            return {}

        monkeypatch.setattr(operator_action_queue, "_read_json_path", fake_read)

        item = operator_action_queue._op20_supervisor_broker_reconcile()

        assert item.verdict == VERDICT_BLOCKED
        assert item.evidence["overall_severity"] == "red"
        assert item.evidence["launch_blocker"] is True
        assert item.evidence["broker_only_symbols"] == ["MCL", "MYM"]
        assert item.evidence["divergent_symbols"] == ["MNQ"]
        assert item.evidence["order_action_allowed"] is False
        first_action = item.evidence["blockers"][0]["next_commands"][0]
        assert first_action.startswith("Do not unlock new entries")
        assert "MNQ" in item.detail

    def test_raw_reconcile_match_marks_done(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        def fake_read(path):
            if path == operator_action_queue.workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH:
                return {
                    "source": "supervisor_broker_reconcile_heartbeat",
                    "mismatch_count": 0,
                    "broker_only": [],
                    "supervisor_only": [],
                    "divergent": [],
                    "order_action_allowed": False,
                }
            return {}

        monkeypatch.setattr(operator_action_queue, "_read_json_path", fake_read)

        item = operator_action_queue._op20_supervisor_broker_reconcile()

        assert item.verdict == VERDICT_DONE
        assert item.evidence["overall_severity"] == "green"
        assert item.evidence["launch_blocker"] is False
        assert item.evidence["mismatch_count"] == 0

    def test_missing_reconcile_artifact_is_unknown_not_silent(self, monkeypatch) -> None:
        from eta_engine.scripts import operator_action_queue

        monkeypatch.setattr(operator_action_queue, "_read_json_path", lambda _path: {})

        item = operator_action_queue._op20_supervisor_broker_reconcile()

        assert item.verdict == VERDICT_UNKNOWN
        assert item.evidence["source"] == "missing_reconcile_artifact"
        assert "No current broker/supervisor reconcile artifact" in item.detail
