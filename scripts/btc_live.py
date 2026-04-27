"""BTC live-trading launcher with mandatory paper-verification gate.

Execution tiers
---------------
PAPER (default)
    No flags required. Runs ``BtcPaperRunner`` with a synthetic stream and
    the ``PaperRouter`` -- no real money, no real API calls. This exists
    so the launcher always has something to do in CI and smoke runs.

LIVE (explicit, double-gated)
    Requires ALL of:
      1. ``--live`` flag
      2. ``APEX_BTC_LIVE=1`` environment variable
      3. A PASS verdict in ``docs/btc_paper/btc_paper_run_latest.json``
         produced by ``scripts/btc_paper_trade.py`` that is ``--max-age-h``
         (default 48) hours old or younger.
      4. A Bybit venue adapter importable at ``eta_engine.venues.bybit``.
         If the adapter is missing we refuse to flip to live.

Any failure falls back to PAPER and logs why. This is intentional -- the
script's job is to NEVER silently trade real money without an operator
explicitly attesting all three gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from eta_engine.bots.crypto_seed.bot import (  # noqa: E402
    SEED_CONFIG,
    CryptoSeedBot,
)
from eta_engine.obs.decision_journal import (  # noqa: E402
    Actor,
    DecisionJournal,
    Outcome,
)
from eta_engine.scripts.btc_paper_trade import (  # noqa: E402
    AlwaysApproveGate,
    BtcPaperRunner,
    ConfluenceFloorGate,
    PaperRouter,
    synthetic_btc_stream,
)

if TYPE_CHECKING:
    from collections.abc import Callable


DEFAULT_VERIFY_PATH = ROOT / "docs" / "btc_paper" / "btc_paper_run_latest.json"
DEFAULT_LIVE_LOG_DIR = ROOT / "docs" / "btc_live"


# ---------------------------------------------------------------------------
# Gate result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveGateDecision:
    """Why btc_live chose PAPER vs LIVE and how it got there."""

    allow_live: bool
    mode: str  # "PAPER" or "LIVE"
    reasons: tuple[str, ...]
    verify_path: Path
    verify_verdict: str | None
    verify_age_h: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow_live": self.allow_live,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "verify_path": str(self.verify_path),
            "verify_verdict": self.verify_verdict,
            "verify_age_h": self.verify_age_h,
        }


# ---------------------------------------------------------------------------
# Gate logic -- pure, testable, no side effects
# ---------------------------------------------------------------------------


def _load_verification(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _verification_age_hours(
    payload: dict[str, Any],
    *,
    now: datetime,
) -> float | None:
    ended = payload.get("ended_utc")
    if not isinstance(ended, str):
        return None
    try:
        ended_ts = datetime.fromisoformat(ended)
    except ValueError:
        return None
    if ended_ts.tzinfo is None:
        ended_ts = ended_ts.replace(tzinfo=UTC)
    return (now - ended_ts).total_seconds() / 3600.0


def _bybit_adapter_available() -> bool:
    """True iff ``eta_engine.venues.bybit`` imports clean.

    We don't actually call any network methods here -- we just want to know
    if the adapter module exists so live mode has somewhere to route fills.
    """
    try:
        import importlib  # noqa: PLC0415  -- defer to runtime

        importlib.import_module("eta_engine.venues.bybit")
    except ImportError:
        return False
    return True


def evaluate_live_gate(
    *,
    want_live: bool,
    env: dict[str, str],
    verify_path: Path = DEFAULT_VERIFY_PATH,
    max_age_h: float = 48.0,
    now: datetime | None = None,
    adapter_probe: Callable[[], bool] | None = None,
) -> LiveGateDecision:
    """Pure decision function -- easy to unit-test, no network, no subprocess.

    Returns a ``LiveGateDecision`` describing whether live mode is allowed and
    why. The caller decides what to do with that (log it, then hand the right
    router to the runner).
    """
    now = now if now is not None else datetime.now(UTC)
    probe = adapter_probe if adapter_probe is not None else _bybit_adapter_available

    reasons: list[str] = []
    if not want_live:
        reasons.append("paper requested (no --live flag)")

    env_flag = env.get("APEX_BTC_LIVE", "").strip()
    if env_flag != "1":
        reasons.append(
            f"APEX_BTC_LIVE env flag not '1' (got {env_flag!r})",
        )

    payload = _load_verification(verify_path)
    verdict: str | None = None
    age_h: float | None = None
    if payload is None:
        reasons.append(f"no paper verification artifact at {verify_path}")
    else:
        verdict = payload.get("verdict")
        if verdict != "PASS":
            reasons.append(f"paper verification verdict={verdict!r}, need PASS")
        age_h = _verification_age_hours(payload, now=now)
        if age_h is None:
            reasons.append("paper verification artifact has no usable ended_utc")
        elif age_h > max_age_h:
            reasons.append(
                f"paper verification is {age_h:.1f}h old (max_age_h={max_age_h})",
            )

    if not probe():
        reasons.append("bybit venue adapter not importable -- refusing live")

    allow_live = want_live and not reasons
    mode = "LIVE" if allow_live else "PAPER"
    return LiveGateDecision(
        allow_live=allow_live,
        mode=mode,
        reasons=tuple(reasons),
        verify_path=verify_path,
        verify_verdict=verdict,
        verify_age_h=age_h,
    )


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


async def _run_paper(
    *,
    bars: int,
    gate_floor: float,
    out_dir: Path,
    journal: DecisionJournal,
) -> dict[str, Any]:
    """Execute the same paper flow btc_paper_trade exposes, but under the
    btc_live.py logging prefix so live-log reviewers see the fallback.
    """
    bot = CryptoSeedBot(SEED_CONFIG)
    router = PaperRouter(fee_bps=2.0)
    gate = AlwaysApproveGate() if gate_floor <= 0 else ConfluenceFloorGate(floor=gate_floor)
    runner = BtcPaperRunner(
        bot=bot,
        router=router,
        gate=gate,
        journal=journal,
        max_bars=bars,
    )
    result = await runner.run(synthetic_btc_stream(n_bars=bars))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"btc_live_paperfallback_{ts}.json"
    out_path.write_text(
        json.dumps(result.to_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    latest = out_dir / "btc_live_latest.json"
    latest.write_text(
        json.dumps(result.to_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return {"mode": "PAPER", "artifact": str(out_path), "verdict": result.verdict}


async def _run_live(
    *,
    out_dir: Path,
    journal: DecisionJournal,
) -> dict[str, Any]:
    """Placeholder that records a live-activation decision and exits without
    placing a real order.

    We intentionally do NOT call into Bybit from this launcher. Live trading
    is owned by the runtime supervisor (``scripts/live_supervisor.py``) which
    reads this decision record before bringing up the Bybit router. Keeping
    the actual order-placing logic out of this one-shot launcher means we
    can't accidentally double-arm a live session.
    """
    journal.record(
        actor=Actor.TRADE_ENGINE,
        outcome=Outcome.NOTED,
        intent="btc_live.py granted LIVE activation -- handing off to supervisor",
        metadata={"handoff": "scripts/live_supervisor.py", "layer": 2},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    activation_path = out_dir / f"btc_live_activation_{ts}.json"
    activation = {
        "ts_utc": datetime.now(UTC).isoformat(),
        "mode": "LIVE",
        "handoff": "scripts/live_supervisor.py",
        "note": "BTC live activation recorded; launcher does not place orders.",
    }
    activation_path.write_text(
        json.dumps(activation, indent=2) + "\n",
        encoding="utf-8",
    )
    latest = out_dir / "btc_live_latest.json"
    latest.write_text(
        json.dumps(activation, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"mode": "LIVE", "artifact": str(activation_path)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Launch the BTC bot. Defaults to PAPER; LIVE requires --live, "
            "APEX_BTC_LIVE=1, and a recent PASS paper-verification artifact."
        ),
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Request LIVE mode. Still subject to env flag + verification gate.",
    )
    p.add_argument(
        "--verify-path",
        type=str,
        default=str(DEFAULT_VERIFY_PATH),
        help="Paper-verification artifact consulted by the gate.",
    )
    p.add_argument(
        "--max-age-h",
        type=float,
        default=48.0,
        help="Reject verification artifacts older than this many hours.",
    )
    p.add_argument(
        "--out-dir",
        type=str,
        default=str(DEFAULT_LIVE_LOG_DIR),
        help="Where to write activation/fallback artifacts.",
    )
    p.add_argument(
        "--bars",
        type=int,
        default=180,
        help="Synthetic bars for paper fallback only.",
    )
    p.add_argument(
        "--gate-floor",
        type=float,
        default=7.0,
        help="ConfluenceFloorGate threshold for paper fallback (<=0 = always-approve).",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    decision = evaluate_live_gate(
        want_live=args.live,
        env=dict(os.environ),
        verify_path=Path(args.verify_path),
        max_age_h=args.max_age_h,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    journal = DecisionJournal(out_dir / "btc_live_decisions.jsonl")
    journal.record(
        actor=Actor.RISK_GATE,
        outcome=Outcome.NOTED,
        intent=f"btc_live gate decided mode={decision.mode}",
        metadata=decision.as_dict(),
    )

    # Always emit the decision so ops can audit the launcher.
    decision_path = out_dir / "btc_live_gate_decision.json"
    decision_path.write_text(
        json.dumps(decision.as_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    if decision.allow_live:
        outcome = await _run_live(out_dir=out_dir, journal=journal)
    else:
        outcome = await _run_paper(
            bars=args.bars,
            gate_floor=args.gate_floor,
            out_dir=out_dir,
            journal=journal,
        )

    print(f"btc_live mode:        {decision.mode}")
    if decision.reasons:
        print("  reasons:")
        for r in decision.reasons:
            print(f"    - {r}")
    print(f"  verify_verdict:     {decision.verify_verdict}")
    if decision.verify_age_h is not None:
        print(f"  verify_age_h:       {decision.verify_age_h:.2f}")
    print(f"  artifact:           {outcome.get('artifact', '?')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
