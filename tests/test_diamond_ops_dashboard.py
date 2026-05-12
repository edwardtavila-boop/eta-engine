"""Tests for diamond_ops_dashboard — wave-13 unified status surface."""
# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path


def _stub_promotion(verdict: str = "REJECT") -> dict:
    return {"bot_id": "test_bot", "verdict": verdict}


def _stub_sizing(verdict: str = "SIZING_OK", cum_r: float = 1.0,
                 cum_usd: float = 100.0, n: int = 25) -> dict:
    return {
        "bot_id": "test_bot",
        "verdict": verdict,
        "cum_r": cum_r,
        "cum_usd": cum_usd,
        "n_trades_with_pnl": n,
    }


def _stub_watchdog(cls: str = "HEALTHY",
                    cls_usd: str = "HEALTHY",
                    cls_r: str = "HEALTHY") -> dict:
    return {
        "bot_id": "test_bot",
        "classification": cls,
        "classification_usd": cls_usd,
        "classification_r": cls_r,
    }


def _stub_direction(verdict: str = "SYMMETRIC",
                     long_avg: float | None = 0.4,
                     short_avg: float | None = 0.4,
                     n_total: int = 25) -> dict:
    out: dict = {
        "bot_id": "test_bot",
        "verdict": verdict,
        "n_total": n_total,
        "long": {"avg_r": long_avg} if long_avg is not None else {},
        "short": {"avg_r": short_avg} if short_avg is not None else {},
    }
    return out


# ────────────────────────────────────────────────────────────────────
# _synthesize — priority assignment and recommended actions
# ────────────────────────────────────────────────────────────────────


def test_priority_P0_when_watchdog_CRITICAL() -> None:
    """Watchdog CRITICAL = P0_CRITICAL regardless of other signals."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_OK"),
        watchdog=_stub_watchdog(cls="CRITICAL", cls_usd="CRITICAL", cls_r="HEALTHY"),
        direction=_stub_direction(),
    )
    assert syn.priority == "P0_CRITICAL"
    # USD-only CRITICAL with R-HEALTHY -> action recommends sizing fix
    assert "SIZING failure" in syn.recommended_action


def test_priority_P0_when_sizing_BREACHED() -> None:
    """Sizing BREACHED = P0 even if watchdog HEALTHY (single-stopout fragile)."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_BREACHED"),
        watchdog=_stub_watchdog(cls="HEALTHY"),
        direction=_stub_direction(),
    )
    assert syn.priority == "P0_CRITICAL"
    assert "halve risk_per_trade_pct" in syn.recommended_action


def test_priority_P0_recommends_strategy_retire_when_R_critical() -> None:
    """R-CRITICAL means strategy edge has decayed (not sizing).
    The action recommendation must point to operator retire decision,
    not a sizing fix."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_OK"),
        watchdog=_stub_watchdog(cls="CRITICAL", cls_usd="CRITICAL", cls_r="CRITICAL"),
        direction=_stub_direction(),
    )
    assert syn.priority == "P0_CRITICAL"
    assert "strategy edge has" in syn.recommended_action
    assert "retire" in syn.recommended_action


def test_priority_P1_when_watchdog_WARN() -> None:
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_OK"),
        watchdog=_stub_watchdog(cls="WARN"),
        direction=_stub_direction(),
    )
    assert syn.priority == "P1_REVIEW"


def test_priority_P1_when_sizing_FRAGILE() -> None:
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_FRAGILE"),
        watchdog=_stub_watchdog(cls="HEALTHY"),
        direction=_stub_direction(),
    )
    assert syn.priority == "P1_REVIEW"
    assert "FRAGILE" in syn.recommended_action


def test_priority_P1_when_direction_LONG_ONLY_EDGE() -> None:
    """Asymmetric edge with one negative side: P1 — operator should
    consider filtering the weak side once n>=100."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_OK"),
        watchdog=_stub_watchdog(cls="HEALTHY"),
        direction=_stub_direction(verdict="LONG_ONLY_EDGE"),
    )
    assert syn.priority == "P1_REVIEW"
    assert "weak side is net negative" in syn.recommended_action


