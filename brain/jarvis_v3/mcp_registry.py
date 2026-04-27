"""
JARVIS v3 // mcp_registry
=========================
Every MCP routed through JARVIS.

Similar pattern to ``skills_registry``, but for MCP servers. MCPs are
powerful: tradingview can buy/sell; computer-use can control the screen;
Desktop_Commander can read any file; blockscout can run on-chain queries.
Each tool gets classified and scoped.

Conventions:
  * MCP name -- the server's short name (e.g. ``tradingview``, ``slack``)
  * Tool     -- one tool inside that server (``chart_set_symbol``)
  * Risk tier is MAX across (server risk, tool risk)

A single JSON file (``jarvis_mcp_registry.json``) on disk is the
runtime source of truth. ``default_registry`` supplies sensible tiers
that mirror the operator doctrine.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class MCPRiskTier(StrEnum):
    READ = "READ"  # read-only, idempotent
    WRITE = "WRITE"  # writes data but not irreversible
    TRADE = "TRADE"  # can move money / place orders
    ADMIN = "ADMIN"  # can change system state (computer-use, files)


class MCPToolScope(BaseModel):
    """Allowlist entry for one tool inside one server."""

    model_config = ConfigDict(frozen=False)

    server: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    tier: MCPRiskTier
    allowed_subsystems: list[str] = Field(default_factory=lambda: ["operator.edward"])
    cool_down_s: float = Field(default=0.0, ge=0.0)
    # If true, approval requires explicit 'review_acknowledged' in payload.
    needs_operator_ack: bool = False
    rationale: str = ""


class MCPRegistry:
    """Catalog of MCP tools JARVIS is allowed to route."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], MCPToolScope] = {}

    def register(self, scope: MCPToolScope) -> None:
        self._by_key[(scope.server, scope.tool)] = scope

    def get(self, server: str, tool: str) -> MCPToolScope | None:
        return self._by_key.get((server, tool))

    def list(self, server: str | None = None) -> list[MCPToolScope]:
        if server is None:
            return list(self._by_key.values())
        return [v for (s, _t), v in self._by_key.items() if s == server]

    def can_use(
        self,
        server: str,
        tool: str,
        subsystem: str,
    ) -> tuple[bool, str]:
        sc = self._by_key.get((server, tool))
        if sc is None:
            return False, f"{server}::{tool} not registered"
        if not _matches_any(subsystem, sc.allowed_subsystems):
            return False, f"{subsystem} not in allowlist for {server}::{tool}"
        return True, f"{subsystem} approved for {server}::{tool} ({sc.tier.value})"

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        out = {
            "scopes": [v.model_dump() for v in self._by_key.values()],
        }
        Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> MCPRegistry:
        p = Path(path)
        if not p.exists():
            return default_registry()
        data = json.loads(p.read_text(encoding="utf-8"))
        reg = cls()
        for v in data.get("scopes", []):
            reg.register(MCPToolScope.model_validate(v))
        return reg


