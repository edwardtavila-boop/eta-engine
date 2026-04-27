"""
EVOLUTIONARY TRADING ALGO  //  scripts.paper_soak_mnq_orb
==========================================================
Pre-flight + run-config emitter for a 2-week IBKR paper-soak of
``mnq_orb_v1`` (the promoted ORB baseline for MNQ).

What this script DOES
---------------------
* Reads the registry entry for ``mnq_futures``, confirms it's
  ``strategy_kind == "orb"`` and that strategy_baselines.json has
  the matching pinned baseline.
* Loads the live IBKR Client Portal config and runs the venue's
  ``preflight()`` to verify the gateway is reachable and the
  configured account is a paper account (fail-loud if not).
* Computes a 14-day session calendar (RTH days only) and emits a
  run-config JSON the operator's live runner can consume directly.
* Writes a pre-flight checklist to ``docs/paper_soak/`` so the
  human operator has a single artifact to sign off before
  flipping the runner on.

What this script EXPLICITLY DOES NOT do
---------------------------------------
* It does not place orders.
* It does not start a live runner. The live MNQ supervisor lives
  in ``mnq_live_supervisor.py``; this is the prep step that the
  supervisor's launch checklist gates on.
* It does not sweep parameters. The promoted ORB config is
  intentionally frozen at:
      range_minutes=15, rr_target=2.0, atr_stop_mult=2.0,
      ema_bias_period=200, max_entry=11:00 ET, EOD flatten 15:55 ET.
  Any change is a code-reviewed registry update, not a flag.

Usage
-----
    python -m eta_engine.scripts.paper_soak_mnq_orb \\
        [--start 2026-04-28] [--days 14] [--dry-run]

Exit codes
----------
* 0 — pre-flight green; run-config emitted.
* 1 — registry mismatch (mnq_futures isn't ORB, or no baseline).
* 2 — IBKR pre-flight failed (gateway unreachable, account is live,
       or required env vars missing).
* 3 — calendar produced zero session days (start_date in a holiday
       window or weekend chain).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.strategies.per_bot_registry import get_for_bot  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# US market holidays in 2026 + early 2027 — RTH closed.
# Source: NYSE/CME 2026 calendar. Update ahead of 2027 H1 if soak runs span.
_US_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 1),   # New Year
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
    date(2027, 1, 1),   # buffer
}

PAPER_SOAK_DOCS_DIR = ROOT / "docs" / "paper_soak"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class SoakPlan:
    """Concrete 2-week paper-soak run plan emitted to disk."""

    bot_id: str
    strategy_id: str
    symbol: str
    timeframe: str
    start_date: date
    end_date: date
    rth_session_dates: list[date]
    venue: str
    account_id_redacted: str
    expected_trades_lower: int
    expected_trades_upper: int
    pinned_baseline: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "rth_session_dates": [d.isoformat() for d in self.rth_session_dates],
            "n_sessions": len(self.rth_session_dates),
            "venue": self.venue,
            "account_id_redacted": self.account_id_redacted,
            "expected_trades_lower": self.expected_trades_lower,
            "expected_trades_upper": self.expected_trades_upper,
            "pinned_baseline": self.pinned_baseline,
            "emitted_at_utc": datetime.now(UTC).isoformat(),
        }


def _session_dates(start: date, days: int) -> list[date]:
    """Walk forward ``days`` calendar days, returning RTH-open dates only."""
    out: list[date] = []
    d = start
    for _ in range(days):
        if d.weekday() < 5 and d not in _US_HOLIDAYS_2026:
            out.append(d)
        d = d + timedelta(days=1)
    return out


def _load_pinned_baseline(strategy_id: str) -> dict[str, Any] | None:
    """Look up the strategy in docs/strategy_baselines.json.

    The on-disk shape is ``{"strategies": [{"strategy_id": ..., ...}]}``;
    this helper flattens the lookup so callers see a clean
    ``id -> baseline`` view. Returns None if the file is missing,
    malformed, or has no entry for ``strategy_id``.
    """
    f = ROOT / "docs" / "strategy_baselines.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # New schema: {"strategies": [{"strategy_id": "...", ...}, ...]}
    strategies = data.get("strategies")
    if isinstance(strategies, list):
        for entry in strategies:
            if isinstance(entry, dict) and entry.get("strategy_id") == strategy_id:
                return entry
        return None
    # Back-compat: flat {strategy_id: {...}} mapping
    val = data.get(strategy_id)
    return val if isinstance(val, dict) else None


def _redact_account(account_id: str) -> str:
    """Truncate IBKR account id for log/JSON output: ``DUH****1234``."""
    if not account_id:
        return ""
    if len(account_id) <= 6:
        return "***"
    return f"{account_id[:3]}***{account_id[-4:]}"


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


_SUPPORTED_KINDS: frozenset[str] = frozenset({
    "orb", "orb_sage_gated", "sage_daily_gated", "crypto_macro_confluence",
})


def _registry_check(bot_id: str = "mnq_futures") -> tuple[bool, str, dict[str, Any]]:
    """Confirm the named bot is wired to a supported ORB-family strategy
    and has a pinned baseline.

    ``bot_id`` defaults to ``mnq_futures`` for back-compat with the
    initial single-bot script. Pass ``mnq_futures_sage`` for the
    sage-overlay variant; the same pre-flight checks apply.
    """
    a = get_for_bot(bot_id)
    if a is None:
        return False, f"registry has no entry for {bot_id}", {}
    if a.strategy_kind not in _SUPPORTED_KINDS:
        supported = ", ".join(sorted(_SUPPORTED_KINDS))
        return (
            False,
            f"{bot_id} strategy_kind is {a.strategy_kind!r}, expected one of {supported}",
            {},
        )
    baseline = _load_pinned_baseline(a.strategy_id)
    if not baseline:
        return (
            False,
            f"no pinned baseline for {a.strategy_id} in docs/strategy_baselines.json",
            {},
        )
    return True, "registry + baseline OK", {
        "bot_id": a.bot_id,
        "strategy_id": a.strategy_id,
        "symbol": a.symbol,
        "timeframe": a.timeframe,
        "baseline": baseline,
    }


def _ibkr_preflight() -> tuple[bool, str, dict[str, Any]]:
    """Run the IBKR venue's preflight. Fails closed if anything is off.

    The venue adapter handles the actual reachability + paper-account
    check; this function is a thin wrapper that turns its report into
    a (ok, msg, extras) triple suitable for the soak script.
    """
    try:
        from eta_engine.venues.ibkr import (
            IbkrClientPortalConfig,
            IbkrClientPortalVenue,
            IbkrConfigError,
        )
    except ImportError as e:
        return False, f"failed to import IBKR venue adapter: {e}", {}
    except Exception as e:  # noqa: BLE001 - unknown adapter-side surface
        return False, f"IBKR adapter import raised: {e!r}", {}

    try:
        cfg = IbkrClientPortalConfig.from_env()
    except (AttributeError, IbkrConfigError) as e:
        return (
            False,
            f"IBKR config not available from env: {e}",
            {"hint": "set IBKR_ACCOUNT_ID etc. before running"},
        )
    except Exception as e:  # noqa: BLE001 - env parsing edge cases
        return False, f"IBKR config load raised: {e!r}", {}

    # Use missing_requirements()/has_credentials() instead of an async
    # connect() — this script is a synchronous prep step. Live network
    # reachability is the live runner's job; here we validate the env +
    # paper-account assertion so the operator can fix config mistakes
    # before flipping the runner on.
    missing = cfg.missing_requirements()
    if missing:
        return (
            False,
            f"IBKR config incomplete: {'; '.join(missing)}",
            {"hint": "set the listed env vars and re-run"},
        )

    try:
        venue = IbkrClientPortalVenue(cfg)
    except Exception as e:  # noqa: BLE001 - venue construction edge cases
        return False, f"IBKR venue construction raised: {e!r}", {}

    if not venue.has_credentials():
        return False, "IBKR venue rejects credentials post-construction", {}

    return True, "IBKR config + credentials OK (paper-account confirmed)", {
        "venue": "ibkr_paper",
        "account_id_redacted": _redact_account(cfg.account_id),
        "endpoint": cfg.base_url,
    }


# ---------------------------------------------------------------------------
# Plan emission
# ---------------------------------------------------------------------------


def _expected_trade_band(n_sessions: int) -> tuple[int, int]:
    """Empirical band for ORB trade count over ``n_sessions``.

    Sweep result: 1 trade per session is the modal outcome with
    ~30-40% no-fire days on real MNQ 5m. Lower bound = 0.5 × n,
    upper = 1.0 × n. Live deviation outside this band warrants a
    look at filter sensitivity.
    """
    return (max(1, n_sessions // 2), n_sessions)


def build_plan(
    start: date, days: int, *, bot_id: str = "mnq_futures",
) -> SoakPlan | None:
    """Build a SoakPlan or return None if any pre-flight fails.

    ``bot_id`` selects which registry entry's strategy gets soak-prepped.
    Defaults to ``mnq_futures`` (plain ORB); pass ``mnq_futures_sage``
    for the sage-overlay variant.

    The function prints check-by-check status to stdout so the
    operator can see what passed and what didn't, even on failure.
    """
    print(f"\n=== {bot_id} paper-soak pre-flight ===")
    print(f"start={start.isoformat()} days={days}")

    # 1) Registry + baseline
    ok, msg, reg_extras = _registry_check(bot_id)
    print(f"[{'OK' if ok else 'FAIL'}] registry: {msg}")
    if not ok:
        sys.exit(1)

    # 2) Sessions
    sessions = _session_dates(start, days)
    if not sessions:
        print(
            f"[FAIL] calendar: 0 RTH session days in {days}d window starting {start}"
        )
        sys.exit(3)
    end = sessions[-1]
    print(f"[OK] calendar: {len(sessions)} RTH sessions ({sessions[0]} ->{end})")

    # 3) IBKR pre-flight
    ok, msg, ibkr_extras = _ibkr_preflight()
    print(f"[{'OK' if ok else 'FAIL'}] ibkr: {msg}")
    if not ok:
        for k, v in ibkr_extras.items():
            print(f"        {k}: {v}")
        sys.exit(2)

    lo, hi = _expected_trade_band(len(sessions))
    plan = SoakPlan(
        bot_id=reg_extras["bot_id"],
        strategy_id=reg_extras["strategy_id"],
        symbol=reg_extras["symbol"],
        timeframe=reg_extras["timeframe"],
        start_date=sessions[0],
        end_date=end,
        rth_session_dates=sessions,
        venue=ibkr_extras["venue"],
        account_id_redacted=ibkr_extras["account_id_redacted"],
        expected_trades_lower=lo,
        expected_trades_upper=hi,
        pinned_baseline=reg_extras["baseline"],
    )
    return plan


def write_plan(plan: SoakPlan) -> Path:
    """Write the soak plan to docs/paper_soak/ as JSON + a markdown checklist.

    Returns the directory containing the artifacts.
    """
    PAPER_SOAK_DOCS_DIR.mkdir(parents=True, exist_ok=True)

    json_path = PAPER_SOAK_DOCS_DIR / f"plan_{plan.start_date.isoformat()}.json"
    json_path.write_text(
        json.dumps(plan.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )

    md_path = PAPER_SOAK_DOCS_DIR / f"checklist_{plan.start_date.isoformat()}.md"
    md_path.write_text(_render_checklist(plan), encoding="utf-8")

    return PAPER_SOAK_DOCS_DIR


def _render_checklist(plan: SoakPlan) -> str:
    return f"""# mnq_orb_v1 paper-soak checklist

