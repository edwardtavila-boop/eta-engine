"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_regime_exclusion.

Tests the HIGH_VOL exclusion gate that closed
``cross_regime_validation 2026-04-17`` (sign-flip overfit).

Coverage:
  * Default config excludes HIGH_VOL + CRISIS
  * Disk override is honoured (mtime-keyed cache)
  * Corrupt JSON falls through to defaults (no raise)
  * ``_risk_mult`` in eta_policy zeroes for excluded regimes
  * Non-excluded regimes still receive their old multipliers
  * Cache invalidation works
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.strategies import regime_exclusion as rex
from eta_engine.strategies.eta_policy import StrategyContext, _risk_mult

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect _CONFIG_PATH to a tmp file so tests don't poison the
    real ``docs/cross_regime/regime_exclusions.json`` and don't see
    each other's writes."""
    cfg = tmp_path / "regime_exclusions.json"
    monkeypatch.setattr(rex, "_CONFIG_PATH", cfg)
    rex._invalidate_cache()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaultExclusions:
    def test_high_vol_excluded_by_default(self) -> None:
        d = rex.is_regime_excluded("HIGH_VOL")
        assert d.excluded is True
        assert "sign-flip" in d.reason.lower()

    def test_crisis_excluded_by_default(self) -> None:
        d = rex.is_regime_excluded("CRISIS")
        assert d.excluded is True
        assert "crisis" in d.reason.lower()

    def test_trending_not_excluded(self) -> None:
        d = rex.is_regime_excluded("TRENDING")
        assert d.excluded is False
        assert d.reason == ""

    def test_ranging_not_excluded(self) -> None:
        d = rex.is_regime_excluded("RANGING")
        assert d.excluded is False

    def test_low_vol_not_excluded_via_this_gate(self) -> None:
        # LOW_VOL is killed by the legacy structural rule in _risk_mult,
        # not by the OOS exclusion gate. The exclusion gate's job is
        # only to enforce findings from cross_regime_validation.
        d = rex.is_regime_excluded("LOW_VOL")
        assert d.excluded is False

    def test_unknown_label_fails_open(self) -> None:
        d = rex.is_regime_excluded("MARS_LANDED")
        assert d.excluded is False

    def test_case_insensitive(self) -> None:
        assert rex.is_regime_excluded("high_vol").excluded is True
        assert rex.is_regime_excluded("Trending").excluded is False

    def test_decision_is_falsy_when_not_excluded(self) -> None:
        # __bool__ enables `if is_regime_excluded(...): ...`
        d = rex.is_regime_excluded("TRENDING")
        assert bool(d) is False
        d2 = rex.is_regime_excluded("HIGH_VOL")
        assert bool(d2) is True


# ---------------------------------------------------------------------------
# Disk config
# ---------------------------------------------------------------------------


class TestDiskOverride:
    def test_loader_reads_excluded_regimes_block(self) -> None:
        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps(
                {
                    "spec_id": "REGIME_EXCLUSION_v1",
                    "excluded_regimes": {
                        "RANGING": "test override -- temporarily blocked",
                    },
                }
            ),
            encoding="utf-8",
        )
        rex._invalidate_cache()
        excl = rex.excluded_regimes()
        assert "RANGING" in excl
        # HIGH_VOL is no longer present because we replaced the whole map
        assert "HIGH_VOL" not in excl

    def test_loader_accepts_flat_dict(self) -> None:
        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps({"HIGH_VOL": "still bad"}),
            encoding="utf-8",
        )
        rex._invalidate_cache()
        d = rex.is_regime_excluded("HIGH_VOL")
        assert d.excluded is True
        assert d.reason == "still bad"

    def test_corrupt_json_falls_back_to_defaults(self) -> None:
        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("not-valid-json{{", encoding="utf-8")
        rex._invalidate_cache()
        # Defaults still in effect -> HIGH_VOL excluded
        assert rex.is_regime_excluded("HIGH_VOL").excluded is True

    def test_non_dict_payload_falls_back_to_defaults(self) -> None:
        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('["HIGH_VOL"]', encoding="utf-8")
        rex._invalidate_cache()
        assert rex.is_regime_excluded("HIGH_VOL").excluded is True

    def test_mtime_cache_picks_up_edits(self) -> None:
        import os
        import time

        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps({"excluded_regimes": {"RANGING": "v1"}}),
            encoding="utf-8",
        )
        rex._invalidate_cache()
        assert rex.is_regime_excluded("RANGING").reason == "v1"

        # Bump mtime forward and rewrite -- no need for invalidate this time
        time.sleep(0.01)
        cfg.write_text(
            json.dumps({"excluded_regimes": {"RANGING": "v2"}}),
            encoding="utf-8",
        )
        # Force mtime to differ even on coarse-resolution filesystems
        future = cfg.stat().st_mtime + 1
        os.utime(cfg, (future, future))
        assert rex.is_regime_excluded("RANGING").reason == "v2"


