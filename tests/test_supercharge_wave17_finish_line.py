"""Finish-line hardening for JARVIS/Sage/Quantum max batch."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path


def _bars(n: int = 80, *, trend: str = "up", symbol: str = "MNQ") -> list[dict]:
    rows = []
    price = 100.0
    drift = 0.45 if trend == "up" else -0.45
    for i in range(n):
        price += drift
        ts = datetime(2026, 4, 27, tzinfo=UTC) + timedelta(minutes=i)
        rows.append(
            {
                "ts": ts.isoformat().replace("+00:00", "Z"),
                "symbol": symbol,
                "open": price - 0.2,
                "high": price + 0.6,
                "low": price - 0.6,
                "close": price,
                "volume": 1000 + i,
            }
        )
    return rows


def test_online_updater_safely_shrinks_cold_bucket_and_persists(tmp_path: Path) -> None:
    from eta_engine.brain.online_learning import OnlineUpdater

    updater = OnlineUpdater(bot_name="mnq", alpha=0.5)
    for _ in range(5):
        updater.observe(feature_bucket="MNQ:long:conf_8", r_multiple=-1.0)

    decision = updater.sizing_decision("MNQ:long:conf_8")
    assert decision.status == "cold"
    assert decision.multiplier == 0.5

    path = tmp_path / "online.json"
    updater.save(path)
    restored = OnlineUpdater.load(path)
    assert restored.expected_r("MNQ:long:conf_8") == pytest.approx(-1.0)
    assert restored.health_summary()["status"] == "cold"


def test_preflight_online_learning_caps_size_when_flag_enabled(monkeypatch) -> None:
    from eta_engine.brain.jarvis_pre_flight import bot_pre_flight
    from eta_engine.brain.online_learning import OnlineUpdater

    class Bot:
        def __init__(self) -> None:
            self._online_updater = OnlineUpdater(bot_name="mnq", alpha=0.5)

        def _ask_jarvis(self, action, **payload):  # noqa: ANN001
            return True, 1.0, "approved"

    bot = Bot()
    for _ in range(5):
        bot._online_updater.observe(feature_bucket="setup_cold", r_multiple=-1.0)
    monkeypatch.setenv("ETA_FF_ONLINE_LEARNING", "true")

    decision = bot_pre_flight(
        bot=bot,
        symbol="MNQ",
        side="long",
        confluence=8.0,
        fleet_positions={},
        extra_payload={"feature_bucket": "setup_cold"},
    )

    assert decision.allowed is True
    assert decision.binding == "online_learning"
    assert decision.size_cap_mult == 0.5


def test_ml_school_fallback_contributes_without_model(monkeypatch) -> None:
    from eta_engine.brain.jarvis_v3.sage.base import Bias, MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.ml_school import MLSchool

    monkeypatch.setattr(MLSchool, "_model", None)
    monkeypatch.setattr(MLSchool, "_load_attempted", True)
    verdict = MLSchool().analyze(MarketContext(bars=_bars(80, trend="up"), side="long", symbol="MNQ"))

    assert verdict.bias == Bias.LONG
    assert verdict.conviction > 0.05
    assert verdict.aligned_with_entry is True
    assert verdict.signals["source"] == "deterministic_fallback"


def test_sage_optional_telemetry_payloads_reach_schools() -> None:
    from eta_engine.brain.jarvis_v3.sage.base import Bias, MarketContext
    from eta_engine.brain.jarvis_v3.sage.schools.cross_asset_correlation import (
        CrossAssetCorrelationSchool,
    )
    from eta_engine.brain.jarvis_v3.sage.schools.funding_basis import FundingBasisSchool

    bars = _bars(80, trend="up")
    funding = FundingBasisSchool().analyze(
        MarketContext(
            bars=bars,
            side="short",
            symbol="BTC",
            instrument_class="crypto",
            funding={"funding_rate_bps": 8.0, "perp_spot_basis_pct": 0.4},
        )
    )
    cross_asset = CrossAssetCorrelationSchool().analyze(
        MarketContext(
            bars=bars,
            side="long",
            symbol="BTC",
            peer_returns={"ETH": [0.01] * 40},
        )
    )

    assert funding.bias == Bias.SHORT
    assert funding.aligned_with_entry is True
    assert funding.signals["funding_rate_bps"] == 8.0
    assert cross_asset.signals["n_peers"] == 1


def test_sage_backtester_replays_jsonl_journal_with_file_bars(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.sage.backtester import main

    journal = tmp_path / "closed_trades.jsonl"
    journal.write_text(
        json.dumps(
            {
                "symbol": "MNQ",
                "side": "long",
                "entry_ts": "2026-04-27T01:00:00Z",
                "realized_r": 1.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    (bars_dir / "MNQ.jsonl").write_text(
        "\n".join(json.dumps(row) for row in _bars(80)),
        encoding="utf-8",
    )
    output = tmp_path / "sage_backtest.json"

    assert (
        main(
            [
                "--journal",
                str(journal),
                "--bars-source",
                str(bars_dir),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["n_trades"] == 1
    assert payload["n_replayed"] == 1


def test_meta_learner_mutates_parameter_selected_by_bandit() -> None:
    from eta_engine.brain.jarvis_v3.meta_learner_full import (
        MetaLearnerFull,
        MetaLearnerFullConfig,
    )

    learner = MetaLearnerFull(
        cfg=MetaLearnerFullConfig(n_challengers=1, max_experiments_per_day=1),
    )
    trial = learner.spawn_challengers(rng=random.Random(7))[0]
    before = learner.champion().to_dict()
    after = trial.challenger_cfg.to_dict()
    changed = [name for name, value in after.items() if value != before[name]]

    assert changed == [trial.parent_param_mutated]


def test_jarvis_decision_seed_is_stable_for_audit_replay() -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.orchestrator import _stable_decision_seed

    p1 = Proposal(
        signal_id="s1",
        direction="long",
        regime="trend",
        session="rth",
        stress=0.2,
        sentiment=0.4,
        sage_score=0.6,
        extra={"b": 2, "a": 1},
    )
    p2 = Proposal(
        signal_id="s1",
        direction="long",
        regime="trend",
        session="rth",
        stress=0.2,
        sentiment=0.4,
        sage_score=0.6,
        extra={"a": 1, "b": 2},
    )

    assert _stable_decision_seed(p1) == _stable_decision_seed(p2)


def test_jarvis_health_endpoint_surfaces_authority_and_quantum(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ETA_STATE_DIR", str(tmp_path))
    quantum_dir = tmp_path / "quantum"
    quantum_dir.mkdir()
    (quantum_dir / "jobs.jsonl").write_text(
        json.dumps(
            {
                "backend": "classical_sa",
                "fell_back_to_classical": True,
                "cost_estimate_usd": 0.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    from eta_engine.deploy.scripts.dashboard_api import app

    payload = TestClient(app).get("/api/jarvis/health").json()
    assert payload["policy_authority"] == "JARVIS"
    assert payload["quantum"]["recent_jobs"] == 1
    assert payload["quantum"]["recent_fallbacks"] == 1
