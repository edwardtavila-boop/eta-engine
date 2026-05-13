from __future__ import annotations

from types import SimpleNamespace

import pytest

from eta_engine.scripts.flatten_ibkr_positions import (
    _action_for_position,
    _patch_contract_exchange,
    _position_matches_filter,
)


def test_action_for_position_maps_long_and_short() -> None:
    assert _action_for_position(3) == "SELL"
    assert _action_for_position(-2) == "BUY"


def test_action_for_position_rejects_zero() -> None:
    with pytest.raises(ValueError):
        _action_for_position(0)


def test_patch_contract_exchange_fills_missing_futures_exchange() -> None:
    contract = SimpleNamespace(symbol="MNQ", secType="FUT", exchange="")

    _patch_contract_exchange(contract)

    assert contract.exchange == "CME"


def test_patch_contract_exchange_leaves_existing_exchange_intact() -> None:
    contract = SimpleNamespace(symbol="GC", secType="FUT", exchange="COMEX")

    _patch_contract_exchange(contract)

    assert contract.exchange == "COMEX"


# ────────────────────────────────────────────────────────────────────
# Selective flatten — per-symbol / per-localSymbol filter
# Added 2026-05-12 so the operator can flatten only paper-account
# residue (e.g. MNQM6 + MCLM6) without touching managed positions.
# ────────────────────────────────────────────────────────────────────


def test_filter_no_arguments_matches_every_position() -> None:
    """Default behaviour: empty filter → flatten everything."""
    contract = SimpleNamespace(symbol="MNQ", localSymbol="MNQM6")
    assert _position_matches_filter(contract, None, None) is True


def test_filter_symbol_match_root_contract() -> None:
    contract = SimpleNamespace(symbol="MNQ", localSymbol="MNQM6")
    assert _position_matches_filter(contract, {"MNQ"}, None) is True


def test_filter_symbol_excludes_non_match() -> None:
    contract = SimpleNamespace(symbol="MYM", localSymbol="MYMM6")
    assert _position_matches_filter(contract, {"MNQ"}, None) is False


def test_filter_local_symbol_match_front_month() -> None:
    """Allows surgical flatten of one expiry but not another."""
    contract = SimpleNamespace(symbol="MNQ", localSymbol="MNQM6")
    assert _position_matches_filter(contract, None, {"MNQM6"}) is True
    contract_next_month = SimpleNamespace(symbol="MNQ", localSymbol="MNQU6")
    assert _position_matches_filter(contract_next_month, None, {"MNQM6"}) is False


def test_filter_case_insensitive() -> None:
    """The CLI uppercases inputs; the function must work either way
    so direct callers (tests, scripts) aren't surprised."""
    contract = SimpleNamespace(symbol="MNQ", localSymbol="MNQM6")
    assert _position_matches_filter(contract, {"mnq"}, None) is True
    assert _position_matches_filter(contract, None, {"mnqm6"}) is True


def test_filter_symbol_or_local_either_matches() -> None:
    """Filters are a union — symbol OR localSymbol can satisfy."""
    contract = SimpleNamespace(symbol="MNQ", localSymbol="MNQM6")
    # Symbol set has MYM (no), local set has MNQM6 (yes) → match
    assert _position_matches_filter(contract, {"MYM"}, {"MNQM6"}) is True
    # Neither matches → false
    assert _position_matches_filter(contract, {"MYM"}, {"ESM6"}) is False


def test_filter_handles_missing_symbol_attribute() -> None:
    """A contract without symbol or localSymbol must not crash —
    just fails to match any filter (return False)."""
    contract = SimpleNamespace()
    assert _position_matches_filter(contract, {"MNQ"}, None) is False
    assert _position_matches_filter(contract, None, {"MNQM6"}) is False
    # But no-filter still returns True (flatten all)
    assert _position_matches_filter(contract, None, None) is True