def _matches_any(subsystem: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p == "*":
            return True
        if p == subsystem:
            return True
        if p.endswith(".*"):
            prefix = p[:-2]
            if subsystem.startswith(prefix + ".") or subsystem == prefix:
                return True
    return False


def default_registry() -> MCPRegistry:
    """Default catalog -- covers the MCPs currently connected in the firm stack."""
    reg = MCPRegistry()

    # === TradingView (READ + TRADE split) ==================================
    # Read tools -- chart_get_*, data_get_*, pine_*, symbol_*, quote_get
    for t in (
        "chart_get_state",
        "chart_get_visible_range",
        "data_get_ohlcv",
        "data_get_equity",
        "data_get_indicator",
        "data_get_pine_boxes",
        "data_get_pine_labels",
        "data_get_pine_lines",
        "data_get_pine_tables",
        "data_get_strategy_results",
        "data_get_study_values",
        "data_get_trades",
        "quote_get",
        "symbol_info",
        "symbol_search",
        "pine_analyze",
        "pine_check",
        "pine_compile",
        "pine_get_console",
        "pine_get_errors",
        "pine_get_source",
        "pine_list_scripts",
        "layout_list",
        "tab_list",
        "tv_discover",
        "tv_health_check",
        "tv_ui_state",
        "watchlist_get",
        "capture_screenshot",
    ):
        reg.register(
            MCPToolScope(
                server="tradingview",
                tool=t,
                tier=MCPRiskTier.READ,
                allowed_subsystems=["operator.edward", "bot.mnq", "bot.nq", "watchdog.autopilot"],
                rationale="read-only chart / indicator / quote data",
            )
        )
    # Write / chart-mutate tools
    for t in (
        "chart_set_symbol",
        "chart_set_timeframe",
        "chart_set_type",
        "chart_manage_indicator",
        "chart_scroll_to_date",
        "chart_set_visible_range",
        "draw_shape",
        "draw_clear",
        "draw_remove_one",
        "draw_get_properties",
        "indicator_set_inputs",
        "indicator_toggle_visibility",
        "pine_new",
        "pine_open",
        "pine_save",
        "pine_set_source",
        "pine_smart_compile",
        "layout_switch",
        "pane_focus",
        "pane_set_layout",
        "pane_set_symbol",
        "replay_start",
        "replay_stop",
        "replay_step",
        "replay_autoplay",
        "replay_status",
        "tab_new",
        "tab_close",
        "tab_switch",
        "watchlist_add",
    ):
        reg.register(
            MCPToolScope(
                server="tradingview",
                tool=t,
                tier=MCPRiskTier.WRITE,
                allowed_subsystems=["operator.edward"],
                rationale="chart-mutating tool; operator-only",
            )
        )
    # TRADE tier (alert / replay trade simulation)
    for t in ("alert_create", "alert_delete", "alert_list", "replay_trade", "batch_run"):
        reg.register(
            MCPToolScope(
                server="tradingview",
                tool=t,
                tier=MCPRiskTier.TRADE,
                allowed_subsystems=["operator.edward"],
                needs_operator_ack=True,
                rationale="alert / replay trade -- operator ack required",
            )
        )

    # === Desktop Commander (ADMIN) ========================================
    for t in (
        "create_directory",
        "edit_block",
        "force_terminate",
        "get_config",
        "get_file_info",
        "get_more_search_results",
        "get_prompts",
        "get_recent_tool_calls",
        "get_usage_stats",
        "give_feedback_to_desktop_commander",
        "interact_with_process",
        "kill_process",
        "list_directory",
        "list_processes",
        "list_searches",
        "list_sessions",
        "move_file",
        "read_file",
        "read_multiple_files",
        "read_process_output",
        "set_config_value",
        "start_process",
        "start_search",
        "stop_search",
        "write_file",
        "write_pdf",
    ):
        reg.register(
            MCPToolScope(
                server="Desktop_Commander",
                tool=t,
                tier=MCPRiskTier.ADMIN,
                allowed_subsystems=["operator.edward"],
                needs_operator_ack=True,
                rationale="shell / filesystem access; admin tier",
            )
        )

    # === computer-use (ADMIN) =============================================
    for t in (
        "screenshot",
        "left_click",
        "right_click",
        "double_click",
        "triple_click",
        "mouse_move",
        "type",
        "key",
        "hold_key",
        "scroll",
        "cursor_position",
        "left_click_drag",
        "middle_click",
        "wait",
        "zoom",
        "open_application",
        "list_granted_applications",
        "read_clipboard",
        "write_clipboard",
        "computer_batch",
        "teach_batch",
        "teach_step",
        "left_mouse_down",
        "left_mouse_up",
        "request_access",
        "request_teach_access",
        "switch_display",
    ):
        reg.register(
            MCPToolScope(
                server="computer-use",
                tool=t,
                tier=MCPRiskTier.ADMIN,
                allowed_subsystems=["operator.edward"],
                needs_operator_ack=True,
                rationale="screen / keyboard / mouse control",
            )
        )

    # === Chrome / Claude_in_Chrome (WRITE + ADMIN mix) ====================
    for t in (
        "find",
        "form_input",
        "get_page_text",
        "navigate",
        "read_console_messages",
        "read_network_requests",
        "read_page",
        "switch_browser",
        "tabs_close_mcp",
        "tabs_context_mcp",
        "tabs_create_mcp",
        "resize_window",
        "computer",
    ):
        reg.register(
            MCPToolScope(
                server="Claude_in_Chrome",
                tool=t,
                tier=MCPRiskTier.WRITE,
                allowed_subsystems=["operator.edward"],
                rationale="browser automation",
            )
        )

    # === Slack (WRITE) ====================================================
    for t in (
        "slack_create_canvas",
        "slack_read_canvas",
        "slack_read_channel",
        "slack_read_thread",
        "slack_read_user_profile",
        "slack_schedule_message",
        "slack_search_channels",
        "slack_search_public",
        "slack_search_users",
        "slack_send_message",
        "slack_send_message_draft",
        "slack_update_canvas",
    ):
        reg.register(
            MCPToolScope(
                server="slack",
                tool=t,
                tier=MCPRiskTier.WRITE,
                allowed_subsystems=["operator.edward"],
                rationale="external comms",
            )
        )

    # === Blockscout (READ) ================================================
    for t in (
        "get_address_info",
        "get_block_info",
        "get_block_number",
        "get_chains_list",
        "get_contract_abi",
        "get_token_transfers_by_address",
        "get_tokens_by_address",
        "get_transaction_info",
        "get_transactions_by_address",
        "inspect_contract_code",
        "lookup_token_by_symbol",
        "nft_tokens_by_address",
        "read_contract",
        "direct_api_call",
        "get_address_by_ens_name",
    ):
        reg.register(
            MCPToolScope(
                server="blockscout",
                tool=t,
                tier=MCPRiskTier.READ,
                allowed_subsystems=[
                    "operator.edward",
                    "bot.btc_hybrid",
                    "bot.btc_perp",
                    "bot.eth_perp",
                    "bot.sol_perp",
                    "bot.yield_vault",
                ],
                rationale="on-chain read for crypto desks",
            )
        )

    # === Scheduled tasks (WRITE) ==========================================
    for t in (
        "create_scheduled_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
    ):
        reg.register(
            MCPToolScope(
                server="scheduled-tasks",
                tool=t,
                tier=MCPRiskTier.WRITE,
                allowed_subsystems=["operator.edward"],
                rationale="cron-style scheduling",
            )
        )

    return reg
