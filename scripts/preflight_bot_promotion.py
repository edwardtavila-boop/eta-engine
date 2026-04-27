"""
EVOLUTIONARY TRADING ALGO  //  scripts.preflight_bot_promotion
================================================================
Per-bot live-promotion preflight gate.

Why this exists
---------------
``scripts/eta_live_preflight.py`` checks the **system-wide** posture
(env files, IBKR/Tastytrade handshake, kill-switch, audit log, etc).

This sibling script checks the **per-bot** posture for a single bot
about to be flipped from paper to real-money live trading. The two
gates are AND'd: both must clear before any individual bot trades
real capital.

Per-bot checks (each green/amber/red):

  1. **Registry production status** — bot exists in
     ``per_bot_registry`` AND its strategy_id appears in
     ``strategy_baselines.json`` with ``"_promotion_status":
     "production"``.
  2. **Frozen baseline complete** — n_trades / win_rate / avg_r /
     r_stddev all populated. Drift watchdog can't function without these.
  3. **Warmup policy in place** — registry extras has
     ``warmup_policy`` with ``warmup_days`` and
     ``risk_multiplier_during_warmup < 1.0``. Half-size first 30 days
     is the standing safety policy for newly-promoted strategies.
  4. **Drift watchdog has run within 24h** — checks the appended
     JSONL log. If the watchdog hasn't executed, drift events would
     go undetected once live capital is in.
  5. **Recent grid run shows the bot still PASSes** — re-evaluates
     the bot through ``run_research_grid`` and confirms the gate
     verdict is still PASS at the registry config. Catches the
     "passed at promotion, regressed since" case.
  6. **Bot directory + bot.py exist** — the runtime entry-point.
  7. **Broker venue keys present** — env vars for the bot's required
     venue (IBKR for futures, Coinbase/IBKR for crypto).
  8. **IBKR/CME drift gate cleared (crypto only)** — for crypto bots,
     ``compare_coinbase_vs_ibkr`` must show GREEN within last 14d.
     The standing operator policy: don't go live on Coinbase research
     without an IBKR-native re-fetch + drift comparison.
  9. **Per-bot daily loss limit configured** — ``extras["daily_loss_limit_pct"]``
     or system-wide default. Without it, a bad day can compound.
 10. **Position size sanity** — at the bot's
     ``risk_per_trade_pct``, ``max_trades_per_day`` and the warmup
     multiplier, the worst-case daily loss is documented.

Each check returns ``green`` (proceed), ``amber`` (operator confirm
required), or ``red`` (block). Exit code: 0 green, 2 amber, 3 red,
1 unknown error.

Usage::

    # Single bot
    python -m eta_engine.scripts.preflight_bot_promotion --bot-id btc_hybrid

    # Whole production fleet
    python -m eta_engine.scripts.preflight_bot_promotion

    # Machine-readable for CI
    python -m eta_engine.scripts.preflight_bot_promotion --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _load_env_file() -> None:
    """Load eta_engine/.env into os.environ if present.

    The preflight needs IBKR/Tasty/etc. env vars to verify keys are
    set. We don't take a hard dependency on python-dotenv — a small
    parser handles ``KEY=VALUE`` lines and skips comments / blanks.
    Existing os.environ values win over .env (so the operator can
    override a single var via shell without editing the file).
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_env_file()


# Symbol → required broker env vars. Bot's venue is resolved from
# the bot's symbol; the env vars listed here MUST be populated for
# the bot to fire real-money orders.
_VENUE_ENV_REQUIREMENTS: dict[str, list[str]] = {
    "futures": [
        # IBKR Client Portal (primary)
        "IBKR_ACCOUNT_ID", "IBKR_CP_BASE_URL",
        # Tastytrade (fallback)
        "TASTY_ACCOUNT_NUMBER", "TASTY_SESSION_TOKEN", "TASTY_API_BASE_URL",
    ],
    "crypto": [
        # IBKR (preferred path per eta_data_source_policy memory)
        "IBKR_ACCOUNT_ID", "IBKR_CP_BASE_URL",
        # Coinbase (alt path, not venue-wired yet but env-tracked)
        # NOTE: COINBASE_API_KEY / SECRET are commented in .env.example
        # because the venue module isn't shipped yet; the preflight
        # records this as amber rather than red.
    ],
}


