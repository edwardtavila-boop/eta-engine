from __future__ import annotations

from pathlib import Path

from eta_engine.brain.jarvis_v3.mcp_registry import (
    MCPRegistry,
    MCPRiskTier,
    MCPToolScope,
    default_registry,
)


def test_default_registry_splits_tradingview_read_write_and_trade_tiers() -> None:
    registry = default_registry()

    read_scope = registry.get("tradingview", "quote_get")
    write_scope = registry.get("tradingview", "chart_set_symbol")
    trade_scope = registry.get("tradingview", "alert_create")

    assert read_scope is not None
    assert read_scope.tier is MCPRiskTier.READ
    assert "bot.mnq" in read_scope.allowed_subsystems

    assert write_scope is not None
    assert write_scope.tier is MCPRiskTier.WRITE
    assert write_scope.allowed_subsystems == ["operator.edward"]

    assert trade_scope is not None
    assert trade_scope.tier is MCPRiskTier.TRADE
    assert trade_scope.needs_operator_ack is True


def test_default_registry_marks_filesystem_and_computer_control_as_admin_ack() -> None:
    registry = default_registry()

    read_file = registry.get("Desktop_Commander", "read_file")
    screenshot = registry.get("computer-use", "screenshot")

    assert read_file is not None
    assert read_file.tier is MCPRiskTier.ADMIN
    assert read_file.needs_operator_ack is True
    assert screenshot is not None
    assert screenshot.tier is MCPRiskTier.ADMIN
    assert screenshot.needs_operator_ack is True


def test_can_use_allows_exact_wildcard_and_prefix_scopes() -> None:
    registry = MCPRegistry()
    registry.register(
        MCPToolScope(
            server="research",
            tool="scan",
            tier=MCPRiskTier.READ,
            allowed_subsystems=["bot.*", "operator.edward"],
        )
    )

    assert registry.can_use("research", "scan", "bot.mnq")[0] is True
    assert registry.can_use("research", "scan", "bot")[0] is True
    assert registry.can_use("research", "scan", "operator.edward")[0] is True

    allowed, reason = registry.can_use("research", "scan", "watchdog.autopilot")
    assert allowed is False
    assert "not in allowlist" in reason


def test_can_use_reports_unregistered_tools_and_filters_by_server() -> None:
    registry = MCPRegistry()
    registry.register(
        MCPToolScope(
            server="alpha",
            tool="read",
            tier=MCPRiskTier.READ,
            allowed_subsystems=["*"],
        )
    )
    registry.register(
        MCPToolScope(
            server="beta",
            tool="write",
            tier=MCPRiskTier.WRITE,
            allowed_subsystems=["operator.edward"],
        )
    )

    allowed, reason = registry.can_use("alpha", "missing", "operator.edward")
    assert allowed is False
    assert "not registered" in reason
    assert [scope.tool for scope in registry.list("beta")] == ["write"]


def test_load_missing_registry_returns_safe_default_catalog() -> None:
    # Canonical workspace path: under var/eta_engine/state/ per CLAUDE.md
    # hard rule #1. The path must not exist for this test (load() returns
    # the safe default catalog when the file is missing).
    registry = MCPRegistry.load(
        Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/definitely_missing_mcp_registry_for_test.json")
    )

    assert registry.get("tradingview", "quote_get") is not None
    allowed, reason = registry.can_use("tradingview", "quote_get", "bot.mnq")
    assert allowed is True
    assert "approved" in reason

    # Legacy in-repo path also returns the safe default when missing —
    # verifies that the read-fallback location continues to behave
    # correctly during the migration window.
    legacy_registry = MCPRegistry.load(
        Path(
            "C:/EvolutionaryTradingAlgo/eta_engine/state/"  # HISTORICAL-PATH-OK
            "definitely_missing_mcp_registry_for_test.json"  # HISTORICAL-PATH-OK
        )
    )
    assert legacy_registry.get("tradingview", "quote_get") is not None