def test_priority_P2_when_watchdog_WATCH_or_sizing_TIGHT() -> None:
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_TIGHT"),
        watchdog=_stub_watchdog(cls="WATCH"),
        direction=_stub_direction(),
    )
    assert syn.priority == "P2_MONITOR"


def test_priority_P2_when_direction_LONG_DOMINANT() -> None:
    """Both sides positive but one stronger — P2 (no urgent action)."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(),
        sizing=_stub_sizing(verdict="SIZING_OK"),
        watchdog=_stub_watchdog(cls="HEALTHY"),
        direction=_stub_direction(verdict="LONG_DOMINANT"),
    )
    assert syn.priority == "P2_MONITOR"
    assert "LONG_DOMINANT" in syn.recommended_action


def test_priority_P3_when_all_green() -> None:
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(verdict="PROMOTE"),
        sizing=_stub_sizing(verdict="SIZING_OK"),
        watchdog=_stub_watchdog(cls="HEALTHY"),
        direction=_stub_direction(verdict="SYMMETRIC"),
    )
    assert syn.priority == "P3_OK"
    assert "no action" in syn.recommended_action


def test_priority_P4_when_insufficient_data_everywhere() -> None:
    """When the watchdog is INCONCLUSIVE and sizing INSUFFICIENT_DATA,
    we have nothing to act on — P4 (passive wait)."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    syn = dd._synthesize(
        "test_bot", enrolled=True,
        promotion=_stub_promotion(verdict="REJECT"),
        sizing=_stub_sizing(verdict="INSUFFICIENT_DATA"),
        watchdog=_stub_watchdog(cls="INCONCLUSIVE",
                                  cls_usd="INCONCLUSIVE", cls_r="INCONCLUSIVE"),
        direction=_stub_direction(verdict="INSUFFICIENT_DATA"),
    )
    assert syn.priority == "P4_INSUFFICIENT_DATA"
    assert "let trades accumulate" in syn.recommended_action


# ────────────────────────────────────────────────────────────────────
# Worst-first sort + JSON receipt
# ────────────────────────────────────────────────────────────────────


def test_priority_order_constants_match_documentation() -> None:
    """The internal priority order must rank worst-first (P0 highest)."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    order = dd._PRIORITY_ORDER
    assert order["P0_CRITICAL"] > order["P1_REVIEW"] > order["P2_MONITOR"]
    assert order["P2_MONITOR"] > order["P3_OK"] > order["P4_INSUFFICIENT_DATA"]


def test_run_writes_json_receipt(tmp_path: Path, monkeypatch: object) -> None:
    """run() invokes all 4 sub-audits and persists a synthesis."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    # Stub all four sub-audits to return an empty fixture.  We don't
    # need real data here — we just need the runner to compose them
    # into the synthesis JSON without crashing.
    monkeypatch.setattr(dd, "_run_promotion_gate", lambda: {})  # type: ignore[attr-defined]
    monkeypatch.setattr(dd, "_run_sizing_audit", lambda: {})  # type: ignore[attr-defined]
    monkeypatch.setattr(dd, "_run_watchdog", lambda: {})  # type: ignore[attr-defined]
    monkeypatch.setattr(dd, "_run_direction_stratify", lambda: {})  # type: ignore[attr-defined]
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(dd, "OUT_LATEST", out_path)  # type: ignore[attr-defined]

    summary = dd.run()
    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert "ts" in on_disk
    assert "syntheses" in on_disk
    assert on_disk["n_diamonds"] == summary["n_diamonds"]
    # Every diamond should appear (enrolled=True regardless of audit data)
    assert len(on_disk["syntheses"]) == summary["n_diamonds"]
    for syn in on_disk["syntheses"]:
        assert syn["enrolled"] is True


def test_safe_run_swallows_exceptions() -> None:
    """If a sub-audit crashes, the dashboard must not propagate the
    exception — it returns {} and prints a warning so the dashboard
    can still report on the OTHER signals."""
    from eta_engine.scripts import diamond_ops_dashboard as dd

    def _raises() -> dict:
        msg = "synthetic"
        raise RuntimeError(msg)

    out = dd._safe_run("crashy_audit", _raises)
    assert out == {}