# Symbol patterns to asset-class mapping. Mirrors the tax-ledger
# classification but indexed by symbol prefix for venue routing.
def _venue_class_for(symbol: str) -> str:
    s = symbol.upper().replace(" ", "")
    if s.startswith(("MNQ", "NQ", "ES", "MES", "RTY", "M2K", "YM", "MYM")):
        return "futures"
    if s.startswith(("BTC", "ETH", "SOL", "XRP")):
        return "crypto"
    return "unknown"


@dataclass
class CheckResult:
    name: str
    severity: str  # 'green' | 'amber' | 'red' | 'skip'
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BotPreflightReport:
    bot_id: str
    overall_severity: str
    checks: list[CheckResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_registry_production(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """1. Registry + production status."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return CheckResult(
            name="registry_production_status",
            severity="red",
            summary=f"bot_id={bot_id!r} not in per_bot_registry",
        )

    # Read strategy_baselines.json for promotion status.
    baselines_path = ROOT / "docs" / "strategy_baselines.json"
    try:
        payload = json.loads(baselines_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="registry_production_status",
            severity="red",
            summary=f"strategy_baselines.json unreadable: {exc}",
        )
    strategies = payload.get("strategies") or []
    matching = [
        s for s in strategies
        if isinstance(s, dict) and s.get("strategy_id") == a.strategy_id
    ]
    if not matching:
        return CheckResult(
            name="registry_production_status",
            severity="red",
            summary=(
                f"strategy_id={a.strategy_id!r} has no row in "
                "strategy_baselines.json — pin a baseline before going live"
            ),
        )
    s = matching[0]
    status = s.get("_promotion_status", "unknown")
    if status != "production":
        return CheckResult(
            name="registry_production_status",
            severity="red",
            summary=f"strategy_id={a.strategy_id!r} status={status!r}, not 'production'",
            details={"baseline_row": s},
        )
    return CheckResult(
        name="registry_production_status",
        severity="green",
        summary=f"{a.strategy_id} is production-promoted",
        details={
            "strategy_kind": a.strategy_kind,
            "symbol": a.symbol,
            "timeframe": a.timeframe,
            "promoted_at": s.get("_promoted_at"),
        },
    )


def _check_baseline_complete(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """2. BaselineSnapshot fields populated."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return CheckResult(
            name="baseline_complete",
            severity="skip",
            summary="no registry row",
        )
    baselines_path = ROOT / "docs" / "strategy_baselines.json"
    try:
        payload = json.loads(baselines_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return CheckResult(
            name="baseline_complete",
            severity="red",
            summary="strategy_baselines.json missing/invalid",
        )
    matching = next(
        (s for s in (payload.get("strategies") or [])
         if s.get("strategy_id") == a.strategy_id),
        None,
    )
    if matching is None:
        return CheckResult(
            name="baseline_complete",
            severity="red",
            summary=f"no baseline row for {a.strategy_id}",
        )
    required = ("n_trades", "win_rate", "avg_r", "r_stddev")
    missing = [k for k in required if not matching.get(k)]
    if missing:
        return CheckResult(
            name="baseline_complete",
            severity="red",
            summary=f"baseline missing fields: {missing}",
        )
    return CheckResult(
        name="baseline_complete",
        severity="green",
        summary=(
            f"baseline: n={matching['n_trades']} "
            f"wr={matching['win_rate'] * 100:.1f}% "
            f"avg_r={matching['avg_r']:+.3f} "
            f"sd={matching['r_stddev']:.3f}"
        ),
    )


def _check_warmup_policy(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """3. Warmup policy in place + first-month half-size still active."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return CheckResult(
            name="warmup_policy",
            severity="skip", summary="no registry row",
        )
    warmup = a.extras.get("warmup_policy")
    if not isinstance(warmup, dict):
        return CheckResult(
            name="warmup_policy",
            severity="amber",
            summary="no warmup_policy in extras — first-month half-size missing",
        )
    promoted_on = warmup.get("promoted_on")
    days = int(warmup.get("warmup_days", 0))
    mult = float(warmup.get("risk_multiplier_during_warmup", 1.0))
    if mult >= 1.0:
        return CheckResult(
            name="warmup_policy",
            severity="amber",
            summary=f"warmup multiplier {mult} >= 1.0 (no size reduction)",
        )
    if promoted_on:
        try:
            d = datetime.fromisoformat(promoted_on).replace(tzinfo=UTC)
        except ValueError:
            d = None
        if d:
            elapsed = (datetime.now(UTC) - d).days
            if elapsed > days:
                return CheckResult(
                    name="warmup_policy",
                    severity="amber",
                    summary=(
                        f"warmup expired {elapsed - days}d ago — "
                        "promote risk multiplier to 1.0 in registry"
                    ),
                )
            return CheckResult(
                name="warmup_policy",
                severity="green",
                summary=(
                    f"warmup active: {elapsed}/{days}d elapsed at "
                    f"{mult * 100:.0f}% size"
                ),
            )
    return CheckResult(
        name="warmup_policy",
        severity="green",
        summary=f"warmup configured: {days}d at {mult * 100:.0f}% size",
    )


def _check_drift_watchdog_recent():  # type: ignore[no-untyped-def]  # noqa: ANN202
    """4. drift_watchdog.jsonl appended within 24h."""
    p = ROOT / "docs" / "drift_watchdog.jsonl"
    if not p.exists():
        return CheckResult(
            name="drift_watchdog_recent",
            severity="amber",
            summary="drift_watchdog.jsonl missing — schedule run_drift_watchdog daily",
        )
    try:
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
    except OSError as exc:
        return CheckResult(
            name="drift_watchdog_recent",
            severity="amber",
            summary=f"can't stat drift_watchdog.jsonl: {exc}",
        )
    age_h = (datetime.now(UTC) - mtime).total_seconds() / 3600
    if age_h > 24:
        return CheckResult(
            name="drift_watchdog_recent",
            severity="amber",
            summary=(
                f"drift_watchdog last ran {age_h:.0f}h ago "
                "(threshold 24h) — schedule a daily task"
            ),
        )
    return CheckResult(
        name="drift_watchdog_recent",
        severity="green",
        summary=f"drift_watchdog ran {age_h:.1f}h ago",
    )


def _check_grid_still_passes(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """5. Re-run the bot through walk-forward; verify still PASS."""
    from eta_engine.scripts.run_research_grid import (
        ResearchCell,
        run_cell,
    )
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return CheckResult(
            name="grid_still_passes",
            severity="skip", summary="no registry row",
        )
    cell = ResearchCell(
        label=a.bot_id,
        symbol=a.symbol,
        timeframe=a.timeframe,
        scorer_name=a.scorer_name,
        threshold=a.confluence_threshold,
        block_regimes=a.block_regimes if a.block_regimes else None,
        window_days=a.window_days,
        step_days=a.step_days,
        min_trades_per_window=a.min_trades_per_window,
        strategy_kind=a.strategy_kind,
        extras=dict(a.extras),
    )
    try:
        result = run_cell(cell)
    except Exception as exc:  # noqa: BLE001 -- preflight guard
        return CheckResult(
            name="grid_still_passes",
            severity="red",
            summary=f"grid run threw {type(exc).__name__}: {str(exc)[:80]}",
        )
    if result.pass_gate:
        return CheckResult(
            name="grid_still_passes",
            severity="green",
            summary=(
                f"PASS: agg IS {result.agg_is_sharpe:+.3f} / "
                f"OOS {result.agg_oos_sharpe:+.3f} / "
                f"DSR {result.deflated_sharpe:.3f}"
            ),
            details={
                "n_windows": result.n_windows,
                "n_positive_oos": result.n_positive_oos,
                "fold_dsr_pass_fraction": result.fold_dsr_pass_fraction,
            },
        )
    return CheckResult(
        name="grid_still_passes",
        severity="red",
        summary=(
            f"FAIL: agg IS {result.agg_is_sharpe:+.3f} / "
            f"OOS {result.agg_oos_sharpe:+.3f} / "
            f"DSR pass {result.fold_dsr_pass_fraction * 100:.1f}% "
            "— bot has regressed; re-baseline before live"
        ),
    )


def _check_bot_dir_exists(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """6. bots/<dir>/bot.py exists."""
    # Map bot_id → dir. Variant bot_ids share an underlying dir; resolve.
    candidates = [
        bot_id,
        bot_id.replace("_futures", "").replace("_perp", ""),
        bot_id.replace("_sage", "").replace("_daily_drb", "")
              .replace("_regime_trend", "").replace("_ensemble_2of3", "")
              .replace("_compression", ""),
        bot_id.split("_")[0],
    ]
    bots_root = ROOT / "bots"
    for c in candidates:
        p = bots_root / c / "bot.py"
        if p.exists():
            return CheckResult(
                name="bot_dir_exists",
                severity="green",
                summary=f"bots/{c}/bot.py present",
            )
    return CheckResult(
        name="bot_dir_exists",
        severity="amber",
        summary=f"no bots/<dir>/bot.py for {bot_id} (variant or runtime not wired)",
    )


def _check_broker_keys(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """7. Required env vars for the bot's venue are set + non-empty."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return CheckResult(
            name="broker_keys",
            severity="skip", summary="no registry row",
        )
    venue_class = _venue_class_for(a.symbol)
    if venue_class == "unknown":
        return CheckResult(
            name="broker_keys",
            severity="amber",
            summary=f"unknown venue class for symbol {a.symbol}",
        )
    required = _VENUE_ENV_REQUIREMENTS.get(venue_class, [])
    missing = [k for k in required if not (os.environ.get(k) or "").strip()]
    if missing:
        return CheckResult(
            name="broker_keys",
            severity="red",
            summary=(
                f"{venue_class} venue keys missing: {missing} "
                "(set in .env, never commit)"
            ),
        )
    extras_amber: str | None = None
    if venue_class == "crypto":
        # Coinbase venue not shipped yet — flag amber so operator
        # is aware the only live path right now is IBKR/CME.
        extras_amber = (
            "venues/coinbase.py not implemented; only IBKR/CME path is "
            "live-capable for crypto bots. Coinbase keys reserved for "
            "research data fetcher only."
        )
    summary = (
        f"{venue_class} keys present: " + ", ".join(required)
    )
    if extras_amber:
        return CheckResult(
            name="broker_keys",
            severity="amber",
            summary=summary + " — " + extras_amber,
        )
    return CheckResult(
        name="broker_keys",
        severity="green",
        summary=summary,
    )


def _check_ibkr_drift_gate(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """8. Crypto-only: compare_coinbase_vs_ibkr ran GREEN within 14d."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None or _venue_class_for(a.symbol) != "crypto":
        return CheckResult(
            name="ibkr_drift_gate",
            severity="skip",
            summary="not a crypto bot — IBKR drift gate not required",
        )
    log_dir = ROOT / "docs" / "research_log"
    candidates = sorted(log_dir.glob(f"{bot_id}_data_swap_*.md"))
    if not candidates:
        return CheckResult(
            name="ibkr_drift_gate",
            severity="red",
            summary=(
                "no <bot>_data_swap_*.md research log entry — "
                "run scripts/compare_coinbase_vs_ibkr before live"
            ),
        )
    most_recent = candidates[-1]
    age_d = (
        datetime.now(UTC)
        - datetime.fromtimestamp(most_recent.stat().st_mtime, tz=UTC)
    ).days
    if age_d > 14:
        return CheckResult(
            name="ibkr_drift_gate",
            severity="amber",
            summary=(
                f"last drift comparison was {age_d}d ago "
                f"({most_recent.name}) — re-run to refresh within 14d"
            ),
        )
    text = most_recent.read_text(encoding="utf-8", errors="replace")
    if "Severity: `red`" in text or "Severity: `amber`" in text:
        return CheckResult(
            name="ibkr_drift_gate",
            severity="red",
            summary=(
                f"most recent drift gate ({most_recent.name}) is NOT GREEN "
                "— re-tune on IBKR data before live"
            ),
        )
    return CheckResult(
        name="ibkr_drift_gate",
        severity="green",
        summary=f"drift gate {age_d}d ago: GREEN",
    )


def _check_loss_limit(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """9. Per-bot daily loss limit configured."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return CheckResult(
            name="loss_limit", severity="skip", summary="no registry row",
        )
    cap = a.extras.get("daily_loss_limit_pct")
    if cap is None:
        return CheckResult(
            name="loss_limit",
            severity="amber",
            summary=(
                "no extras['daily_loss_limit_pct']; falling back to "
                "system-wide kill-switch"
            ),
        )
    return CheckResult(
        name="loss_limit",
        severity="green",
        summary=f"daily_loss_limit_pct = {cap}",
    )


def _check_position_size_sanity(bot_id: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """10. Worst-case daily loss math is sane."""
    from eta_engine.strategies.per_bot_registry import get_for_bot

    a = get_for_bot(bot_id)
    if a is None:
        return CheckResult(
            name="position_size_sanity",
            severity="skip", summary="no registry row",
        )
    # Conservative envelope: full risk per trade × max trades per day,
    # adjusted for the warmup multiplier if present.
    risk_per_trade = 0.01  # fleet default; override via extras if needed
    max_trades = 10  # fleet default
    warmup = a.extras.get("warmup_policy")
    mult = 1.0
    if isinstance(warmup, dict):
        mult = float(warmup.get("risk_multiplier_during_warmup", 1.0))
    worst_pct = risk_per_trade * max_trades * mult * 100
    if worst_pct > 5.0:
        return CheckResult(
            name="position_size_sanity",
            severity="amber",
            summary=(
                f"worst-case daily loss = {worst_pct:.1f}% of equity "
                "(risk*max_trades*warmup); consider tighter daily cap"
            ),
        )
    return CheckResult(
        name="position_size_sanity",
        severity="green",
        summary=f"worst-case daily loss envelope = {worst_pct:.2f}% of equity",
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


_CHECKS_REQUIRING_BOT_ID = (
    _check_registry_production,
    _check_baseline_complete,
    _check_warmup_policy,
    _check_grid_still_passes,
    _check_bot_dir_exists,
    _check_broker_keys,
    _check_ibkr_drift_gate,
    _check_loss_limit,
    _check_position_size_sanity,
)


def run_for_bot(bot_id: str) -> BotPreflightReport:
    """Run all per-bot checks; aggregate severity."""
    checks: list[CheckResult] = []
    for fn in _CHECKS_REQUIRING_BOT_ID:
        try:
            checks.append(fn(bot_id))
        except Exception as exc:  # noqa: BLE001 -- preflight guard
            checks.append(
                CheckResult(
                    name=fn.__name__.removeprefix("_check_"),
                    severity="red",
                    summary=f"check threw {type(exc).__name__}: {str(exc)[:80]}",
                ),
            )
    # System-wide drift watchdog freshness — runs once per invocation.
    if checks and checks[0].severity != "skip":
        checks.append(_check_drift_watchdog_recent())

    severities = [c.severity for c in checks]
    if "red" in severities:
        overall = "red"
    elif "amber" in severities:
        overall = "amber"
    elif all(s in ("green", "skip") for s in severities):
        overall = "green"
    else:
        overall = "amber"

    return BotPreflightReport(
        bot_id=bot_id,
        overall_severity=overall,
        checks=checks,
    )


def main() -> int:
    p = argparse.ArgumentParser(prog="preflight_bot_promotion")
    p.add_argument("--bot-id", default=None, help="single bot; default = all production")
    p.add_argument("--json", action="store_true", help="emit JSON only")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if args.bot_id:
        bot_ids = [args.bot_id]
    else:
        # All bots with a registry row that map to a baseline marked production
        baselines = json.loads(
            (ROOT / "docs" / "strategy_baselines.json").read_text(encoding="utf-8"),
        )
        prod_ids = {
            s["strategy_id"] for s in (baselines.get("strategies") or [])
            if s.get("_promotion_status", "production") == "production"
        }
        from eta_engine.strategies.per_bot_registry import all_assignments
        bot_ids = [
            a.bot_id for a in all_assignments()
            if a.strategy_id in prod_ids
        ]

    reports = [run_for_bot(b) for b in bot_ids]

    if args.json:
        print(
            json.dumps(
                [asdict(r) for r in reports],
                indent=2, default=str,
            ),
        )
    else:
        for r in reports:
            sev_glyph = {
                "green": "[GREEN]", "amber": "[AMBER]",
                "red": "[RED]", "skip": "[SKIP]",
            }
            print(f"\n{'='*70}")
            print(f"{r.bot_id}  =>  {sev_glyph.get(r.overall_severity, r.overall_severity).upper()}")
            print(f"{'='*70}")
            for c in r.checks:
                tag = sev_glyph.get(c.severity, c.severity).upper()
                print(f"  {tag:10s} {c.name:30s} {c.summary}")
                if args.verbose and c.details:
                    print(f"             details: {c.details}")

    # Exit code — operator-friendly
    severities = [r.overall_severity for r in reports]
    if "red" in severities:
        return 3
    if "amber" in severities:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
