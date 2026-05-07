"""Tests for the kaizen-loop sidecar deactivation override.

Coverage:
  * per_bot_registry.is_active() honors the sidecar
  * kaizen_loop._apply_kaizen_deactivation() writes the sidecar
  * kaizen_reactivate.reactivate() removes entries
  * 2-run confirmation gate (HELD on first sighting, APPLIED on second)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def tmp_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch every module that owns a _OVERRIDES_PATH / _KAIZEN_OVERRIDES_PATH
    constant to point at a tmp file, so each test runs in isolation.
    """
    sidecar = tmp_path / "kaizen_overrides.json"

    from eta_engine.scripts import kaizen_loop, kaizen_reactivate
    from eta_engine.strategies import per_bot_registry

    monkeypatch.setattr(kaizen_loop, "_OVERRIDES_PATH", sidecar)
    monkeypatch.setattr(kaizen_reactivate, "_OVERRIDES_PATH", sidecar)
    monkeypatch.setattr(
        per_bot_registry, "_KAIZEN_OVERRIDES_PATH", sidecar,
    )
    return sidecar


@pytest.fixture()
def tmp_action_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "kaizen_actions.jsonl"
    from eta_engine.scripts import kaizen_loop
    monkeypatch.setattr(kaizen_loop, "_ACTION_LOG", log)
    return log


# ---------------------------------------------------------------------------
# is_active honors the sidecar
# ---------------------------------------------------------------------------


def test_is_active_true_when_sidecar_missing(tmp_overrides: Path) -> None:
    """Empty / missing sidecar → registry decides on its own.

    Uses ``rsi_mr_mnq`` as a known-active reference. It is the top survivor
    of the 2026-05-07 strict-gate audit (137 trades, Sharpe 1.91,
    expR_net +0.124, split_half_sign_stable=True). Previous reference
    ``btc_optimized`` was retired 2026-05-07 (Sharpe -2.82).
    """
    from eta_engine.strategies.per_bot_registry import is_bot_active

    assert is_bot_active("rsi_mr_mnq") is True
    assert not tmp_overrides.exists()  # no sidecar created accidentally


def test_is_active_false_when_listed_in_sidecar(tmp_overrides: Path) -> None:
    """Bot in sidecar → drops out of is_active()."""
    tmp_overrides.write_text(
        json.dumps({"deactivated": {
            "rsi_mr_mnq": {
                "applied_at": "2026-05-05T06:00:00+00:00",
                "reason": "tier=DECAY mc=DEAD test fixture",
            },
        }}),
        encoding="utf-8",
    )

    from eta_engine.strategies.per_bot_registry import is_bot_active

    assert is_bot_active("rsi_mr_mnq") is False


def test_is_active_ignores_malformed_sidecar(tmp_overrides: Path) -> None:
    """Garbage sidecar → fail-safe to registry-only behavior."""
    tmp_overrides.write_text("{ this isn't valid JSON", encoding="utf-8")

    from eta_engine.strategies.per_bot_registry import is_bot_active

    # Still active because the parse failed and we returned {}.
    assert is_bot_active("rsi_mr_mnq") is True


def test_is_active_ignores_wrong_schema(tmp_overrides: Path) -> None:
    """Sidecar with deactivated=<list> instead of dict → ignored safely."""
    tmp_overrides.write_text(
        json.dumps({"deactivated": ["rsi_mr_mnq"]}),  # wrong shape
        encoding="utf-8",
    )

    from eta_engine.strategies.per_bot_registry import is_bot_active

    assert is_bot_active("rsi_mr_mnq") is True


def test_registry_deactivation_takes_precedence_over_missing_sidecar(
    tmp_overrides: Path,
) -> None:
    """Bot deactivated at registry-level stays inactive even with empty sidecar."""
    from eta_engine.strategies.per_bot_registry import is_bot_active

    # eth_perp was deactivated 2026-05-05 by elite-scoreboard evidence.
    assert is_bot_active("eth_perp") is False


# ---------------------------------------------------------------------------
# kaizen_loop writes the sidecar correctly
# ---------------------------------------------------------------------------


def test_apply_kaizen_deactivation_creates_sidecar(tmp_overrides: Path) -> None:
    """First-time apply creates the file with the bot listed."""
    from eta_engine.scripts.kaizen_loop import _apply_kaizen_deactivation

    _apply_kaizen_deactivation("ghost_bot", {
        "reason": "test",
        "tier": "DECAY", "mc_verdict": "DEAD",
        "expectancy_r": -0.05, "n": 50,
    })

    assert tmp_overrides.exists()
    data = json.loads(tmp_overrides.read_text(encoding="utf-8"))
    assert "ghost_bot" in data["deactivated"]
    rec = data["deactivated"]["ghost_bot"]
    assert rec["tier"] == "DECAY"
    assert rec["mc_verdict"] == "DEAD"
    assert rec["expectancy_r"] == -0.05
    assert rec["n"] == 50
    assert "applied_at" in rec  # ISO timestamp added