class TestWriteDefaultConfig:
    def test_writes_when_absent(self) -> None:
        cfg = rex._CONFIG_PATH
        assert not cfg.exists()
        path = rex.write_default_config()
        assert path == cfg
        assert cfg.exists()
        payload = json.loads(cfg.read_text(encoding="utf-8"))
        assert payload["spec_id"] == "REGIME_EXCLUSION_v1"
        assert "HIGH_VOL" in payload["excluded_regimes"]

    def test_no_overwrite_without_force(self) -> None:
        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("user-edited", encoding="utf-8")
        rex.write_default_config()
        assert cfg.read_text(encoding="utf-8") == "user-edited"

    def test_force_overwrites(self) -> None:
        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("user-edited", encoding="utf-8")
        rex.write_default_config(force=True)
        payload = json.loads(cfg.read_text(encoding="utf-8"))
        assert "HIGH_VOL" in payload["excluded_regimes"]


# ---------------------------------------------------------------------------
# Integration with eta_policy._risk_mult
# ---------------------------------------------------------------------------


class TestRiskMultIntegration:
    def test_high_vol_zeroes_risk(self) -> None:
        ctx = StrategyContext(regime_label="HIGH_VOL")
        assert _risk_mult(ctx, base_mult=1.0) == 0.0

    def test_crisis_zeroes_risk(self) -> None:
        ctx = StrategyContext(regime_label="CRISIS")
        assert _risk_mult(ctx, base_mult=1.0) == 0.0

    def test_trending_passes_through(self) -> None:
        ctx = StrategyContext(regime_label="TRENDING")
        # base_mult survives clamping to [0, 1.5]
        assert _risk_mult(ctx, base_mult=1.0) == 1.0

    def test_ranging_passes_through(self) -> None:
        ctx = StrategyContext(regime_label="RANGING")
        assert _risk_mult(ctx, base_mult=1.0) == 1.0

    def test_low_vol_legacy_zero_still_applies(self) -> None:
        # LOW_VOL is killed by the structural rule, not the exclusion
        # gate. Verify this still holds so we haven't regressed.
        ctx = StrategyContext(regime_label="LOW_VOL")
        assert _risk_mult(ctx, base_mult=1.0) == 0.0

    def test_kill_switch_overrides_anything(self) -> None:
        ctx = StrategyContext(
            regime_label="TRENDING",
            kill_switch_active=True,
        )
        assert _risk_mult(ctx, base_mult=1.0) == 0.0

    def test_session_closed_overrides_anything(self) -> None:
        ctx = StrategyContext(
            regime_label="TRENDING",
            session_allows_entries=False,
        )
        assert _risk_mult(ctx, base_mult=1.0) == 0.0

    def test_high_vol_can_be_re_enabled_via_config(
        self,
        tmp_path: Path,
    ) -> None:
        cfg = rex._CONFIG_PATH
        cfg.parent.mkdir(parents=True, exist_ok=True)
        # Empty exclusion map -> HIGH_VOL no longer blocked
        cfg.write_text(
            json.dumps({"excluded_regimes": {}}),
            encoding="utf-8",
        )
        rex._invalidate_cache()
        ctx = StrategyContext(regime_label="HIGH_VOL")
        # Should now pass through full base_mult (vol_z=0, no other penalties)
        assert _risk_mult(ctx, base_mult=1.0) == 1.0

    def test_vol_z_penalty_still_applies_to_non_excluded(self) -> None:
        ctx = StrategyContext(regime_label="TRENDING", vol_z=3.0)
        # vol_z > 2.5 -> *0.5
        assert _risk_mult(ctx, base_mult=1.0) == 0.5
