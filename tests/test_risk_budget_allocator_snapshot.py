from __future__ import annotations

import json

from eta_engine.brain.jarvis_v3.risk_budget_allocator import current_envelope, size_for_proposal


def test_current_envelope_prefers_snapshot_when_present(tmp_path) -> None:
    snapshot = tmp_path / "risk_budget_snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-05-15T12:00:00+00:00",
                "fleet": {"mtd_r": 6.0, "drawdown_r": -1.0, "n_trades_mtd": 12},
                "bots": {},
            }
        ),
        encoding="utf-8",
    )

    mult = current_envelope(snapshot_path=snapshot, log_path=tmp_path / "missing.jsonl")

    assert mult.multiplier > 1.0
    assert mult.n_trades_mtd == 12
    assert "snapshot" in mult.reason.lower()


def test_current_envelope_falls_back_to_log_when_snapshot_missing(tmp_path) -> None:
    log = tmp_path / "trades.jsonl"
    log.write_text(
        '{"ts": "2026-05-15T12:00:00+00:00", "realized_r": -7.0, "bot_id": "x"}\n',
        encoding="utf-8",
    )

    mult = current_envelope(snapshot_path=tmp_path / "missing.json", log_path=log)

    assert mult.multiplier == 0.0
    assert "STAND-DOWN" in mult.reason


def test_size_for_proposal_passes_snapshot_path_through(tmp_path) -> None:
    snapshot = tmp_path / "risk_budget_snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-05-15T12:00:00+00:00",
                "fleet": {"mtd_r": 0.0, "drawdown_r": 0.0, "n_trades_mtd": 0},
                "bots": {"mnq": {"mtd_r": 5.5, "drawdown_r": -0.5, "n_trades_mtd": 8}},
            }
        ),
        encoding="utf-8",
    )

    adjusted, mult = size_for_proposal(base_size=2.0, bot_id="mnq", snapshot_path=snapshot)

    assert adjusted > 2.0
    assert mult.n_trades_mtd == 8