def test_apply_kaizen_deactivation_is_idempotent(tmp_overrides: Path) -> None:
    """Re-applying for the same bot just updates the timestamp."""
    from eta_engine.scripts.kaizen_loop import _apply_kaizen_deactivation

    rec = {"reason": "first", "tier": "DECAY", "mc_verdict": "MIXED",
           "expectancy_r": -0.01, "n": 30}
    _apply_kaizen_deactivation("ghost_bot", rec)
    first_data = json.loads(tmp_overrides.read_text(encoding="utf-8"))

    rec["reason"] = "second"
    _apply_kaizen_deactivation("ghost_bot", rec)
    second_data = json.loads(tmp_overrides.read_text(encoding="utf-8"))

    # Still only one bot in the override list.
    assert list(second_data["deactivated"].keys()) == ["ghost_bot"]
    # Reason updated.
    assert second_data["deactivated"]["ghost_bot"]["reason"] == "second"
    # Timestamp may or may not differ (sub-second resolution); structure intact.
    assert "applied_at" in second_data["deactivated"]["ghost_bot"]
    assert isinstance(first_data["deactivated"], dict)


def test_apply_kaizen_deactivation_preserves_other_entries(
    tmp_overrides: Path,
) -> None:
    """Adding a new bot doesn't drop existing entries."""
    from eta_engine.scripts.kaizen_loop import _apply_kaizen_deactivation

    _apply_kaizen_deactivation("bot_a", {"reason": "x", "tier": "DECAY",
                                          "mc_verdict": "DEAD",
                                          "expectancy_r": -0.01, "n": 30})
    _apply_kaizen_deactivation("bot_b", {"reason": "y", "tier": "DECAY",
                                          "mc_verdict": "MIXED",
                                          "expectancy_r": -0.02, "n": 35})

    data = json.loads(tmp_overrides.read_text(encoding="utf-8"))
    assert set(data["deactivated"].keys()) == {"bot_a", "bot_b"}


# ---------------------------------------------------------------------------
# kaizen_reactivate
# ---------------------------------------------------------------------------


def test_reactivate_removes_listed_bot(tmp_overrides: Path) -> None:
    """`kaizen_reactivate <bot>` removes the entry from the sidecar."""
    tmp_overrides.write_text(json.dumps({"deactivated": {
        "ghost_bot": {"applied_at": "2026-05-05T00:00:00+00:00",
                      "reason": "test"},
        "other_bot": {"applied_at": "2026-05-05T00:00:00+00:00",
                      "reason": "test"},
    }}), encoding="utf-8")

    from eta_engine.scripts.kaizen_reactivate import reactivate

    rc = reactivate(["ghost_bot"])
    assert rc == 0

    data = json.loads(tmp_overrides.read_text(encoding="utf-8"))
    assert "ghost_bot" not in data["deactivated"]
    assert "other_bot" in data["deactivated"]  # untouched


def test_reactivate_handles_unknown_bot(tmp_overrides: Path) -> None:
    """Reactivating a bot that's not in the sidecar is a no-op (rc=1)."""
    tmp_overrides.write_text(json.dumps({"deactivated": {}}), encoding="utf-8")

    from eta_engine.scripts.kaizen_reactivate import reactivate

    rc = reactivate(["never_deactivated_bot"])
    assert rc == 1  # nothing to do — caller can branch on this


def test_clear_all_empties_sidecar(tmp_overrides: Path) -> None:
    tmp_overrides.write_text(json.dumps({"deactivated": {
        "a": {"reason": "x"}, "b": {"reason": "y"}, "c": {"reason": "z"},
    }}), encoding="utf-8")

    from eta_engine.scripts.kaizen_reactivate import clear_all

    rc = clear_all()
    assert rc == 0
    data = json.loads(tmp_overrides.read_text(encoding="utf-8"))
    assert data["deactivated"] == {}


# ---------------------------------------------------------------------------
# 2-run confirmation gate
# ---------------------------------------------------------------------------


