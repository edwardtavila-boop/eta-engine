"""
Tests for the 10-optimization bundle:
  - META_UPGRADE task handler (#5)
  - PROMPT_WARMUP handler (#7 -- gracefully skips without API key)
  - TelegramAdapter (#9)
  - status_page HTML exists + is well-formed (#10)
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# #5 META_UPGRADE
# ---------------------------------------------------------------------------


class TestMetaUpgrade:
    def test_handler_registered(self):
        from eta_engine.brain.avengers import BackgroundTask
        from eta_engine.deploy.scripts.run_task import HANDLERS

        assert BackgroundTask.META_UPGRADE in HANDLERS

    def test_task_has_owner_and_cadence(self):
        from eta_engine.brain.avengers import (
            TASK_CADENCE,
            TASK_OWNERS,
            BackgroundTask,
        )

        assert TASK_OWNERS[BackgroundTask.META_UPGRADE] == "ALFRED"
        assert TASK_CADENCE[BackgroundTask.META_UPGRADE].startswith("30 4")

    def test_handler_skips_when_not_a_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ETA_REPO_DIR", str(tmp_path))
        from eta_engine.deploy.scripts.run_task import _task_meta_upgrade

        result = _task_meta_upgrade(tmp_path / "state")
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        # Re-run after state dir exists
        result = _task_meta_upgrade(tmp_path / "state")
        assert result.get("skipped") is True


# ---------------------------------------------------------------------------
# #7 PROMPT_WARMUP
# ---------------------------------------------------------------------------


class TestPromptWarmup:
    def test_skips_without_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from eta_engine.deploy.scripts.run_task import _task_prompt_warmup

        out = _task_prompt_warmup(tmp_path)
        assert out.get("skipped") is True
        assert "no API key" in out.get("reason", "")


# ---------------------------------------------------------------------------
# #9 Telegram adapter
# ---------------------------------------------------------------------------


class TestTelegramAdapter:
    def test_from_env_returns_none_if_unconfigured(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        from eta_engine.deploy.scripts.telegram_alerts import TelegramAdapter

        assert TelegramAdapter.from_env() is None

    def test_from_env_builds_adapter(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        from eta_engine.deploy.scripts.telegram_alerts import TelegramAdapter

        adapter = TelegramAdapter.from_env(state_dir=tmp_path)
        assert adapter is not None
        assert adapter.bot_token == "123:abc"
        assert adapter.chat_id == "999"
        assert adapter.api_base == "https://api.telegram.org/bot123:abc"

    def test_voice_sender_wraps_send(self, monkeypatch, tmp_path):
        from eta_engine.deploy.scripts.telegram_alerts import TelegramAdapter

        adapter = TelegramAdapter("t", "c", state_dir=tmp_path)
        sent = []
        adapter.send = lambda text, priority="INFO", **k: sent.append((text, priority)) or {"ok": True}
        fn = adapter.as_voice_sender()
        fn("TELEGRAM", "hello", "CRITICAL")
        assert sent == [("hello", "CRITICAL")]

    def test_send_records_to_state(self, monkeypatch, tmp_path):
        # Mock httpx.post to avoid network
        import httpx

        import eta_engine.deploy.scripts.telegram_alerts as mod

        class FakeResp:
            def json(self):
                return {"ok": True, "result": {"message_id": 1}}

        def fake_post(*args, **kwargs):
            return FakeResp()

        monkeypatch.setattr(httpx, "post", fake_post)

        adapter = mod.TelegramAdapter("t", "c", state_dir=tmp_path)
        result = adapter.send("test msg", priority="WARN")
        assert result.get("ok")
        log_path = tmp_path / "telegram_alerts.jsonl"
        assert log_path.exists()
        line = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert line["priority"] == "WARN"
        assert line["ok"] is True


# ---------------------------------------------------------------------------
# #10 status page
# ---------------------------------------------------------------------------


# Dashboard contract — split between two surfaces:
#   1. ``index.html`` was rewritten in commit 6a7b9af ("Live fleet
#      dashboard: clean HTML") into a self-contained inline-CSS/JS page.
#      Its anchors are what we test in test_index_has_expected_anchors.
#   2. The ``js/*.js`` files (panels, command_center, bot_fleet, etc.)
#      still exist with the original architectural contracts. They are
#      served by dashboard_api but NOT loaded by the new index.html.
#      The tests below validate the JS-side contracts (still active
#      surface for any operator who hits a JS file directly), but no
#      longer assert on index.html-side panel anchors that were dropped
#      in the rewrite.
#
# If/when the index.html is re-wired to load the JS modules, the
# JS-side contracts here will already be green — those tests act as
# a forward-compatible spec.


class TestStatusPage:
    def test_index_exists(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "index.html"
        assert path.exists()

    def test_index_has_expected_anchors(self):
        """Smoke-test the new clean-HTML dashboard's actual contract.

        Rewritten in commit 6a7b9af. Asserts the live anchor set the
        page actually renders + the API endpoint it fetches. If a future
        rewrite drops one of these elements, this test will fail loudly.
        """
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "index.html"
        html = path.read_text(encoding="utf-8")

        # Header / metadata
        assert "Evolutionary Trading Algo | Command Center" in html
        assert 'lang="en"' in html
        assert 'name="viewport"' in html

        # Stat-card anchors the dashboard JS writes into
        for anchor in (
            'id="todayPnl"',
            'id="totalPnl"',
            'id="totalPnlSub"',
            'id="totalTrades"',
            'id="winRate"',
            'id="cumulativeR"',
            'id="cumulativeRSub"',
            'id="activeBots"',
            'id="activeBotsSub"',
            'id="totalBots"',
            'id="riskLevel"',
            'id="riskBar"',
            'id="riskSub"',
            'id="jarvisMode"',
            'id="gatewayStatus"',
            'id="gatewayStatusSub"',
            'id="brokerRouterStatus"',
            'id="brokerRouterSub"',
            'id="bracketAuditStatus"',
            'id="bracketAuditSub"',
            'id="paperLiveStatus"',
            'id="paperLiveSub"',
            'id="exitWatchStatus"',
            'id="exitWatchSub"',
            'id="operatorQueueStatus"',
            'id="operatorQueueSub"',
            'id="signalCadenceStatus"',
            'id="signalCadenceSub"',
            'id="opsProxyStatus"',
            'id="opsProxySub"',
            'id="opsWatchdogStatus"',
            'id="opsWatchdogSub"',
        ):
            assert anchor in html, f"missing dashboard anchor: {anchor}"

        # Bot fleet table + PnL chart shell
        assert 'id="botTable"' in html
        assert 'id="pnlChart"' in html
        assert 'id="pnlHistoryLabel"' in html
        assert 'id="apiSourceText"' in html

        # Live data fetch contract: dashboard pulls from /api/bot-fleet
        assert "/api/bot-fleet" in html
        assert "const API = '/api/bot-fleet';" in html
        assert "target_exit_summary" in html
        assert "broker_bracket_audit" in html
        assert "primary_unprotected_position" in html
        assert "unprotected_positions" in html
        assert "unprotectedSymbolsText" in html
        assert "BLOCKED_UNBRACKETED_EXPOSURE" in html
        assert "missing broker OCO" in html
        assert "brokerBracketActionLabels" in html
        assert "broker_bracket_operator_action_labels" in html
        assert "operator choice" in html
        assert "Verify broker OCO coverage" in html
        assert "Flatten unprotected paper exposure" in html
        assert 'id="bracketActionStrip"' in html
        assert "function renderBracketActionStrip" in html
        assert "bracket blocker actions" in html
        assert "prop dry-run blocked" in html
        assert "function bracketPrimaryExposureText" in html
        assert "qty " in html
        assert "market value" in html
        assert "unrealized" in html
        assert "primary unprotected" in html
        assert "position_staleness" in html
        assert "force_flatten_due_count" in html
        assert "seconds_to_next_action" in html
        assert "formatDurationSeconds" in html
        assert "next review" in html
        assert "review due now" in html
        assert "past max-hold; force flatten due" in html
        assert "tighten_stop_due" in html
        assert "Tighten Stop" in html
        assert "operator ack due" in html
        assert "operator ack due; oldest" in html
        missing_priority_idx = html.index("targetExitStatus === 'missing_brackets'")
        require_ack_idx = html.index("requireAckDue > 0")
        assert missing_priority_idx < require_ack_idx
        assert "broker bracket missing; verify broker OCO or manage flatten manually" in html
        assert "exit watch active; next review" in html
        assert "drawdown + exit-watch SLA" in html
        assert "paper_watching" in html
        assert "paper-local open" in html
        assert "gateway healthy; broker data probe timed out" in html
        assert "probe_ok_watchdog_stale" in html
        assert "Proxy OK" in html
        assert "watchdog stale" in html
        assert "https://jarvis.evolutionarytradingalgo.com/api/bot-fleet" in html
        assert "const OPERATOR_QUEUE_API = '/api/jarvis/operator_queue';" in html
        assert "const PAPER_LIVE_API = '/api/jarvis/paper_live_transition';" in html
        assert "const DASHBOARD_DIAGNOSTICS_API = '/api/dashboard/diagnostics';" in html
        assert "command_center_watchdog" in html
        assert "Ops Watchdog" in html
        assert "endpointCandidates(" in html
        assert "cache: 'no-store'" in html
        assert "FETCH_TIMEOUT_MS" in html
        assert "AUX_FETCH_TIMEOUT_MS" in html
        assert "AbortController" in html
        assert "Promise.any" in html
        assert "loadInFlight" in html
        assert "Last data retained - reconnecting" in html

        # Trade/activity visibility contract: supervisor signal updates are
        # displayed separately from actual fill-backed trades.
        assert "<th>Last Activity</th>" in html
        assert 'colspan="12"' in html
        assert "last_trade_ts" in html
        assert "last_signal_ts" in html
        assert "signal_cadence" in html
        assert "live_wr_today" in html
        assert "signalUpdates" in html
        assert "broker_gateway" in html
        assert "gateway_crash" in html
        assert "broker_router" in html
        assert "value === null || value === undefined || value === ''" in html
        assert "isRuntimeActiveBot" in html
        assert "readiness/staged rows" in html
        assert "lifetime ledger not attached; today shown above" in html
        assert "function fleetEquityLifetimeEvidence" in html
        assert "function closeEvidenceSummary" in html
        assert "function closeHistoryWindowPayload" in html
        assert "rootPayload?.close_history" in html
        assert "closeHistoryWindowPayload(liveBroker, selectedCloseHistoryWindow, d)" in html
        assert "function renderPnlWindowControls" in html
        assert "data-close-window=\"mtd\"" in html
        assert "data-close-window=\"all\"" in html
        assert "MTD closed outcomes" in html
        assert "MTD Close History" in html
        assert "selected-window close history" in html
        assert "broker position rows unavailable" in html
        assert "broker-reported open positions without row detail" in html
        assert 'id="pnlWindowStrip"' in html
        assert "function renderPnlWindowStrip" in html
        assert "selected-window realized PnL" in html
        assert "Closed PnL" in html
        assert "function winRateSourceText" in html
        assert "close-ledger outcomes" in html
        assert "function pnlHistoryEvidence" in html
        assert "Closed PnL" in html
        assert "lifetime ledger pending" in html
        assert "closed outcome" in html
        assert "closed outcomes in ledger" in html
        assert "selected-window closed outcomes" in html
        assert '<details class="collapsible-section" id="opsTruthSection">' in html
        assert "source === 'supervisor_heartbeat'" in html
        assert "source === 'fills_intraday'" in html
        assert "total_pnl_is_lifetime" in html
        assert "value: finiteNumber(equitySummary.total_pnl)" not in html
        assert "function botSleeveLabel" in html
        assert "priority_bucket" in html
        assert "primary_edges" in html
        assert "edge_thesis" in html
        assert "exit_playbook" in html
        assert "risk_playbook" in html
        assert "daily_focus" in html
        assert "CME Crypto Futures" in html
        assert "Spot Crypto" in html
        assert "edge-chip-row" in html
        assert "allocation-doctrine" in html
        assert "waiting for realized-R closed trades" in html
        assert "close ledger present; realized R not attached yet" in html
        assert "open/no fill qty" in html
        assert "open/no-fill" in html
        assert "actualRouterFills" in html
        assert "paperLaunchBlocked" in html
        assert "paperTransitionLaunchBlocked > 0" in html
        assert "liveModes" in html
        assert "historical_reasons" in html or "history:" in html

        # Status indicator + clock (live freshness cues)
        assert 'id="statusDot"' in html
        assert 'id="statusText"' in html
        assert 'id="clockText"' in html

        # No hardcoded secrets or debug leftovers
        assert "console.log" not in html

    def test_command_center_renders_operator_queue_panel(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "js" / "command_center.js"
        js = path.read_text(encoding="utf-8")
        assert "OperatorQueuePanel" in js
        assert "/api/jarvis/operator_queue" in js
        assert "top-operator-queue" in js
        assert "next_actions" in js
        assert "launch_blocked_count" in js
        assert "top_launch_blockers" in js

    def test_command_center_renders_paper_live_transition_panel(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "js" / "command_center.js"
        js = path.read_text(encoding="utf-8")
        assert "PaperLiveTransitionPanel" in js
        assert "/api/jarvis/paper_live_transition" in js
        assert "critical_ready" in js
        assert "paper_ready_bots" in js
        assert "operator_queue_first_launch_blocker_op_id" in js

    def test_command_center_renders_bot_strategy_readiness_panel(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        js = (root / "js" / "command_center.js").read_text(encoding="utf-8")
        css = (root / "theme.css").read_text(encoding="utf-8")

        assert "BotStrategyReadinessPanel" in js
        assert "/api/jarvis/bot_strategy_readiness" in js
        assert "top-bot-readiness" in js
        assert "paper ready" in js
        assert "launch_lanes" in js
        assert "#top-bot-readiness" in css
        assert "setAttribute('data-readiness', blockedData > 0 ? 'blocked' : 'ready')" in js
        assert "setAttribute('data-readiness', 'degraded')" in js

    def test_command_center_renders_strategy_supercharge_panel(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "js" / "command_center.js").read_text(encoding="utf-8")
        panels = (root / "js" / "panels.js").read_text(encoding="utf-8")

        assert "StrategySuperchargeManifestPanel" in js
        assert "/api/jarvis/strategy_supercharge_manifest" in js
        assert "next_batch" in js
        assert "A+C now" in js
        assert "StrategySuperchargeResultsPanel" in js
        assert "cc-strategy-supercharge-results" in js
        assert "/api/jarvis/strategy_supercharge_results" in js
        assert "near_misses" in js
        assert "retune_queue" in js
        assert 'data-panel-id="cc-strategy-supercharge-results"' in html
        assert "/api/jarvis/strategy_supercharge_results" in html
        assert "strategyNearMisses" in html
        assert "strategyRetuneQueue" in html
        assert "id.includes('supercharge')" in panels

    def test_status_page_mobile_fleet_and_equity_contracts(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        css = (root / "theme.css").read_text(encoding="utf-8")
        bot_fleet = (root / "js" / "bot_fleet.js").read_text(encoding="utf-8")
        panels = (root / "js" / "panels.js").read_text(encoding="utf-8")
        auth = (root / "js" / "auth.js").read_text(encoding="utf-8")

        # Batches 5-7: phone roster cards, equity sizing, live freshness cues.
        assert "@media (max-width: 760px)" in css
        assert "@media (max-width: 520px)" in css
        assert ".mobile-card-table" in css
        assert "content: attr(data-label)" in css
        assert ".mobile-chart-shell" in css
        assert "data-label=\"Bot\"" in bot_fleet
        assert "data-label=\"Day PnL\"" in bot_fleet
        assert "data-label=\"Last Trade\"" in bot_fleet
        assert "data-label=\"Readiness\"" in bot_fleet
        assert "formatBotStrategyReadiness" in bot_fleet
        assert "formatBotStrategyReadiness(status)" in bot_fleet
        assert "strategy-readiness-chip" in bot_fleet
        assert "strategy-readiness-detail" in bot_fleet
        assert "Strategy Readiness" in bot_fleet
        assert ".strategy-readiness-chip[data-readiness-state=\"blocked\"]" in css
        assert ".strategy-readiness-action" in css
        assert "readiness_next_action" in bot_fleet
        assert "can_paper_trade" in bot_fleet
        assert "launch_lane" in bot_fleet
        assert "mobile-card-table" in bot_fleet
        assert "mobile-chart-shell" in bot_fleet
        assert "data-quality" in bot_fleet
        assert "server_ts" in bot_fleet
        assert "source_age_s" in bot_fleet
        assert "source_updated_at" in bot_fleet
        assert "dashboard_version" in bot_fleet
        assert "release_stage" in bot_fleet
        assert "ensureLiveBotSelection" in bot_fleet
        assert "selectBot(firstLiveBot.name" in bot_fleet
        assert "/api/fleet-equity" in bot_fleet
        assert "/api/equity?" not in bot_fleet
        assert "document.hidden" in panels
        assert "cache: 'no-store'" in auth

    def test_status_page_uses_position_exposure_close_evidence(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "liveBroker?.position_exposure" in html
        assert "selected-window closed outcomes" in html
        assert "renderRecentCloses" in html
        assert "target_exit_visibility" in html
        assert "broker_open_position_count" in html
        assert "supervisor_local_position_count" in html
        assert "paper-local watched" in html

    def test_status_page_does_not_coerce_missing_broker_pnl_to_zero(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "function formatMoneyOrUnavailable" in html
        assert "brokerHasPnlTruth" in html
        assert "broker PnL fields unavailable" in html
        assert "formatMoney(totalRealized ?? 0)" not in html
        assert "formatMoney(totalUnrealized ?? 0)" not in html
        assert "Number(liveBroker.today_realized_pnl || 0)" not in html
        assert "Number(liveBroker.total_unrealized_pnl || 0)" not in html

    def test_status_page_uses_explicit_bracket_blocker_truth(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "summary.broker_bracket_prop_dry_run_blocked" in html
        assert "const propDryRunBlocked" in html
        assert "propDryRunBlocked ? 'prop dry-run blocked' : ''" in html
        assert "actionChoices.length && propDryRunBlocked ? '' : auditAction" in html
        assert "bracketSummary === 'BLOCKED_UNBRACKETED_EXPOSURE' ? 'prop dry-run blocked' : ''" not in html

    def test_status_page_marks_paper_live_held_by_bracket_audit(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "const paperGateHeld = propDryRunBlocked && paperReady" in html
        assert "paperLabel = 'Held'" in html
        assert "paperClass = 'yellow'" in html
        assert "held by Bracket Audit" in html
        assert "paperGateHeld ? paperBracketHold : firstPaperGate?.detail" in html

    def test_status_page_surfaces_vps_root_reconciliation_card(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "VPS Root Review" in html
        assert "vpsRootStatus" in html
        assert "vpsRootSub" in html
        assert "vpsRootDetailsWrap" in html
        assert "vpsRootSteps" in html
        assert "/api/vps/root-reconciliation" in html
        assert "renderVpsRootReview" in html
        assert "renderVpsRootDetails" in html
        assert "focusVpsRootDetails" in html
        assert "scrollIntoView" in html
        assert "source_or_governance_deleted" in html
        assert "generated_untracked" in html
        assert "steps.length" in html
        assert "cleanupAllowed" in html
        assert "'locked'" in html

    def test_status_page_card_health_contract_is_wired(self):
        """Card-health JS+CSS contract still intact. The index.html-side
        ``id="top-card-health"`` anchor was dropped in 6a7b9af; the
        supercharge.js + theme.css contracts here are the forward-
        compatible spec.
        """
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        css = (root / "theme.css").read_text(encoding="utf-8")
        supercharge = (root / "js" / "supercharge.js").read_text(encoding="utf-8")

        assert "card-health-chip" in css
        assert "initCardHealthContract" in supercharge
        assert "/api/dashboard/card-health" in supercharge
        assert "dead_cards" in supercharge
        assert "stale_cards" in supercharge
        assert "LIVE_CARD_WATCHDOG_GRACE_MS" in supercharge
        assert "eta-card-health" in supercharge
        assert "never_refreshed" in supercharge
        assert "panel_error" in supercharge
        assert "refresh_age_exceeded" in supercharge
        assert "card-health-inspector" in supercharge
        assert "Card Health Inspector" in supercharge
        assert "toggleCardHealthInspector" in supercharge
        assert "focusCardHealthPanel" in supercharge
        assert "data-focus-card" in supercharge
        assert "card-health-focus" in supercharge
        assert "card-health-dead" in supercharge
        assert "card-health-stale" in supercharge
        assert "retryUnhealthyCards" in supercharge
        assert "data-retry-card-health" in supercharge
        assert "eta-card-retry" in supercharge
        assert "Retry unhealthy" in supercharge
        assert ".card-health-inspector" in css
        assert ".card-health-retry" in css
        assert ".panel.card-health-focus" in css
        assert ".panel.card-health-dead" in css
        assert ".panel.card-health-stale" in css

    def test_status_page_diagnostics_contract_is_wired(self):
        """Diagnostics JS+CSS contract still intact. The index.html-side
        anchors (``id="top-diagnostics"`` and the
        ``data-diagnostics-endpoint`` attribute) were dropped in 6a7b9af.
        """
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        css = (root / "theme.css").read_text(encoding="utf-8")
        supercharge = (root / "js" / "supercharge.js").read_text(encoding="utf-8")

        assert "diagnostics-chip" in css
        assert ".diagnostics-inspector" in css
        assert "initCommandCenterDiagnostics" in supercharge
        assert "/api/dashboard/diagnostics" in supercharge
        assert "Command Center Diagnostics" in supercharge
        assert "diagnostics: live" in supercharge
        assert "api_build" in supercharge
        assert "bot_fleet" in supercharge
        assert "equity" in supercharge
        assert "/api/fleet-equity" in supercharge
        assert "eta-command-center-diagnostics" in supercharge

    def test_card_health_registry_is_populated(self):
        """The DASHBOARD_CARD_REGISTRY must contain at least the core
        operator-visible cards. The original test cross-checked this
        registry against ``data-panel-id="..."`` attributes in index.html,
        but those attributes were dropped in the 6a7b9af clean-HTML
        rewrite — the inline dashboard renders cards by HTML id only,
        not by panel-id metadata. This smoke is the residual contract:
        accidentally emptying the registry (e.g. via a refactor that
        moves card definitions elsewhere) would still fail loudly here.
        """
        from eta_engine.deploy.scripts.dashboard_api import DASHBOARD_CARD_REGISTRY

        registered = {str(card["id"]) for card in DASHBOARD_CARD_REGISTRY}
        assert registered, "DASHBOARD_CARD_REGISTRY is empty"
        # IDs must be unique
        assert len(registered) == len(DASHBOARD_CARD_REGISTRY)
        # Every entry must have an id field
        for card in DASHBOARD_CARD_REGISTRY:
            assert card.get("id"), f"registry entry missing id: {card}"

    def test_status_page_has_no_visible_mojibake_tokens(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        files = [
            root / "index.html",
            root / "theme.css",
            root / "js" / "panels.js",
            root / "js" / "command_center.js",
            root / "js" / "bot_fleet.js",
            root / "js" / "auth.js",
            root / "js" / "live.js",
        ]
        bad_tokens = ("â", "Â", "Ï", "�")
        for file in files:
            text = file.read_text(encoding="utf-8")
            for token in bad_tokens:
                assert token not in text, f"{file.name} contains mojibake token {token!r}"

    def test_theme_css_exists(self):
        path = Path(__file__).resolve().parent.parent / "deploy" / "status_page" / "theme.css"
        assert path.exists()
        css = path.read_text(encoding="utf-8")
        # Must define core panel + dark-mode tokens
        assert "--panel-bg" in css
        assert ".panel" in css
        assert ".panel.loading" in css
        assert ".panel.error" in css
        assert ".panel.stale" in css
        assert ".sse-connected" in css
        assert ".toast" in css
        # Batches 8-10: safe-area, touch targets, and readable phone density.
        assert "env(safe-area-inset-top)" in css
        assert "min-height: 44px" in css
        assert "overflow-wrap: anywhere" in css
        assert "prefers-reduced-motion" in css


# ---------------------------------------------------------------------------
# Supercharge tasks (round 2)
# ---------------------------------------------------------------------------


class TestSuperchargeTasks:
    """All 6 new supercharge tasks must be registered + have handlers."""

    def test_all_new_tasks_registered(self):
        from eta_engine.brain.avengers import (
            TASK_CADENCE,
            TASK_OWNERS,
            BackgroundTask,
        )
        from eta_engine.deploy.scripts.run_task import HANDLERS

        new_tasks = (
            BackgroundTask.HEALTH_WATCHDOG,
            BackgroundTask.SELF_TEST,
            BackgroundTask.LOG_ROTATE,
            BackgroundTask.DISK_CLEANUP,
            BackgroundTask.BACKUP,
            BackgroundTask.PROMETHEUS_EXPORT,
        )
        for task in new_tasks:
            assert task in TASK_OWNERS, f"{task.value} missing from TASK_OWNERS"
            assert task in TASK_CADENCE, f"{task.value} missing from TASK_CADENCE"
            assert task in HANDLERS, f"{task.value} missing from HANDLERS"

    def test_log_rotate_handler_writes_report(self, tmp_path):
        """LOG_ROTATE should run without error even on empty log dir."""
        state = tmp_path / "state"
        state.mkdir()
        logdir = tmp_path / "logs"
        logdir.mkdir()
        # Create a fresh .log file (too new to archive)
        (logdir / "active.log").write_text("hello\n")
        from eta_engine.deploy.scripts.run_task import _task_log_rotate

        out = _task_log_rotate(state, logdir)
        assert "archived" in out
        assert (state / "log_rotate.json").exists()

    def test_backup_handler_creates_archive(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        (state / "foo.json").write_text("{}")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".env").write_text("DUMMY=1")
        monkeypatch.setenv("ETA_REPO_DIR", str(repo))
        from eta_engine.deploy.scripts.run_task import _task_backup

        out = _task_backup(state)
        assert "archive" in out
        assert out["size_bytes"] > 0
        backups = list((state / "backups").glob("apex-backup-*.tar.gz"))
        assert len(backups) == 1

    def test_prometheus_export_handler_writes_metrics(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        # Seed minimal heartbeat
        hb = {
            "ts": "2026-04-24T00:00:00+00:00",
            "quota_state": "OK",
            "hourly_pct": 0.05,
            "daily_pct": 0.12,
            "cache_hit_rate": 0.88,
            "distiller_version": 3,
            "distiller_trained": True,
        }
        (state / "avengers_heartbeat.json").write_text(json.dumps(hb))
        from eta_engine.deploy.scripts.run_task import _task_prometheus_export

        out = _task_prometheus_export(state)
        prom_file = state / "prometheus" / "avengers.prom"
        assert prom_file.exists()
        text = prom_file.read_text(encoding="utf-8")
        assert "eta_up 1" in text
        assert "eta_quota_hourly_pct 0.05" in text
        assert "eta_cache_hit_rate 0.88" in text
        assert out["metrics"] > 0

    def test_self_test_report_written(self, tmp_path, monkeypatch):
        """SELF_TEST writes a structured report even when probes fail."""
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from eta_engine.deploy.scripts.run_task import _task_self_test

        out = _task_self_test(state)
        assert "overall" in out
        assert (state / "self_test.json").exists()

    def test_health_watchdog_non_windows_skip(self, tmp_path, monkeypatch):
        """On non-Windows, watchdog reports skipped without error."""
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("os.name", "posix", raising=False)
        from eta_engine.deploy.scripts.run_task import _task_health_watchdog

        out = _task_health_watchdog(state)
        assert out.get("skipped") is True

    def test_disk_cleanup_runs_without_error(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setenv("ETA_REPO_DIR", str(tmp_path / "nonexistent"))
        from eta_engine.deploy.scripts.run_task import _task_disk_cleanup

        out = _task_disk_cleanup(state)
        assert "bytes_freed" in out
        assert "files_deleted" in out


class TestPrometheusEndpoint:
    """Dashboard API should expose /metrics."""

    def test_metrics_endpoint_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
        import importlib

        import eta_engine.deploy.scripts.dashboard_api as mod

        importlib.reload(mod)
        from fastapi.testclient import TestClient

        client = TestClient(mod.app)

        # Empty -- no metrics file yet
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "eta_up" in r.text

        # Seed metrics file (use the same metric name as PROMETHEUS_EXPORT
        # produces — see run_task.py::_task_prometheus_export which
        # canonically writes ``eta_up 1``).
        prom_dir = tmp_path / "prometheus"
        prom_dir.mkdir()
        (prom_dir / "avengers.prom").write_text(
            "# HELP eta_up daemon alive\n# TYPE eta_up gauge\neta_up 1\n",
        )
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "eta_up 1" in r.text
        assert "text/plain" in r.headers["content-type"]
