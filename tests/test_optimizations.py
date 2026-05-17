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
    def test_skips_retired_prompt_cache_lane(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ETA_ENABLE_LEGACY_PROMPT_WARMUP", raising=False)
        from eta_engine.deploy.scripts.run_task import _task_prompt_warmup

        out = _task_prompt_warmup(tmp_path)
        assert out.get("skipped") is True
        assert "retired_codex_deepseek_policy" in out.get("reason", "")


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
            'id="totalTradesLabel"',
            'id="winRate"',
            'id="cumulativeR"',
            'id="cumulativeRLabel"',
            'id="cumulativeRSub"',
            'id="dataIntelligenceStatus"',
            'id="dataIntelligenceSub"',
            'id="retuneFactoryStatus"',
            'id="retuneFactorySub"',
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
        assert "symbol_intelligence" in html
        assert "diamond_retune_status" in html
        assert "renderRetuneFactory" in html
        assert "Retune Factory" in html
        assert "need broker proof" in html
        assert "/api/jarvis/diamond_retune_status" in html
        assert "focus_active_experiment" in html
        assert "focus_active_experiment_outcome_line" in html
        assert "post_change_closed_trade_count" in html
        assert "awaiting first post-change close" in html
        assert "PnL ${formatCurrency(postChangePnl)}" in html
        assert "PF ${postChangeProfitFactor.toFixed(2)}" in html
        assert 'data-panel-id="cc-strategy-supercharge-results"' not in html
        assert 'id="strategyNearMisses"' not in html
        assert 'id="strategyRetuneQueue"' not in html
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
        assert "Admin AI" in html
        assert "adminAiStatus" in html
        assert "vps_ops_hardening" in html
        assert "force_multiplier_control_plane" in html
        assert "fmNeedsAttention" in html
        assert "FM control plane" in html
        assert "service_runtime_drift" in html
        assert "service_config_drift" in html
        assert "runtime drift:" in html
        assert "config drift:" in html
        assert "endpointCandidates(" in html
        assert "cache: 'no-store'" in html
        assert "FETCH_TIMEOUT_MS" in html
        assert "AUX_FETCH_TIMEOUT_MS" in html
        assert "DASHBOARD_DIAGNOSTICS_TIMEOUT_MS" in html
        assert "VPS_ROOT_RECONCILIATION_TIMEOUT_MS" in html
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
        assert "function readinessSnapshotPerformanceEvidence" in html
        assert "broker bracket gap" in html
        assert "Snapshot Stale" in html
        assert "function closeEvidenceSummary" in html
        assert "function closeHistoryWindowPayload" in html
        assert "rootPayload?.close_history" in html
        assert "closeHistoryWindowPayload(liveBroker, selectedCloseHistoryWindow, d)" in html
        assert "function renderPnlWindowControls" in html
        assert 'data-close-window="today"' in html
        assert 'data-close-window="mtd"' in html
        assert 'data-close-window="all"' in html
        assert "MTD closed outcomes" in html
        assert "selected-window Close History" in html
        assert "const historyTitle = `${scope} Close History`" in html
        assert "selected-window close history" in html
        assert "broker position rows unavailable" in html
        assert "broker-reported open futures exposure without row detail" in html
        assert 'id="pnlWindowStrip"' in html
        assert "function renderPnlWindowStrip" in html
        assert "selected-window realized PnL" in html
        assert "Closed PnL" in html
        assert "function winRateSourceText" in html
        assert "close-ledger outcomes" in html
        assert "function pnlHistoryEvidence" in html
        assert "Closed PnL" in html
        assert "lifetime ledger pending" in html
        assert "readiness ledger:" in html
        assert "closed outcome" in html
        assert "closed outcomes in ledger" in html
        assert "selected-window closed outcomes" in html
        assert '<details class="collapsible-section ops-priority-section" id="opsTruthSection" open>' in html
        assert "source === 'supervisor_heartbeat'" in html
        assert "source === 'fills_intraday'" in html
        assert "total_pnl_is_lifetime" in html
        assert "readinessPerformance.winRatePct" in html
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
        assert 'id="allocationOrbRing"' in html
        assert 'id="allocationSectionWrap" hidden' in html
        assert "function visibleAllocationBuckets" in html
        assert "function allocationRingGradient" in html
        assert "edge-chip-row" in html
        assert "allocation-doctrine" in html
        assert "waiting for selected-window realized-R outcomes" in html
        assert "dollar-like R value" in html
        assert "function plausibleRealizedR" in html
        assert "MAX_REASONABLE_R_MULTIPLE" in html
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

    def test_command_center_keeps_supercharge_modules_out_of_static_dashboard(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "js" / "command_center.js").read_text(encoding="utf-8")
        panels = (root / "js" / "panels.js").read_text(encoding="utf-8")

        # Direct research modules remain available for explicit operator use.
        assert "StrategySuperchargeManifestPanel" in js
        assert "/api/jarvis/strategy_supercharge_manifest" in js
        assert "next_batch" in js
        assert "A+C now" in js
        assert "StrategySuperchargeResultsPanel" in js
        assert "cc-strategy-supercharge-results" in js
        assert "/api/jarvis/strategy_supercharge_results" in js
        assert "near_misses" in js
        assert "retune_queue" in js
        assert "commandCenterContractAnchors" in html
        assert "cc-strategy-supercharge-results" not in html
        assert 'id="strategyNearMisses"' not in html
        assert 'id="strategyRetuneQueue"' not in html
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
        assert 'data-label="Bot"' in bot_fleet
        assert 'data-label="Day PnL"' in bot_fleet
        assert 'data-label="Last Trade"' in bot_fleet
        assert 'data-label="Readiness"' in bot_fleet
        assert "formatBotStrategyReadiness" in bot_fleet
        assert "formatBotStrategyReadiness(status)" in bot_fleet
        assert "strategy-readiness-chip" in bot_fleet
        assert "strategy-readiness-detail" in bot_fleet
        assert "Strategy Readiness" in bot_fleet
        assert '.strategy-readiness-chip[data-readiness-state="blocked"]' in css
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

    def test_status_page_mobile_background_and_atlanta_time_contract(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "All times Atlanta / ET" in html
        assert "const DASHBOARD_TIME_ZONE = 'America/New_York'" in html
        assert "const DASHBOARD_TIME_ZONE_LABEL = 'Atlanta ET'" in html
        assert "function parseDashboardDate" in html
        assert "formatActivityTime(lastTrade.last_trade_ts)" in html
        assert "formatActivityTime(lastSignal.last_signal_ts)" in html
        assert "formatActivityTime(activity.ts)" in html
        assert ".replace('T',' ').substring(0,19)" not in html

        # Phone view gets a centered, viewport-sized background so the right
        # glow/grid does not crop against the edge of narrow devices.
        assert "@media(max-width:640px){body{background:" in html
        assert "background-size:100vw 100dvh,100vw 100dvh,auto" in html
        assert "body:before,body:after{left:0;right:0;width:100vw;max-width:100vw" in html
        assert "body:after{inset:0;background-size:38px 38px,38px 38px,auto" in html

    def test_status_page_pnl_map_uses_daily_strategy_winner_loser_lanes(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "Daily strategy winners/losers" in html
        assert "function closeHistoryContributionRows" in html
        assert "type: 'daily_close_realized'" in html
        assert "aggregateContributionRows(pnlMapContributionRows" in html
        assert "function distinctImpactRows" in html
        assert "function pickWinnerLoserRows" in html
        assert "Daily Winners" in html
        assert "Daily Losers" in html
        assert "top 5 winners and top 5 losers" in html
        assert "renderContributionGraph(bots, liveBroker, portfolioSummary, todayCloseHistory)" in html
        assert "Daily close history pending | showing" in html
        assert "Top 5 distinct PnL movers" in html

    def test_status_page_bottom_half_uses_finished_operator_language(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "Live Book + Exits" in html
        assert "Open risk, closed PnL, and order flow" in html
        assert "Live Book &amp; Exit Cover" in html
        assert "Router Flow" in html
        assert "Trade Quality" in html
        assert "R score" in html
        assert "ops-priority-section .card" in html
        assert "mobile-card-table" in html
        assert "dashboard_task_contract_drift: 'Reload'" in html
        assert "upstream_failure: 'Proxy 5xx'" in html
        assert "local_dependency_gap: 'Deps'" in html
        assert "public_tunnel_token_rejected: 'Tunnel'" in html
        assert "repair_prompted: 'Repair'" in html
        assert "commandWatchdog.next_command" in html
        assert "commandWatchdog.display_issue_summary ||" in html
        assert "commandWatchdog.display_summary ||" in html
        assert "commandWatchdog.issue_summary ||" in html
        assert "commandWatchdog.summary ||" in html
        assert "dashboard_task_missing_task_names" in html
        assert "const commandWatchdogMissingTasksAlreadyNamed =" in html
        assert "!commandWatchdogMissingTasksAlreadyNamed" in html
        assert "missing runtime tasks:" in html
        assert "commandWatchdogCommand," in html
        assert "function readinessCommandCenterHint" in html
        assert "command_center_dashboard_task_missing_task_names" in html
        assert "ops reload required (${missingTasks.join(', ')})" in html
        assert "ops reload required" in html
        assert "local deps repair required" in html
        assert "local proxy 5xx" in html
        assert "function readinessSnapshotStrategyHint" in html
        assert "BLOCKED_STALE_FLAT_OPEN_ORDERS" in html
        assert "public_live_broker_ready" in html
        assert "public_live_broker_snapshot_state" in html
        assert "public_live_broker_snapshot_source" in html
        assert "public_live_broker_source" in html
        assert "public_live_broker_degraded_display" in html
        assert "dashboard_api_runtime_drift_display" in html
        assert "dashboard_api_runtime_retune_drift_display" in html
        assert "dashboard_api_runtime_probe_display" in html
        assert "dashboard_api_runtime_refresh_command" in html
        assert "dashboard_api_runtime_refresh_requires_elevation" in html
        assert "public_fallback_broker_open_order_drift_display" in html
        assert "primary_blocker" in html
        assert "const primaryBlocker = String(snapshot?.primary_blocker || '').trim();" in html
        assert "const primaryAction = String(snapshot?.primary_action || snapshot?.detail || '').trim();" in html
        assert "const primaryBlockedHint = primaryBlocker === 'prop_live_readiness_gate'" in html
        assert "brackets_summary" in html
        assert "brackets_next_action" in html
        assert "current_live_broker_degraded_display" in html
        assert "current_live_broker_open_order_drift_display" in html
        assert "public_fallback_stale_flat_open_order_count" in html
        assert "public_fallback_stale_flat_open_order_symbols" in html
        assert "public_fallback_stale_flat_open_order_display" in html
        assert "public_fallback_stale_flat_open_order_relation_display" in html
        assert "retune_focus_active_experiment_drift_display" in html
        assert "publicLiveBrokerDegradedDisplay" in html
        assert "dashboardApiRuntimeDriftDisplay" in html
        assert "dashboardApiRuntimeRetuneDriftDisplay" in html
        assert "dashboardApiRuntimeProbeDisplay" in html
        assert "dashboardApiRuntimeRefreshCommand" in html
        assert "dashboardApiRuntimeRefreshRequiresElevation" in html
        assert "dashboardApiRuntimeBrokerHint" in html
        assert "dashboardApiRuntimeRetuneHint" in html
        assert "dashboardApiRuntimeProbeHint" in html
        assert "readinessReceiptHint" in html
        assert "bracketsPrimaryHint" in html
        assert "currentLiveBrokerDegradedDisplay" in html
        assert "currentLiveBrokerDegradedHint" in html
        assert "fallbackBrokerOrderDriftDisplay" in html
        assert "currentLiveBrokerOrderDriftDisplay" in html
        assert "prop gate blocked; keep paper soak" in html
        assert "add(primaryBlockedHint);" in html
        assert "fleet truth unavailable; restore /api/bot-fleet" in html
        assert "live broker cache missing" in html
        assert "add(readinessReceiptHint);" in html
        assert "add(bracketsPrimaryHint || snapshot?.public_fallback_reason || '');" in html
        assert (
            "add(currentLiveBrokerDegradedHint || currentLiveBrokerDegradedDisplay || "
            "publicLiveBrokerCacheHint || publicLiveBrokerDegradedDisplay);"
        ) in html
        assert "refresh local 8421 runtime" in html
        assert "8421 refresh needs elevation" in html
        assert "add(dashboardApiRuntimeBrokerHint);" in html
        assert "add(dashboardApiRuntimeRetuneHint);" in html
        assert "add(dashboardApiRuntimeProbeHint);" in html
        assert "local 8421 broker stale" in html
        assert "local 8421 retune stale" in html
        assert "local 8421 probe failed" in html
        assert "retuneExperimentDriftDisplay" in html
        assert "const publicLiveBrokerCacheHint = !publicLiveBrokerReady" in html
        assert "broker cache ${publicLiveBrokerSnapshotState || 'degraded'}" in html
        assert "via ${publicLiveBrokerSnapshotSource}" in html
        assert "public_live_retune_focus_active_experiment_outcome_line" in html
        assert "current_live_retune_focus_active_experiment_outcome_line" in html
        assert "local_retune_focus_active_experiment_outcome_line" in html
        assert "public_live_retune_generated_at_utc" in html
        assert "current_live_retune_generated_at_utc" in html
        assert "public_live_retune_sync_drift_display" in html
        assert "current_live_retune_sync_drift_display" in html
        assert "dashboard_api_runtime_retune_drift_display" in html
        assert "local_retune_generated_at_utc" in html
        assert "current_local_retune_generated_at_utc" in html
        assert "local_retune_sync_drift_display" in html
        assert "currentPublicRetuneGeneratedAt" in html
        assert "currentPublicRetuneExperimentOutcomeLine" in html
        assert "currentPublicRetuneSyncDriftDisplay" in html
        assert "current public retune newer" in html
        assert (
            "add(currentLiveRetuneSyncHint || currentLiveRetuneSyncDriftDisplay || "
            "publicRetuneSyncDriftDisplay);"
        ) in html
        assert "publicRetuneSyncDriftDisplay" in html
        assert "dashboardApiRuntimeRetuneDriftDisplay" in html
        assert "const readinessReceiptAgeSeconds = finiteNumber(readinessSnapshot.age_s);" in html
        assert (
            "const readinessReceiptStale = readinessSnapshot?.status === "
            "'stale_receipt' || readinessSnapshot?.fresh === false;"
        ) in html
        assert "const readinessReceiptFreshnessHint = readinessReceiptAgeSeconds !== null" in html
        assert "receipt stale" in html
        assert "receipt fresh" in html
        assert "const retuneMirrorDriftHint = readinessReceiptStale" in html
        assert "const retuneMirrorTimingHint = (" in html
        assert "publicRetuneSyncDriftDisplay" in html
        assert "localRetuneSyncDriftDisplay" in html
        assert "|| dashboardApiRuntimeRetuneDriftDisplay" in html
        assert "|| dashboardApiRuntimeProbeDisplay" in html
        assert "public ${formatTimeCompact(publicRetuneGeneratedAt)}" in html
        assert "cached local ${formatTimeCompact(cachedLocalRetuneGeneratedAt)}" in html
        assert "current local ${formatTimeCompact(currentLocalRetuneGeneratedAt)}" in html
        assert "local retune mirror stale" in html
        assert "const retuneRuntimeDriftHint = dashboardApiRuntimeRetuneDriftDisplay" in html
        assert "const retuneRuntimeProbeHint = dashboardApiRuntimeProbeDisplay" in html
        assert (
            "const retuneRuntimeRefreshElevationHint = "
            "dashboardApiRuntimeRefreshRequiresElevation &&"
        ) in html
        assert "readiness receipt stale" in html
        assert "fallbackStaleRelationDisplay" in html
        assert "stale broker orders (" in html
        assert "fallbackStaleCount} stale broker orders (" in html
        assert "promotion retired" in html
        assert "authoritative gateway required" in html
        assert "live fleet truth fallback" in html
        assert "parts.slice(0, 3).join(' | ')" in html
        assert "function retuneFactoryReadout(snapshot, diagnostics = {})" in html
        assert "function renderRetuneFactory(snapshot, diagnostics = {})" in html
        assert (
            "renderRetuneFactory(d.diamond_retune_status || diagnostics?.diamond_retune_status || {}, diagnostics);"
            in html
        )
        assert "const readinessBlockedParts = [" in html
        assert (
            "readinessSnapshot.detail || readinessSnapshot.primary_action || "
            "readinessSnapshot.promotion_summary || 'gates blocked'"
        ) in html
        assert "readinessBlockedParts.join(' | ')" in html

    def test_status_page_surfaces_broker_truth_freshness(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "function brokerTruthStateLabel" in html
        assert "function brokerTruthChipMarkup" in html
        assert "broker-truth-chip" in html
        assert "broker_snapshot_state" in html
        assert "fresh IBKR read" in html
        assert "cached IBKR" in html
        assert "last-good IBKR" in html
        assert "last probe issue" in html
        assert "brokerTruthChipMarkup(liveBroker)" in html

    def test_status_page_uses_position_exposure_close_evidence(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "liveBroker?.position_exposure" in html
        assert "selected-window closed outcomes" in html
        assert "renderRecentCloses" in html
        assert "target_exit_visibility" in html
        assert "broker_open_position_count" in html
        assert "supervisor_local_position_count" in html
        assert "paper-local open" in html

    def test_status_page_prefers_fresh_public_fleet_and_cellars_spot(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "fetchBestFleetData" in html
        assert "payloadFleetScore" in html
        assert "isCellarBot" in html
        assert "cellarCount" in html
        assert "Alpaca/spot paused" in html
        assert "Backburner: Spot Paused" in html
        assert "Focus Bots" in html

    def test_status_page_does_not_coerce_missing_broker_pnl_to_zero(self):
        root = Path(__file__).resolve().parent.parent / "deploy" / "status_page"
        html = (root / "index.html").read_text(encoding="utf-8")

        assert "function formatMoneyOrUnavailable" in html
        assert "brokerHasPnlTruth" in html
        # formatMoneyOrUnavailable returns 'n/a' instead of coercing to 0
        assert "broker PnL fields unavailable" in html
        assert "return n == null ? 'n/a'" in html
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
        assert "paperDailyLossHold" in html
        assert "? paperEffectiveDetail || 'Daily-loss soft stop is active" in html
        assert "? paperBracketHold" in html

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
        assert "dirty_companion_repos" in html
        assert "submodule_uninitialized" in html
        assert "Root clean" in html
        assert "Lab artifacts" in html
        assert "Dormant submodules" in html
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
        assert "diagnostics: ${confirmed}/${botTotal} live" in supercharge
        assert "| held ${blockedBots}" in supercharge
        assert "api_build" in supercharge
        assert "bot_fleet" in supercharge
        assert "equity" in supercharge
        assert "vps_ops" in supercharge
        assert "admin_ai" in supercharge
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

    def test_service_alive_probe_known_port_returns_status(self, monkeypatch):
        """Port-probe should return alive=True when the port accepts a connection."""
        from eta_engine.deploy.scripts import run_task

        class FakeSocket:
            def __enter__(self) -> FakeSocket:
                return self

            def __exit__(self, *_: object) -> bool:
                return False

        monkeypatch.setattr(
            "socket.create_connection",
            lambda *args, **kwargs: FakeSocket(),
        )
        alive, detail = run_task._service_alive_via_probe("ETA-IBGateway")
        assert alive is True
        assert "port_4002" in detail

    def test_service_alive_probe_returns_false_when_port_closed(self, monkeypatch):
        from eta_engine.deploy.scripts import run_task

        def boom(*_a, **_kw):
            raise OSError("refused")

        monkeypatch.setattr("socket.create_connection", boom)
        alive, detail = run_task._service_alive_via_probe("ETA-IBGateway")
        assert alive is False
        assert "closed" in detail

    def test_service_alive_probe_unknown_svc_returns_no_probe(self):
        from eta_engine.deploy.scripts import run_task

        alive, detail = run_task._service_alive_via_probe("ETA-NoSuchSvc")
        assert alive is False
        assert detail == "no_probe_configured"

    def test_service_alive_probe_never_raises(self, monkeypatch):
        """Even with totally broken probe, helper returns (False, reason) cleanly."""
        from eta_engine.deploy.scripts import run_task

        def boom(*_a, **_kw):
            raise RuntimeError("simulated crash")

        monkeypatch.setattr("socket.create_connection", boom)
        alive, _detail = run_task._service_alive_via_probe("ETA-Dashboard-API")
        assert alive is False  # Did not raise

    def test_health_watchdog_alert_dedupe_suppresses_within_window(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Once a service has triggered an alert, suppress repeats for 60min.

        Without this dedupe, a persistently-flapping service (like the
        ETA-IBGateway state issue) would page the operator every 5min.
        """
        import json
        from datetime import UTC, datetime, timedelta

        from eta_engine.deploy.scripts import run_task

        state = tmp_path / "state"
        state.mkdir()

        # Pre-seed the dedup file showing we alerted for ETA-IBGateway 10 min ago
        dedup_path = state / "health_watchdog_alert_dedup.json"
        ten_min_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        dedup_path.write_text(
            json.dumps({"ETA-IBGateway": ten_min_ago}),
            encoding="utf-8",
        )

        # Pre-seed history showing 5 restarts of ETA-IBGateway in last hour
        history_path = state / "health_watchdog_restart_history.jsonl"
        now_iso = datetime.now(UTC).isoformat()
        with history_path.open("w", encoding="utf-8") as fh:
            for _ in range(5):
                fh.write(json.dumps({"ts": now_iso, "svc": "ETA-IBGateway"}) + "\n")

        # Capture telegram send attempts
        sent: list[tuple[str, str]] = []

        def fake_send(text: str, priority: str = "INFO") -> dict:
            sent.append((text, priority))
            return {"ok": True}

        monkeypatch.setattr(
            "eta_engine.deploy.scripts.telegram_alerts.send_from_env",
            fake_send,
        )
        monkeypatch.setattr("os.name", "nt", raising=False)
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda *a, **kw: "Ready",
        )
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: None,
        )
        # Force the probe to say "not alive" so restart path is exercised
        monkeypatch.setattr(
            run_task,
            "_service_alive_via_probe",
            lambda svc: (False, "test_probe_dead"),
        )

        run_task._task_health_watchdog(state)
        # Should NOT have sent: we already alerted 10min ago, dedup_window=60min
        assert sent == [], (
            f"alert was sent despite dedupe (expected suppression): {sent}"
        )

    def test_health_watchdog_skips_restart_when_port_alive(
        self,
        tmp_path,
        monkeypatch,
    ):
        """REGRESSION: when the underlying port is listening, skip the false restart.

        This was the root cause of the every-5-min ETA-IBGateway spam: the
        scheduled-task state goes Ready while the gateway process keeps
        running. Without the port probe, watchdog 'restarts' it 12x per hour.
        """
        from eta_engine.deploy.scripts import run_task

        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("os.name", "nt", raising=False)
        # Task state reports "Ready" (not Running) for ETA-IBGateway
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda *a, **kw: "Ready",
        )
        restart_calls: list[str] = []

        def fake_run(args, **_kw):
            # Track if Start-ScheduledTask is ever invoked
            joined = " ".join(args) if isinstance(args, list) else str(args)
            if "Start-ScheduledTask" in joined:
                restart_calls.append(joined)
            return None

        monkeypatch.setattr("subprocess.run", fake_run)
        # Port 4002 IS alive: probe returns True
        monkeypatch.setattr(
            run_task,
            "_service_alive_via_probe",
            lambda svc: (svc == "ETA-IBGateway", "port_4002_listening"),
        )

        result = run_task._task_health_watchdog(state)
        # No restart attempt should have been made for ETA-IBGateway
        assert not any(
            "ETA-IBGateway" in c for c in restart_calls
        ), f"unexpected restart attempt: {restart_calls}"
        # The action record should show skipped_restart=True
        skipped = [a for a in result.get("restarted", []) if "IBGateway" in str(a)]
        assert skipped == [], "ETA-IBGateway should not be in restarted list"

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
