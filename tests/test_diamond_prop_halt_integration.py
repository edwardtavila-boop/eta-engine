"""Tests for the wave-22 prop-guard integration surface.

This wave wires the drawdown guard into TWO operator-visible channels:

  1. alerts_log.jsonl  - dashboard reads this for the daily alerts panel
  2. flag files        - supervisor reads these BEFORE each entry decision
                         (prop_halt_active.flag / prop_watch_active.flag)

Plus three thin capital_allocator helpers the supervisor can use:

  get_prop_guard_signal()      - return "HALT" / "WATCH" / "OK"
  should_block_prop_entry(bid) - True if bot is prop_ready AND signal=HALT
  prop_entry_size_multiplier() - 0.0 / 0.5 / 1.0
"""

# ruff: noqa: N802, PLR2004, SLF001
from __future__ import annotations

import json
from pathlib import Path


def _make_receipt(
    signal: str,
    prop_ready_bots: list[str] | None = None,
    daily_pnl: float = 0.0,
    total_pnl: float = 0.0,
    consistency: float | None = None,
) -> object:
    """Build a GuardReceipt fixture in the requested signal state."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    return dg.GuardReceipt(
        ts="2026-05-12T23:00:00+00:00",
        account_size=50_000.0,
        prop_ready_bots=prop_ready_bots
        or [
            "m2k_sweep_reclaim",
            "met_sweep_reclaim",
            "mes_sweep_reclaim_v2",
        ],
        daily_pnl_usd=daily_pnl,
        total_pnl_usd=total_pnl,
        consistency_ratio=consistency,
        signal=signal,
        rationale=f"synthetic {signal} fixture",
    )


# ────────────────────────────────────────────────────────────────────
# Flag file emission
# ────────────────────────────────────────────────────────────────────


def test_HALT_writes_halt_flag_file(tmp_path: Path, monkeypatch: object) -> None:
    """signal=HALT writes prop_halt_active.flag containing receipt context."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    halt_flag = tmp_path / "halt.flag"
    watch_flag = tmp_path / "watch.flag"
    monkeypatch.setattr(dg, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(dg, "PROP_WATCH_FLAG_PATH", watch_flag)  # type: ignore[attr-defined]

    receipt = _make_receipt("HALT", daily_pnl=-1600.0, total_pnl=-2000.0)
    dg._emit_signal_flags(receipt)
    assert halt_flag.exists()
    assert not watch_flag.exists()
    flag_content = json.loads(halt_flag.read_text(encoding="utf-8"))
    assert flag_content["rationale"] == "synthetic HALT fixture"


def test_WATCH_writes_watch_flag_clears_halt(tmp_path: Path, monkeypatch: object) -> None:
    """signal=WATCH writes watch flag + clears any stale halt flag."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    halt_flag = tmp_path / "halt.flag"
    watch_flag = tmp_path / "watch.flag"
    # Seed a stale HALT flag
    halt_flag.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(dg, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(dg, "PROP_WATCH_FLAG_PATH", watch_flag)  # type: ignore[attr-defined]

    receipt = _make_receipt("WATCH")
    dg._emit_signal_flags(receipt)
    assert watch_flag.exists()
    assert not halt_flag.exists(), "stale HALT flag must be cleared on WATCH"


def test_OK_clears_both_flag_files(tmp_path: Path, monkeypatch: object) -> None:
    """signal=OK clears any leftover flags so supervisor resumes normal entry."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    halt_flag = tmp_path / "halt.flag"
    watch_flag = tmp_path / "watch.flag"
    halt_flag.write_text("stale", encoding="utf-8")
    watch_flag.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(dg, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(dg, "PROP_WATCH_FLAG_PATH", watch_flag)  # type: ignore[attr-defined]

    receipt = _make_receipt("OK")
    dg._emit_signal_flags(receipt)
    assert not halt_flag.exists()
    assert not watch_flag.exists()


# ────────────────────────────────────────────────────────────────────
# Alerts pipeline
# ────────────────────────────────────────────────────────────────────


def test_HALT_appends_RED_alert(tmp_path: Path, monkeypatch: object) -> None:
    """HALT signal writes a RED-severity entry to alerts_log.jsonl."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    alerts_log = tmp_path / "alerts.jsonl"
    monkeypatch.setattr(dg, "ALERTS_LOG", alerts_log)  # type: ignore[attr-defined]

    receipt = _make_receipt("HALT", daily_pnl=-1600.0)
    dg._fire_alerts(receipt)
    assert alerts_log.exists()
    lines = alerts_log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    alert = json.loads(lines[0])
    assert alert["severity"] == "RED"
    assert alert["source"] == "diamond_prop_drawdown_guard"
    assert "HALT" in alert["headline"]


def test_WATCH_appends_YELLOW_alert(tmp_path: Path, monkeypatch: object) -> None:
    """WATCH signal writes a YELLOW-severity alert."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    alerts_log = tmp_path / "alerts.jsonl"
    monkeypatch.setattr(dg, "ALERTS_LOG", alerts_log)  # type: ignore[attr-defined]

    receipt = _make_receipt("WATCH")
    dg._fire_alerts(receipt)
    alert = json.loads(alerts_log.read_text(encoding="utf-8").strip())
    assert alert["severity"] == "YELLOW"


def test_OK_does_not_append_alert(tmp_path: Path, monkeypatch: object) -> None:
    """OK signal does NOT spam the alerts log with heartbeats."""
    from eta_engine.scripts import diamond_prop_drawdown_guard as dg

    alerts_log = tmp_path / "alerts.jsonl"
    monkeypatch.setattr(dg, "ALERTS_LOG", alerts_log)  # type: ignore[attr-defined]

    receipt = _make_receipt("OK")
    dg._fire_alerts(receipt)
    assert not alerts_log.exists()


# ────────────────────────────────────────────────────────────────────
# capital_allocator supervisor-facing helpers
# ────────────────────────────────────────────────────────────────────


def test_get_prop_guard_signal_reads_halt_flag(tmp_path: Path, monkeypatch: object) -> None:
    """When the halt flag exists, signal returns HALT."""
    from eta_engine.feeds import capital_allocator as ca

    halt_flag = tmp_path / "halt.flag"
    watch_flag = tmp_path / "watch.flag"
    halt_flag.write_text("active", encoding="utf-8")
    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", watch_flag)  # type: ignore[attr-defined]
    assert ca.get_prop_guard_signal() == "HALT"


def test_get_prop_guard_signal_reads_watch_flag(tmp_path: Path, monkeypatch: object) -> None:
    """When only the watch flag exists, signal returns WATCH."""
    from eta_engine.feeds import capital_allocator as ca

    halt_flag = tmp_path / "halt.flag"
    watch_flag = tmp_path / "watch.flag"
    watch_flag.write_text("active", encoding="utf-8")
    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", watch_flag)  # type: ignore[attr-defined]
    assert ca.get_prop_guard_signal() == "WATCH"


def test_get_prop_guard_signal_OK_when_no_flags(tmp_path: Path, monkeypatch: object) -> None:
    """No flag files = OK signal = supervisor proceeds normally."""
    from eta_engine.feeds import capital_allocator as ca

    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", tmp_path / "missing1.flag")  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", tmp_path / "missing2.flag")  # type: ignore[attr-defined]
    assert ca.get_prop_guard_signal() == "OK"


def test_halt_dominates_watch_when_both_present(tmp_path: Path, monkeypatch: object) -> None:
    """Defensive: if BOTH flags exist (race condition), HALT wins."""
    from eta_engine.feeds import capital_allocator as ca

    halt_flag = tmp_path / "halt.flag"
    watch_flag = tmp_path / "watch.flag"
    halt_flag.write_text("active", encoding="utf-8")
    watch_flag.write_text("active", encoding="utf-8")
    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", watch_flag)  # type: ignore[attr-defined]
    assert ca.get_prop_guard_signal() == "HALT"


def test_should_block_prop_entry_true_for_prop_ready_bot_on_halt(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Supervisor-facing: prop_ready bot + HALT = block."""
    from eta_engine.feeds import capital_allocator as ca

    halt_flag = tmp_path / "halt.flag"
    halt_flag.write_text("active", encoding="utf-8")
    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", tmp_path / "missing.flag")  # type: ignore[attr-defined]
    # Stub load_prop_ready_bots so we don't depend on the live leaderboard
    monkeypatch.setattr(
        ca,
        "load_prop_ready_bots",
        lambda: frozenset({"m2k_sweep_reclaim"}),
    )
    assert ca.should_block_prop_entry("m2k_sweep_reclaim") is True


def test_should_block_prop_entry_false_for_non_prop_ready_bot(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Non-prop-ready bots are not gated by the prop guard."""
    from eta_engine.feeds import capital_allocator as ca

    halt_flag = tmp_path / "halt.flag"
    halt_flag.write_text("active", encoding="utf-8")
    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", tmp_path / "missing.flag")  # type: ignore[attr-defined]
    monkeypatch.setattr(
        ca,
        "load_prop_ready_bots",
        lambda: frozenset({"m2k_sweep_reclaim"}),  # other bot is prop_ready
    )
    assert ca.should_block_prop_entry("cl_macro") is False


def test_prop_entry_size_multiplier_returns_correct_values(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """0.0 on HALT, 0.5 on WATCH, 1.0 on OK — for prop_ready bots."""
    from eta_engine.feeds import capital_allocator as ca

    halt_flag = tmp_path / "halt.flag"
    watch_flag = tmp_path / "watch.flag"
    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", watch_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(
        ca,
        "load_prop_ready_bots",
        lambda: frozenset({"m2k_sweep_reclaim"}),
    )

    # OK case (no flags)
    assert ca.prop_entry_size_multiplier("m2k_sweep_reclaim") == 1.0

    # WATCH case
    watch_flag.write_text("active", encoding="utf-8")
    assert ca.prop_entry_size_multiplier("m2k_sweep_reclaim") == 0.5

    # HALT case (dominates)
    halt_flag.write_text("active", encoding="utf-8")
    assert ca.prop_entry_size_multiplier("m2k_sweep_reclaim") == 0.0


def test_prop_entry_size_multiplier_always_one_for_non_prop_ready(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """Non-prop-ready bots are never resized by the prop guard."""
    from eta_engine.feeds import capital_allocator as ca

    halt_flag = tmp_path / "halt.flag"
    halt_flag.write_text("active", encoding="utf-8")
    monkeypatch.setattr(ca, "PROP_HALT_FLAG_PATH", halt_flag)  # type: ignore[attr-defined]
    monkeypatch.setattr(ca, "PROP_WATCH_FLAG_PATH", tmp_path / "missing.flag")  # type: ignore[attr-defined]
    monkeypatch.setattr(
        ca,
        "load_prop_ready_bots",
        lambda: frozenset({"m2k_sweep_reclaim"}),  # other bot prop_ready
    )
    # cl_macro is not prop_ready -> multiplier always 1.0 regardless of flags
    assert ca.prop_entry_size_multiplier("cl_macro") == 1.0


# ────────────────────────────────────────────────────────────────────
# Wave-23: supervisor integration regex checks
# ────────────────────────────────────────────────────────────────────


def test_supervisor_imports_prop_guard_helpers() -> None:
    """Wave-23: the supervisor's entry hot path imports the three
    prop-guard helpers. Regex-checked so the integration can't
    silently regress during refactors."""
    p = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "jarvis_strategy_supervisor.py"
    )
    text = p.read_text(encoding="utf-8")
    assert "should_block_prop_entry" in text, (
        "supervisor missing should_block_prop_entry — wave-23 not wired"
    )
    assert "prop_entry_size_multiplier" in text, (
        "supervisor missing prop_entry_size_multiplier — wave-23 not wired"
    )


def test_supervisor_blocks_entry_on_HALT_signal() -> None:
    """The supervisor calls should_block_prop_entry and returns early."""
    p = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "jarvis_strategy_supervisor.py"
    )
    text = p.read_text(encoding="utf-8")
    assert "if should_block_prop_entry(bot.bot_id):" in text, (
        "supervisor missing the should_block_prop_entry check"
    )
    assert "prop_guard_halt:" in text, (
        "supervisor missing the prop_guard_halt rejection reason"
    )


def test_supervisor_applies_watch_size_multiplier() -> None:
    """The supervisor multiplies size_mult by prop_entry_size_multiplier
    so WATCH halves the position and HALT zeros it."""
    p = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "jarvis_strategy_supervisor.py"
    )
    text = p.read_text(encoding="utf-8")
    assert "size_mult *= prop_mult" in text, (
        "supervisor missing the WATCH-mode size_mult multiplication"
    )