def test_first_run_holds_pending_confirmation(
    tmp_overrides: Path, tmp_action_log: Path,
) -> None:
    """First time a bot meets RETIRE criteria, status=HELD_PENDING_CONFIRMATION."""
    from eta_engine.scripts import kaizen_loop

    elite = {"bots": {"bad_bot": {
        "tier": "DECAY", "n": 50, "profit_factor": 0.6,
        "sharpe": -0.4, "expectancy_r": -0.02,
        "max_drawdown_r": 25, "rolling_decay_pct": 80,
        "sum_pnl_usd": -2500,
    }}}
    mc = {"bots": {"bad_bot": {"verdict": "DEAD", "n": 50,
                                "p05_final_R": -0.5, "p_negative": 0.99,
                                "luck_score": 0.1, "actual_final_R": -0.3}}}

    with patch.object(kaizen_loop, "_run_elite_scoreboard", return_value=elite), \
         patch.object(kaizen_loop, "_run_monte_carlo", return_value=mc), \
         patch.object(kaizen_loop, "_read_edge_tracker_snapshot", return_value={}):
        report = kaizen_loop.run_loop(apply_actions=True)

    assert report["held_count"] == 1
    assert report["applied_count"] == 0
    # Sidecar should NOT have been written yet
    assert not tmp_overrides.exists() or json.loads(
        tmp_overrides.read_text(encoding="utf-8"),
    ).get("deactivated") in (None, {})


def test_second_run_applies_after_confirmation(
    tmp_overrides: Path, tmp_action_log: Path,
) -> None:
    """When the same RETIRE recommendation appears twice, --apply triggers
    sidecar write + status=APPLIED."""
    # Pre-seed the action log as if there was a prior run yesterday.
    tmp_action_log.write_text(
        json.dumps({
            "ts": "2026-05-04T06:00:00+00:00",
            "action": "RETIRE",
            "bot_id": "bad_bot",
            "reason": "tier=DECAY mc=DEAD",
            "status": "RECOMMENDED",
        }) + "\n",
        encoding="utf-8",
    )

    from eta_engine.scripts import kaizen_loop

    elite = {"bots": {"bad_bot": {
        "tier": "DECAY", "n": 50, "profit_factor": 0.6,
        "sharpe": -0.4, "expectancy_r": -0.02,
        "max_drawdown_r": 25, "rolling_decay_pct": 80,
        "sum_pnl_usd": -2500,
    }}}
    mc = {"bots": {"bad_bot": {"verdict": "DEAD", "n": 50,
                                "p05_final_R": -0.5, "p_negative": 0.99,
                                "luck_score": 0.1, "actual_final_R": -0.3}}}

    with patch.object(kaizen_loop, "_run_elite_scoreboard", return_value=elite), \
         patch.object(kaizen_loop, "_run_monte_carlo", return_value=mc), \
         patch.object(kaizen_loop, "_read_edge_tracker_snapshot", return_value={}):
        report = kaizen_loop.run_loop(apply_actions=True)

    assert report["held_count"] == 0
    assert report["applied_count"] == 1
    # Sidecar should now have bad_bot deactivated
    assert tmp_overrides.exists()
    data = json.loads(tmp_overrides.read_text(encoding="utf-8"))
    assert "bad_bot" in data["deactivated"]
    assert data["deactivated"]["bad_bot"]["tier"] == "DECAY"
    assert data["deactivated"]["bad_bot"]["mc_verdict"] == "DEAD"


def test_report_only_never_writes_sidecar(
    tmp_overrides: Path, tmp_action_log: Path,
) -> None:
    """Without --apply, even a 2-run-confirmed RETIRE doesn't deactivate."""
    tmp_action_log.write_text(
        json.dumps({"action": "RETIRE", "bot_id": "bad_bot",
                    "ts": "2026-05-04T06:00:00+00:00",
                    "reason": "prior", "status": "RECOMMENDED"}) + "\n",
        encoding="utf-8",
    )

    from eta_engine.scripts import kaizen_loop

    elite = {"bots": {"bad_bot": {
        "tier": "DECAY", "n": 50, "profit_factor": 0.6,
        "sharpe": -0.4, "expectancy_r": -0.02,
        "max_drawdown_r": 25, "rolling_decay_pct": 80,
        "sum_pnl_usd": -2500,
    }}}
    mc = {"bots": {"bad_bot": {"verdict": "DEAD", "n": 50,
                                "p05_final_R": -0.5, "p_negative": 0.99,
                                "luck_score": 0.1, "actual_final_R": -0.3}}}

    with patch.object(kaizen_loop, "_run_elite_scoreboard", return_value=elite), \
         patch.object(kaizen_loop, "_run_monte_carlo", return_value=mc), \
         patch.object(kaizen_loop, "_read_edge_tracker_snapshot", return_value={}):
        report = kaizen_loop.run_loop(apply_actions=False)  # REPORT-ONLY

    assert report["applied_count"] == 0
    assert report["held_count"] == 0  # held_count only increments on --apply path
    # Sidecar must NOT exist or have no deactivations.
    if tmp_overrides.exists():
        data = json.loads(tmp_overrides.read_text(encoding="utf-8"))
        assert data.get("deactivated", {}) == {}