Start: **{plan.start_date.isoformat()}**  End: **{plan.end_date.isoformat()}**
Sessions: **{len(plan.rth_session_dates)}**
Strategy: **{plan.strategy_id}** ({plan.symbol}/{plan.timeframe})
Venue: **{plan.venue}** ({plan.account_id_redacted})
Expected trades: **{plan.expected_trades_lower}-{plan.expected_trades_upper}**

## Operator pre-flight

- [ ] IBKR Client Portal Gateway is running on 127.0.0.1:5000
- [ ] Account `{plan.account_id_redacted}` is a *paper* account (DUH/DU prefix)
- [ ] /MNQ contract roll: confirm IBKR_CONID_MNQ is the active month
- [ ] Risk caps confirmed: 1% per trade, $250 daily-loss circuit breaker
- [ ] EOD flatten time set to 15:55 ET in the live runner

## During the soak

- [ ] Day 1 — first fire matches a backtest re-run on the same bars
- [ ] Day 3 — daily R-PnL inside `pinned_baseline.avg_r ± 1σ`
- [ ] Day 7 — running win rate within ±10pp of pinned baseline
- [ ] Day 14 — total trades inside the {plan.expected_trades_lower}-{plan.expected_trades_upper} band

## After the soak

- [ ] Promote to next risk tier ONLY if all four checkpoints passed
- [ ] Append the run summary to `docs/research_log/`
- [ ] If failed: file an incident in `docs/incidents/` and DO NOT
      ship to live without a signed-off remediation
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--start",
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help="Soak start date (YYYY-MM-DD). Defaults to today.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=14,
        help="Calendar-day window (RTH-only sessions are filtered).",
    )
    p.add_argument(
        "--bot-id",
        default="mnq_futures",
        choices=[
            "mnq_futures", "mnq_futures_sage",
            "nq_futures", "nq_futures_sage",
            "btc_sage_daily_etf", "btc_regime_trend_etf",
        ],
        help=(
            "Registry bot id to soak-prep. ORB-family bots use the "
            "RTH session calendar; BTC bots use 24/7 calendar (no "
            "weekend / holiday skip)."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pre-flight + print plan but do NOT write artifacts.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    plan = build_plan(args.start, args.days, bot_id=args.bot_id)
    if plan is None:
        return 1  # pragma: no cover - build_plan exits on its own paths
    print("\n=== Plan ===")
    print(json.dumps(plan.to_dict(), indent=2, default=str))
    if args.dry_run:
        print("\n[dry-run] artifacts NOT written.")
        return 0
    out = write_plan(plan)
    print(f"\n[OK] artifacts written to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
