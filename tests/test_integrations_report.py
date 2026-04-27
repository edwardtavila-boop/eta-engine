"""Tests for ``funnel.integrations`` + ``scripts.build_integrations_report``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.funnel.integrations import (
    BotIntegration,
    FunnelLayer,
    IntegrationsReport,
    ObservabilityIntegration,
    OnrampRoute,
    StakingIntegration,
    VenueIntegration,
    build_integrations_report,
    canonical_bots,
    canonical_funnel_layers,
    canonical_observability,
    canonical_onramp_routes,
    canonical_staking,
    canonical_venues,
    render_text,
)
from eta_engine.scripts import build_integrations_report as cli

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Pydantic model invariants
# ---------------------------------------------------------------------------


def test_venue_integration_required_fields() -> None:
    v = VenueIntegration(
        name="x",
        kind="onramp",
        module="a.b.c",
        asset_classes=["BTC"],
    )
    assert v.status == "READY"
    assert v.notes == ""
    assert v.asset_classes == ["BTC"]


def test_bot_integration_default_status_is_paper() -> None:
    b = BotIntegration(
        name="mnq",
        module="a.b",
        venue="tradovate",
        funnel_layer="LAYER_1_MNQ",
        risk_tier="A",
    )
    assert b.status == "PAPER"


def test_funnel_layer_rejects_out_of_range_sweep() -> None:
    with pytest.raises(ValueError):
        FunnelLayer(
            layer_id="L",
            label="x",
            sweep_out_pct=1.1,
            min_outgoing_usd=100.0,
            max_position_pct_per_trade=0.01,
            daily_loss_cap_pct=0.03,
            drawdown_kill_pct=0.08,
            leverage_cap=5.0,
        )


def test_funnel_layer_rejects_nonpositive_leverage() -> None:
    with pytest.raises(ValueError):
        FunnelLayer(
            layer_id="L",
            label="x",
            sweep_out_pct=0.5,
            min_outgoing_usd=100.0,
            max_position_pct_per_trade=0.01,
            daily_loss_cap_pct=0.03,
            drawdown_kill_pct=0.08,
            leverage_cap=0.0,
        )


def test_onramp_route_rejects_zero_per_txn() -> None:
    with pytest.raises(ValueError):
        OnrampRoute(
            fiat_source="ACH",
            provider="COINBASE",
            crypto_target="BTC",
            per_txn_limit_usd=0.0,
            monthly_limit_usd=50_000.0,
        )


def test_staking_integration_accepts_zero_apy() -> None:
    s = StakingIntegration(
        protocol="Stub",
        module="a.b",
        chain="ethereum",
        asset_in="USDT",
        asset_out="sUSDT",
        target_apy_pct=0.0,
    )
    assert s.target_apy_pct == 0.0


def test_observability_default_status() -> None:
    o = ObservabilityIntegration(
        name="StubAlerter",
        module="a.b",
        kind="alerter",
    )
    assert o.status == "ACTIVE"


def test_integrations_report_default_schema_version() -> None:
    r = IntegrationsReport(timestamp_utc="2026-04-17T00:00:00+00:00")
    assert r.schema_version == "1.0"
    assert r.venues == []
    assert r.summary == {}


# ---------------------------------------------------------------------------
# Canonical factories -- basic sanity + idempotence
# ---------------------------------------------------------------------------


def test_canonical_venues_list_is_nontrivial() -> None:
    vs = canonical_venues()
    assert len(vs) >= 6
    names = {v.name for v in vs}
    # Tradovate is DORMANT (2026-04-24 operator mandate) until funding
    # clears. Prior status was NEEDS_FUNDING; DORMANT is the superset
    # that also captures "operator has paused this venue."
    assert "tradovate" in names
    tradovate = next(v for v in vs if v.name == "tradovate")
    assert tradovate.status in {"NEEDS_FUNDING", "DORMANT"}


def test_canonical_bots_all_paper_by_default() -> None:
    bots = canonical_bots()
    assert len(bots) >= 6
    assert {b.status for b in bots} == {"PAPER"}


def test_canonical_bots_cover_all_funnel_layers_except_staking() -> None:
    bots = canonical_bots()
    layers = {b.funnel_layer for b in bots}
    # Staking is sink-only; no bot trades on it.
    assert "LAYER_1_MNQ" in layers
    assert "LAYER_2_BTC" in layers
    assert "LAYER_3_PERPS" in layers
    assert "LAYER_4_STAKING" not in layers


def test_canonical_funnel_layers_exactly_four() -> None:
    layers = canonical_funnel_layers()
    ids = [layer.layer_id for layer in layers]
    assert ids == [
        "LAYER_1_MNQ",
        "LAYER_2_BTC",
        "LAYER_3_PERPS",
        "LAYER_4_STAKING",
    ]


def test_canonical_funnel_layers_terminal_staking_has_no_sweep() -> None:
    layers = canonical_funnel_layers()
    terminal = next(layer for layer in layers if layer.layer_id == "LAYER_4_STAKING")
    assert terminal.sweep_out_pct == 0.0


def test_canonical_onramp_routes_default_limits() -> None:
    routes = canonical_onramp_routes()
    assert len(routes) == 3
    for r in routes:
        assert r.per_txn_limit_usd == 10_000.0
        assert r.monthly_limit_usd == 50_000.0


def test_canonical_onramp_routes_respect_overrides() -> None:
    routes = canonical_onramp_routes(
        per_txn_limit_usd=25_000.0,
        monthly_limit_usd=250_000.0,
    )
    assert all(r.per_txn_limit_usd == 25_000.0 for r in routes)
    assert all(r.monthly_limit_usd == 250_000.0 for r in routes)


def test_canonical_staking_covers_lido_jito_flare_ethena() -> None:
    protocols = {s.protocol for s in canonical_staking()}
    assert {"Lido", "Jito", "Flare", "Ethena"}.issubset(protocols)


def test_canonical_observability_contains_jarvis_supervisor() -> None:
    names = {o.name for o in canonical_observability()}
    assert "JarvisSupervisor" in names
    assert "AutopilotWatchdog" in names
    assert "DecisionJournal" in names


# ---------------------------------------------------------------------------
# build_integrations_report
# ---------------------------------------------------------------------------


def test_build_report_without_overlay_uses_canonical_defaults() -> None:
    r = build_integrations_report()
    assert r.schema_version == "1.0"
    assert len(r.venues) == len(canonical_venues())
    assert len(r.bots) == len(canonical_bots())
    assert r.summary["bots_paper"] == len(canonical_bots())
    assert r.summary["bots_live"] == 0
    assert r.summary["bots_blocked"] == 0


def test_build_report_applies_bot_overlay() -> None:
    overlay = {
        "bots": {
            "mnq": {"status": "LIVE", "notes": "tiny-size live"},
            "crypto_seed": {"status": "BLOCKED"},
        },
    }
    r = build_integrations_report(live_status=overlay)
    mnq = next(b for b in r.bots if b.name == "mnq")
    seed = next(b for b in r.bots if b.name == "crypto_seed")
    assert mnq.status == "LIVE"
    assert mnq.notes == "tiny-size live"
    assert seed.status == "BLOCKED"
    assert r.summary["bots_live"] == 1
    assert r.summary["bots_blocked"] == 1
    # All other bots still PAPER.
    assert r.summary["bots_paper"] == len(canonical_bots()) - 2


def test_build_report_applies_venue_overlay() -> None:
    overlay = {"venues": {"tradovate": {"status": "LIVE"}}}
    r = build_integrations_report(live_status=overlay)
    tradovate = next(v for v in r.venues if v.name == "tradovate")
    assert tradovate.status == "LIVE"


def test_build_report_applies_observability_overlay() -> None:
    overlay = {
        "observability": {
            "TelegramAlerter": {"status": "ACTIVE", "notes": "env wired"},
        },
    }
    r = build_integrations_report(live_status=overlay)
    tg = next(o for o in r.observability if o.name == "TelegramAlerter")
    assert tg.status == "ACTIVE"
    assert tg.notes == "env wired"
    assert r.summary["alerters_active"] == 1


def test_build_report_summary_extra_keys_merge_in() -> None:
    overlay = {"summary": {"extra_metric": 42}}
    r = build_integrations_report(live_status=overlay)
    assert r.summary["extra_metric"] == 42
    # Canonical keys must survive.
    assert "venues_total" in r.summary


def test_build_report_respects_onramp_limit_overrides() -> None:
    r = build_integrations_report(
        onramp_per_txn_limit_usd=25_000.0,
        onramp_monthly_limit_usd=250_000.0,
    )
    assert all(route.per_txn_limit_usd == 25_000.0 for route in r.onramp_routes)
    assert all(route.monthly_limit_usd == 250_000.0 for route in r.onramp_routes)


def test_build_report_ignores_non_dict_summary_overlay() -> None:
    overlay = {"summary": "not a dict"}  # type: ignore[dict-item]
    r = build_integrations_report(live_status=overlay)
    # Should still have canonical keys, extra was ignored.
    assert r.summary["venues_total"] == len(canonical_venues())


def test_build_report_handles_none_live_status() -> None:
    r = build_integrations_report(live_status=None)
    assert r.summary["bots_total"] == len(canonical_bots())


def test_build_report_model_dump_is_jsonable() -> None:
    r = build_integrations_report()
    payload = r.model_dump(mode="json")
    # Round-trip through json to make sure every field is serializable.
    s = json.dumps(payload)
    back = json.loads(s)
    assert back["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# render_text
# ---------------------------------------------------------------------------


def test_render_text_mentions_every_section_header() -> None:
    text = render_text(build_integrations_report())
    for header in (
        "SUMMARY",
        "VENUES",
        "BOTS",
        "FUNNEL LAYERS",
        "ONRAMP ROUTES",
        "STAKING",
        "OBSERVABILITY",
    ):
        assert header in text


def test_render_text_counts_match_report() -> None:
    r = build_integrations_report()
    text = render_text(r)
    for v in r.venues:
        assert v.name in text
    for b in r.bots:
        assert b.name in text


def test_render_text_handles_empty_report() -> None:
    r = IntegrationsReport(timestamp_utc="2026-04-17T00:00:00+00:00")
    text = render_text(r)
    assert "INTEGRATIONS MAP" in text


# ---------------------------------------------------------------------------
# CLI: build_integrations_report
# ---------------------------------------------------------------------------


def test_cli_load_live_status_missing_returns_none(tmp_path: Path) -> None:
    assert cli._load_live_status(tmp_path / "nope.json") is None


def test_cli_load_live_status_valid(tmp_path: Path) -> None:
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps({"bots": {"mnq": {"status": "LIVE"}}}))
    assert cli._load_live_status(path) == {"bots": {"mnq": {"status": "LIVE"}}}


def test_cli_load_live_status_malformed_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert cli._load_live_status(path) is None


def test_cli_load_live_status_non_object_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]))
    assert cli._load_live_status(path) is None


def test_cli_write_outputs_creates_dir_and_files(tmp_path: Path) -> None:
    out = tmp_path / "docs"
    r = build_integrations_report()
    cli._write_outputs(r, out)
    assert (out / "integrations_latest.json").exists()
    assert (out / "integrations_latest.txt").exists()
    # JSON is parseable.
    back = json.loads((out / "integrations_latest.json").read_text())
    assert back["schema_version"] == "1.0"


def test_cli_main_writes_canonical_outputs(tmp_path: Path) -> None:
    exit_code = cli.main(
        ["--out-dir", str(tmp_path), "--live-status", str(tmp_path / "missing.json")],
    )
    assert exit_code == 0
    assert (tmp_path / "integrations_latest.json").exists()
    assert (tmp_path / "integrations_latest.txt").exists()


def test_cli_main_respects_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps({"bots": {"mnq": {"status": "LIVE", "notes": "live-tiny"}}}),
    )
    exit_code = cli.main(
        ["--out-dir", str(tmp_path), "--live-status", str(overlay)],
    )
    assert exit_code == 0
    data = json.loads((tmp_path / "integrations_latest.json").read_text())
    mnq = next(b for b in data["bots"] if b["name"] == "mnq")
    assert mnq["status"] == "LIVE"
    assert mnq["notes"] == "live-tiny"
    assert data["summary"]["bots_live"] == 1


def test_cli_main_respects_onramp_overrides(tmp_path: Path) -> None:
    exit_code = cli.main(
        [
            "--out-dir",
            str(tmp_path),
            "--live-status",
            str(tmp_path / "missing.json"),
            "--onramp-per-txn-usd",
            "25000",
            "--onramp-monthly-usd",
            "250000",
        ]
    )
    assert exit_code == 0
    data = json.loads((tmp_path / "integrations_latest.json").read_text())
    for route in data["onramp_routes"]:
        assert route["per_txn_limit_usd"] == 25_000.0
        assert route["monthly_limit_usd"] == 250_000.0


def test_cli_main_print_flag_echoes_to_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        [
            "--out-dir",
            str(tmp_path),
            "--live-status",
            str(tmp_path / "missing.json"),
            "--print",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "INTEGRATIONS MAP" in captured.out
