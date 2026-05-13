"""
JARVIS v3 // anomaly_watcher — proactive noteworthy-event detector.

Despite the legacy "anomaly" name, this module detects ALL operator-
relevant fleet patterns — negative AND positive — so the Telegram
channel doubles as both a problem alerter and a success ticker.

Negative patterns (severity warn/critical):
  * loss_streak       — same bot lost 3+ consecutive trades
  * loss_rate         — same bot lost 5+ of last 8 trades
  * fleet_drawdown    — fleet total today ≤ -3R
  * stale_bot         — bot has not traded in 48h (silent failure)
  * suspicious_win    — single trade R ≥ +5R (possible fill-model bug
                        or backtest leak; ALWAYS investigate the win,
                        not just the loss)

Positive patterns (severity info — celebrate too):
  * win_streak        — same bot won 5+ consecutive trades
  * fleet_hot_day     — fleet total today ≥ +3R

Together these answer the operator's full feedback:
  "telegram only spams me with watchdog autohealed instead of real or
  useful info like pnl and status or trades or success etc"

Negative anomalies trigger investigation skills; positive events serve
as morale + sanity-check signal. The two share the same dedup, log,
Telegram-pulse, and MCP-tool infrastructure.

When a pattern fires, the watcher:
  1. Returns a ``AnomalyHit`` summary
  2. Logs it to ``var/anomaly_watcher.jsonl``
  3. Returns the suggested skill to activate (anomaly_investigator,
     drawdown_response, win_streak_review, etc.)

The watcher does NOT directly send Telegram messages — that's the
cron task's job. The watcher is a pure detector; alerting is delivery
concern handled separately. This keeps it testable.

Dedup
-----

Each event has a stable ``key`` (e.g. ``loss_streak:bot_a:5`` or
``fleet_hot_day:2026-05-12``). The watcher checks the log for prior
hits with the same key in the last ``DEDUP_HOURS`` window — if found,
suppresses the new hit. Operator gets one Telegram per event, not 96
per day.

Public interface
----------------

* ``scan()`` → list[AnomalyHit] of NEW (post-dedup) events
* ``recent_hits(since_hours=24)`` → list[dict] for operator review
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.anomaly_watcher")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
_LEGACY_STATE_ROOT = _WORKSPACE / "eta_engine" / "state"
DEFAULT_TRADE_CLOSES_PATH = _STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
LEGACY_TRADE_CLOSES_PATH = _LEGACY_STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
DEFAULT_HITS_LOG = _WORKSPACE / "var" / "anomaly_watcher.jsonl"

LOSS_STREAK_THRESHOLD = 3  # 3 losses in a row = anomaly
LOSS_RATE_WINDOW = 8  # look-back window for loss-rate check
LOSS_RATE_THRESHOLD = 5  # 5 of last 8 are losses → anomaly
WIN_STREAK_THRESHOLD = 5  # 5 wins in a row = noteworthy (celebrate)
SUSPICIOUS_WIN_R = 5.0  # single trade R >= 5 → suspicious, could be a bug
FLEET_HOT_DAY_R = 3.0  # today's fleet total >= +3R → hot day
FLEET_DRAWDOWN_R = -3.0  # today's fleet total <= -3R → drawdown response
STALE_BOT_HOURS = 48  # bot silent for 48h+ → stale_bot
SCAN_LOOKBACK_HOURS = 24  # how far back the watcher looks each scan
STALE_BOT_LOOKBACK_HOURS = 168  # 7d window for stale-bot detection
DEDUP_HOURS = 4  # don't re-fire same anomaly within this window

EXPECTED_HOOKS = ("scan", "recent_hits")


@dataclass(frozen=True)
class AnomalyHit:
    """One anomaly detected by the watcher."""

    asof: str
    pattern: str  # "loss_streak" | "loss_rate" | "drawdown" | ...
    key: str  # stable dedup key (e.g. "loss_streak:bot_a:5")
    bot_id: str
    severity: str  # "info" | "warn" | "critical"
    detail: str
    suggested_skill: str
    extras: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _parse_iso(s: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_jsonl(path: Path, since_dt: datetime | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_dt is not None:
                    ts = _parse_iso(rec.get("ts") or rec.get("closed_at"))
                    if ts is None or ts < since_dt:
                        continue
                out.append(rec)
    except OSError as exc:
        logger.warning("anomaly_watcher._read_jsonl failed: %s", exc)
    return out


def _read_trades(
    override_path: Path | None,
    since_dt: datetime | None,
) -> list[dict[str, Any]]:
    """Read trade closes from canonical + legacy paths, deduped."""
    if override_path is not None:
        return _read_jsonl(override_path, since_dt)
    primary = _read_jsonl(DEFAULT_TRADE_CLOSES_PATH, since_dt)
    legacy = _read_jsonl(LEGACY_TRADE_CLOSES_PATH, since_dt)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for src in (primary, legacy):
        for r in src:
            k = "|".join(
                [
                    str(r.get("signal_id") or ""),
                    str(r.get("bot_id") or ""),
                    str(r.get("ts") or r.get("closed_at") or ""),
                    str(r.get("realized_r") or ""),
                ]
            )
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
    return out


def _extract_r(rec: dict[str, Any]) -> float | None:
    raw = rec.get("realized_r")
    if raw is None:
        raw = rec.get("r", rec.get("r_value"))
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------


def _recent_keys(
    hits_log: Path = DEFAULT_HITS_LOG,
    within_hours: float = DEDUP_HOURS,
) -> set[str]:
    """Return the set of anomaly keys logged within the dedup window."""
    if not hits_log.exists():
        return set()
    cutoff = datetime.now(UTC) - timedelta(hours=within_hours)
    keys: set[str] = set()
    try:
        with hits_log.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_iso(rec.get("asof"))
                if ts is None or ts < cutoff:
                    continue
                k = rec.get("key")
                if k:
                    keys.add(str(k))
    except OSError as exc:
        logger.warning("anomaly_watcher._recent_keys read failed: %s", exc)
    return keys


def _append_hit(hit: AnomalyHit, hits_log: Path = DEFAULT_HITS_LOG) -> None:
    """Append the hit to the JSONL log. Never raises."""
    try:
        hits_log.parent.mkdir(parents=True, exist_ok=True)
        with hits_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(hit.to_dict(), default=str) + "\n")
    except OSError as exc:
        logger.warning("anomaly_watcher: hit log append failed: %s", exc)


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------


def _detect_loss_streak(by_bot: dict[str, list[tuple[float, str]]]) -> list[AnomalyHit]:
    """Detect bots with ≥ LOSS_STREAK_THRESHOLD consecutive losses (most recent)."""
    hits: list[AnomalyHit] = []
    for bot_id, trades in by_bot.items():
        # Walk from newest backward, count consecutive losses
        streak = 0
        for r, _ts in reversed(trades):
            if r < 0:
                streak += 1
            else:
                break
        if streak >= LOSS_STREAK_THRESHOLD:
            hits.append(
                AnomalyHit(
                    asof=datetime.now(UTC).isoformat(),
                    pattern="loss_streak",
                    key=f"loss_streak:{bot_id}:{streak}",
                    bot_id=bot_id,
                    severity="warn" if streak < 5 else "critical",
                    detail=f"{bot_id} has {streak} consecutive losses",
                    suggested_skill="jarvis-anomaly-investigator",
                    extras={"streak": streak, "last_n_trades": [{"r": r, "ts": t} for r, t in trades[-streak:]]},
                )
            )
    return hits


def _detect_loss_rate(by_bot: dict[str, list[tuple[float, str]]]) -> list[AnomalyHit]:
    """≥ LOSS_RATE_THRESHOLD losses in the last LOSS_RATE_WINDOW trades."""
    hits: list[AnomalyHit] = []
    for bot_id, trades in by_bot.items():
        if len(trades) < LOSS_RATE_WINDOW:
            continue
        recent = trades[-LOSS_RATE_WINDOW:]
        losses = sum(1 for r, _ in recent if r < 0)
        if losses >= LOSS_RATE_THRESHOLD:
            total_r = sum(r for r, _ in recent)
            hits.append(
                AnomalyHit(
                    asof=datetime.now(UTC).isoformat(),
                    pattern="loss_rate",
                    key=f"loss_rate:{bot_id}:{losses}of{LOSS_RATE_WINDOW}",
                    bot_id=bot_id,
                    severity="warn" if losses < 7 else "critical",
                    detail=(f"{bot_id} has {losses}/{LOSS_RATE_WINDOW} losses (total R={total_r:+.2f})"),
                    suggested_skill="jarvis-anomaly-investigator",
                    extras={
                        "losses_in_window": losses,
                        "window": LOSS_RATE_WINDOW,
                        "total_r_window": round(total_r, 4),
                    },
                )
            )
    return hits


def _detect_win_streak(by_bot: dict[str, list[tuple[float, str]]]) -> list[AnomalyHit]:
    """≥ WIN_STREAK_THRESHOLD consecutive wins (severity=info, celebrate)."""
    hits: list[AnomalyHit] = []
    for bot_id, trades in by_bot.items():
        streak = 0
        total_r = 0.0
        for r, _ts in reversed(trades):
            if r > 0:
                streak += 1
                total_r += r
            else:
                break
        if streak >= WIN_STREAK_THRESHOLD:
            hits.append(
                AnomalyHit(
                    asof=datetime.now(UTC).isoformat(),
                    pattern="win_streak",
                    key=f"win_streak:{bot_id}:{streak}",
                    bot_id=bot_id,
                    severity="info",
                    detail=f"{bot_id} has {streak} consecutive wins (total R={total_r:+.2f})",
                    suggested_skill="jarvis-anomaly-investigator",  # also worth a look:
                    # could be regime-fit OR a fill-model gift, both worth confirming
                    extras={
                        "streak": streak,
                        "total_r": round(total_r, 4),
                        "last_n_trades": [{"r": r, "ts": t} for r, t in trades[-streak:]],
                    },
                )
            )
    return hits


def _detect_fleet_total(
    by_bot: dict[str, list[tuple[float, str]]],
    *,
    today_iso_date: str,
) -> list[AnomalyHit]:
    """Fleet aggregate hot day (≥ +3R) or drawdown (≤ -3R) — date-scoped key."""
    hits: list[AnomalyHit] = []
    # sum every trade whose ts starts with today_iso_date (UTC date)
    total_r = 0.0
    n_trades = 0
    for _bot_id, trades in by_bot.items():
        for r, ts in trades:
            if not ts.startswith(today_iso_date):
                continue
            total_r += r
            n_trades += 1
    if n_trades == 0:
        return hits

    if total_r >= FLEET_HOT_DAY_R:
        hits.append(
            AnomalyHit(
                asof=datetime.now(UTC).isoformat(),
                pattern="fleet_hot_day",
                key=f"fleet_hot_day:{today_iso_date}",
                bot_id="__fleet__",
                severity="info",
                detail=f"Fleet up {total_r:+.2f}R today across {n_trades} trades",
                suggested_skill="jarvis-anomaly-investigator",
                extras={"total_r": round(total_r, 4), "n_trades": n_trades, "date": today_iso_date},
            )
        )
    elif total_r <= FLEET_DRAWDOWN_R:
        hits.append(
            AnomalyHit(
                asof=datetime.now(UTC).isoformat(),
                pattern="fleet_drawdown",
                key=f"fleet_drawdown:{today_iso_date}",
                bot_id="__fleet__",
                severity="critical",
                detail=f"Fleet down {total_r:+.2f}R today across {n_trades} trades",
                suggested_skill="jarvis-drawdown-response",
                extras={"total_r": round(total_r, 4), "n_trades": n_trades, "date": today_iso_date},
            )
        )
    return hits


def _detect_suspicious_win(by_bot: dict[str, list[tuple[float, str]]]) -> list[AnomalyHit]:
    """Single trade R ≥ +SUSPICIOUS_WIN_R: could be a fill-model bug or backtest leak.

    A 5R+ win on a fleet trained on ~+0.3R expected value per trade is so
    far outside the distribution it warrants a sanity check. Even if the
    fill is real, the operator should know which trade hit the lottery.
    """
    hits: list[AnomalyHit] = []
    for bot_id, trades in by_bot.items():
        for r, ts in trades:
            if r >= SUSPICIOUS_WIN_R:
                # Dedup key includes ts (each trade is a distinct event)
                ts_short = ts[:19]  # second-level dedup
                hits.append(
                    AnomalyHit(
                        asof=datetime.now(UTC).isoformat(),
                        pattern="suspicious_win",
                        key=f"suspicious_win:{bot_id}:{ts_short}",
                        bot_id=bot_id,
                        severity="warn",
                        detail=(
                            f"{bot_id} closed a {r:+.2f}R trade — verify fill realism "
                            f"(threshold {SUSPICIOUS_WIN_R:+.1f}R)"
                        ),
                        suggested_skill="jarvis-anomaly-investigator",
                        extras={"r": round(r, 4), "ts": ts, "threshold": SUSPICIOUS_WIN_R},
                    )
                )
    return hits


def _detect_prop_firm_approaching_limit() -> list[AnomalyHit]:
    """Approaching prop firm rule breach — fires BEFORE a `blown` would occur.

    For each registered account, fire a warn-or-critical based on how
    close the account is to its daily-loss or trailing-DD limit. The
    operator gets a Telegram alert when an account hits 75% used, and
    a critical when it hits 90%. ``prop_firm_killall`` can be dispatched
    by Hermes or by /killall before the limit actually breaches.

    Dedup key includes the date so each day's alert only fires once.
    """
    hits: list[AnomalyHit] = []
    try:
        from eta_engine.brain.jarvis_v3 import prop_firm_guardrails as g

        snaps = g.aggregate_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("prop_firm scan failed: %s", exc)
        return hits

    today = datetime.now(UTC).date().isoformat()
    for snap in snaps:
        rules = snap.rules
        # Daily-loss approach
        if rules.daily_loss_limit is not None and snap.daily_loss_pct_used is not None:
            used = snap.daily_loss_pct_used
            if used >= 0.90:
                severity = "critical"
                threshold = "90%"
            elif used >= 0.75:
                severity = "warn"
                threshold = "75%"
            else:
                continue
            hits.append(
                AnomalyHit(
                    asof=datetime.now(UTC).isoformat(),
                    pattern="prop_firm_daily_loss_approaching",
                    key=f"prop_firm_daily_loss:{rules.account_id}:{today}:{threshold}",
                    bot_id=rules.account_id,
                    severity=severity,
                    detail=(
                        f"{rules.account_id} daily loss {used:.0%} "
                        f"(${snap.daily_loss_remaining:,.0f} remaining of "
                        f"${rules.daily_loss_limit:,.0f})"
                    ),
                    suggested_skill="jarvis-drawdown-response",
                    extras={
                        "account_id": rules.account_id,
                        "pct_used": round(used, 4),
                        "remaining_usd": snap.daily_loss_remaining,
                        "limit_usd": rules.daily_loss_limit,
                    },
                )
            )
        # Trailing-DD approach
        if rules.trailing_drawdown is not None and snap.trailing_dd_remaining is not None:
            dd_used = (rules.trailing_drawdown - snap.trailing_dd_remaining) / rules.trailing_drawdown
            if dd_used >= 0.90:
                severity = "critical"
                threshold = "90%"
            elif dd_used >= 0.75:
                severity = "warn"
                threshold = "75%"
            else:
                continue
            hits.append(
                AnomalyHit(
                    asof=datetime.now(UTC).isoformat(),
                    pattern="prop_firm_trailing_dd_approaching",
                    key=f"prop_firm_trailing_dd:{rules.account_id}:{today}:{threshold}",
                    bot_id=rules.account_id,
                    severity=severity,
                    detail=(
                        f"{rules.account_id} trailing DD {dd_used:.0%} "
                        f"(${snap.trailing_dd_remaining:,.0f} remaining of "
                        f"${rules.trailing_drawdown:,.0f})"
                    ),
                    suggested_skill="jarvis-drawdown-response",
                    extras={
                        "account_id": rules.account_id,
                        "pct_used": round(dd_used, 4),
                        "remaining_usd": snap.trailing_dd_remaining,
                        "limit_usd": rules.trailing_drawdown,
                    },
                )
            )
    return hits


def _detect_stale_bot(by_bot: dict[str, list[tuple[float, str]]]) -> list[AnomalyHit]:
    """Bot has not closed a trade in STALE_BOT_HOURS+ but had recent activity.

    A bot in by_bot with at least one trade in the lookback window, whose
    most-recent trade is older than ``STALE_BOT_HOURS``, is silently dead.
    Common cause: bot crashed or got stuck on an open position. Operator
    needs to know — silent failure is the worst kind.

    Note: this fires only for bots that ARE in the scan window. Truly
    long-dormant bots (no trades for weeks) won't show up here — they'd
    need a separate "bot registry" check.
    """
    hits: list[AnomalyHit] = []
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=STALE_BOT_HOURS)
    for bot_id, trades in by_bot.items():
        if not trades:
            continue
        # trades are sorted chronologically; last is most recent
        _last_r, last_ts = trades[-1]
        last_dt = _parse_iso(last_ts)
        if last_dt is None:
            continue
        if last_dt < cutoff:
            hours_silent = (now - last_dt).total_seconds() / 3600.0
            hits.append(
                AnomalyHit(
                    asof=now.isoformat(),
                    pattern="stale_bot",
                    key=f"stale_bot:{bot_id}:{last_dt.date().isoformat()}",
                    bot_id=bot_id,
                    severity="warn",
                    detail=(f"{bot_id} has not closed a trade in {hours_silent:.0f}h (last: {last_ts[:19]})"),
                    suggested_skill="jarvis-anomaly-investigator",
                    extras={"hours_silent": round(hours_silent, 2), "last_ts": last_ts},
                )
            )
    return hits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan(
    lookback_hours: float = SCAN_LOOKBACK_HOURS,
    trade_closes_path: Path | None = None,
    hits_log: Path = DEFAULT_HITS_LOG,
) -> list[AnomalyHit]:
    """Run all detectors on recent trades. Returns NEW (post-dedup) hits only.

    Runs 7 detectors covering both negative (loss_streak, loss_rate,
    fleet_drawdown, stale_bot, suspicious_win) and positive (win_streak,
    fleet_hot_day) noteworthy events. Each detector is wrapped in
    contextlib.suppress so a single broken detector won't sabotage the
    rest of the pass.

    Each new hit is also appended to the hits log so subsequent scans
    in the next DEDUP_HOURS window will suppress duplicates.

    NEVER raises. Returns empty list on read failure.
    """
    now = datetime.now(UTC)
    # Use a wider lookback so stale_bot detector can see 48h+ silence
    effective_lookback = max(lookback_hours, STALE_BOT_LOOKBACK_HOURS)
    since = now - timedelta(hours=effective_lookback)
    try:
        records = _read_trades(trade_closes_path, since)
    except Exception as exc:  # noqa: BLE001
        logger.warning("anomaly_watcher.scan read failed: %s", exc)
        return []

    by_bot: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for rec in records:
        r = _extract_r(rec)
        if r is None:
            continue
        bot_id = str(rec.get("bot_id") or "")
        if not bot_id:
            continue
        ts = str(rec.get("ts") or rec.get("closed_at") or "")
        by_bot[bot_id].append((r, ts))
    # Sort each bot's trades chronologically
    for bot_id in by_bot:
        by_bot[bot_id].sort(key=lambda x: x[1])

    today_iso_date = now.date().isoformat()

    all_hits: list[AnomalyHit] = []
    with contextlib.suppress(Exception):
        all_hits.extend(_detect_loss_streak(by_bot))
    with contextlib.suppress(Exception):
        all_hits.extend(_detect_loss_rate(by_bot))
    with contextlib.suppress(Exception):
        all_hits.extend(_detect_win_streak(by_bot))
    with contextlib.suppress(Exception):
        all_hits.extend(_detect_fleet_total(by_bot, today_iso_date=today_iso_date))
    with contextlib.suppress(Exception):
        all_hits.extend(_detect_suspicious_win(by_bot))
    with contextlib.suppress(Exception):
        all_hits.extend(_detect_stale_bot(by_bot))
    with contextlib.suppress(Exception):
        all_hits.extend(_detect_prop_firm_approaching_limit())

    # Dedup against recent log
    recent_keys = _recent_keys(hits_log)
    new_hits = [h for h in all_hits if h.key not in recent_keys]

    # Persist new hits
    for hit in new_hits:
        _append_hit(hit, hits_log)

    return new_hits


def recent_hits(
    since_hours: int = 24,
    hits_log: Path = DEFAULT_HITS_LOG,
) -> list[dict[str, Any]]:
    """Return logged hits newer than ``since_hours`` for operator review."""
    if not hits_log.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    out: list[dict[str, Any]] = []
    try:
        with hits_log.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_iso(rec.get("asof"))
                if ts is None or ts < cutoff:
                    continue
                out.append(rec)
    except OSError as exc:
        logger.warning("anomaly_watcher.recent_hits read failed: %s", exc)
    return out
