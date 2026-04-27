"""
EVOLUTIONARY TRADING ALGO  //  scripts.weekly_review
========================================
Firm re-review cadence artifact.

Once a week, drive The Firm board against the most recent paper-run or live-tiny
result, record the verdict, and append to docs/weekly_review_log.json. Designed
to be safe to run from a scheduler — creates its own directory, appends deltas,
does NOT mutate prior rows.

Outputs
-------
- docs/weekly_review_log.json      — append-only history of weekly reviews
- docs/weekly_review_latest.json   — single-file latest entry (dashboard-friendly)
- docs/weekly_review_latest.txt    — 80-col text summary

Usage
-----
    python -m eta_engine.scripts.weekly_review \
        --spec eta_engine/docs/firm_spec_paper_results_v2.json \
        --tier A

    python -m eta_engine.scripts.weekly_review --auto

The `--auto` mode picks the newest `firm_spec_paper_results_*.json` under docs/.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.core.principles_checklist import (  # noqa: E402
    DEFAULT_PRINCIPLES,
    ChecklistAnswer,
    ChecklistReport,
    build_report,
)


@dataclass
class ReviewEntry:
    generated_at_utc: str
    week_of: str
    spec_id: str
    spec_path: str
    tier: str
    bots_in_scope: list[str]
    trades: int
    blended_expectancy_r: float
    blended_dd_pct: float
    firm_verdict: str
    quant_vote: str
    risk_vote: str
    redteam_vote: str
    macro_vote: str
    micro_vote: str
    pm_vote: str
    actions_required: list[str]
    kill_log_entries_at_time: int


def _pick_latest_spec() -> Path:
    docs = ROOT / "docs"
    candidates = sorted(
        docs.glob("firm_spec_paper_results_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No firm_spec_paper_results_*.json found under {docs}",
        )
    return candidates[0]


def _tier_bots(spec: dict, tier: str) -> list[str]:
    tier = tier.upper()
    prom = (
        spec.get("promotion_path_proposed_v2")
        or spec.get(
            "promotion_path_proposed",
        )
        or {}
    )
    if tier == "A":
        return list(prom.get("tier_A_graduate_to_live_tiny", []))
    if tier == "B":
        return list(prom.get("tier_B_hold_at_paper_gate", []))
    return []


def _load_spec_chain(spec: dict) -> list[dict]:
    """Return [spec, parent_spec, ...] — follows parent_spec field."""
    chain = [spec]
    cursor = spec
    while True:
        parent_id = cursor.get("parent_spec")
        if not parent_id:
            break
        # Look under docs/ for a file matching the ID
        docs = ROOT / "docs"
        # e.g. APEX_PAPER_RESULTS_v1 → firm_spec_paper_results_v1.json
        guess = docs / (parent_id.lower().replace("apex_", "firm_spec_") + ".json")
        if not guess.exists():
            # fallback: search
            hits = list(docs.glob(f"*{parent_id.split('_', 1)[1].lower()}*.json"))
            guess = hits[0] if hits else None
        if not guess or not guess.exists():
            break
        try:
            cursor = json.loads(guess.read_text())
            chain.append(cursor)
        except Exception:
            break
    return chain


def _find_per_bot(chain: list[dict], names: list[str]) -> dict:
    """Search the spec chain for a per_bot row for each name."""
    found: dict[str, dict] = {}
    for spec in chain:
        pm = spec.get("harness_run", {}).get("per_bot", {}) or {}
        for name in names:
            if name in pm and name not in found:
                found[name] = pm[name]
        if len(found) == len(names):
            break
    return found


def _metrics_from_spec(spec: dict, tier: str) -> tuple[int, float, float]:
    """Returns (trades, blended_expectancy_r, blended_dd_pct)."""
    chain = _load_spec_chain(spec)
    if tier.upper() == "A":
        rows = _find_per_bot(chain, ["mnq", "nq"])
        if not rows:
            return 0, 0.0, 0.0
        trades = sum(b.get("trades", 0) for b in rows.values())
        if trades == 0:
            return 0, 0.0, 0.0
        blended = sum(b.get("expectancy_r", 0.0) * b.get("trades", 0) for b in rows.values()) / trades
        dd = max((b.get("max_dd_pct", 0.0) for b in rows.values()), default=0.0)
        return trades, blended, dd
    # Tier B: prefer v2 aggregate_tier_b, fallback to per-bot
    agg = spec.get("harness_run_v2c", {}).get("aggregate_tier_b") or {}
    if agg:
        return (
            int(agg.get("total_trades", 0)),
            float(agg.get("blended_expectancy_r", 0.0)),
            float(agg.get("blended_max_dd_pct", 0.0)),
        )
    rows = _find_per_bot(chain, ["crypto_seed", "eth_perp", "sol_perp", "xrp_perp"])
    trades = sum(b.get("trades", 0) for b in rows.values())
    if trades == 0:
        return 0, 0.0, 0.0
    blended = sum(b.get("expectancy_r", 0.0) * b.get("trades", 0) for b in rows.values()) / trades
    dd = max((b.get("max_dd_pct", 0.0) for b in rows.values()), default=0.0)
    return trades, blended, dd


def _engage_firm(spec_path: Path) -> dict:
    """Call the firm-engagement script and parse the return verdict."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "eta_engine.scripts.engage_firm_board", "--spec", str(spec_path)],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT.parent,
            timeout=300,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return {
            "verdict": "UNKNOWN",
            "error": str(e),
            "raw_stdout": "",
            "raw_stderr": "",
        }
    blob = (out.stdout or "") + "\n" + (out.stderr or "")
    verdict = "UNKNOWN"
    if "FINAL GO" in blob or "Verdict: GO" in blob:
        verdict = "GO"
    elif "FINAL MODIFY" in blob or "Verdict: MODIFY" in blob:
        verdict = "MODIFY"
    elif "FINAL KILL" in blob or "Verdict: KILL" in blob:
        verdict = "KILL"
    votes = {}
    for agent in ("Quant", "RedTeam", "Risk", "Macro", "Micro", "PM"):
        for token in ("GO", "CONTINUE", "MODIFY", "KILL", "PASS", "FAIL"):
            marker = f"{agent}: {token}"
            if marker in blob:
                votes[agent] = token
                break
        votes.setdefault(agent, "UNKNOWN")
    return {
        "verdict": verdict,
        "votes": votes,
        "raw_stdout": out.stdout or "",
        "raw_stderr": out.stderr or "",
    }


def _load_checklist(
    answers_path: Path | None,
    period_label: str,
) -> ChecklistReport | None:
    """If a checklist-answers JSON is supplied, build a report. Otherwise
    write a stub (all no) template beside the output so the operator can
    fill it in.
    """
    if answers_path is None or not answers_path.exists():
        return None
    raw = json.loads(answers_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("checklist-answers must be a JSON list")
    answers = [ChecklistAnswer(**row) for row in raw]
    return build_report(answers, period_label=period_label)


def _write_checklist_stub(out_dir: Path) -> Path:
    """Emit a template the operator can copy + answer."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stub_path = out_dir / "weekly_checklist_template.json"
    stub = [{"index": p.index, "yes": False, "note": ""} for p in DEFAULT_PRINCIPLES]
    stub_path.write_text(json.dumps(stub, indent=2), encoding="utf-8")
    return stub_path


def _write_checklist_report(report: ChecklistReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "weekly_checklist_latest.json"
    latest.write_text(
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    txt = out_dir / "weekly_checklist_latest.txt"
    lines = [
        "EVOLUTIONARY TRADING ALGO -- Weekly Principles Checklist",
        "=" * 80,
        f"Period: {report.period_label}   Generated: {report.ts.isoformat()}",
        f"Score: {report.score:.0%}   Letter: {report.letter_grade}   Discipline: {report.discipline_score}/10",
        "-" * 80,
    ]
    slug_by_index = {p.index: p.slug for p in DEFAULT_PRINCIPLES}
    question_by_index = {p.index: p.question for p in DEFAULT_PRINCIPLES}
    for a in report.answers:
        mark = "[Y]" if a.yes else "[ ]"
        lines.append(
            f"  {mark} {slug_by_index[a.index]:<22} -- {question_by_index[a.index]}",
        )
        if a.note:
            lines.append(f"         note: {a.note}")
    if report.critical_gaps:
        lines.append("-" * 80)
        lines.append("CRITICAL GAPS:")
        for g in report.critical_gaps:
            lines.append(f"  - {g}")
    lines.append("=" * 80)
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return latest


def _kill_log_count() -> int:
    klp = ROOT / "docs" / "kill_log.json"
    if not klp.exists():
        return 0
    try:
        raw = json.loads(klp.read_text())
        if isinstance(raw, dict):
            entries = raw.get("entries", [])
            return len(entries) if isinstance(entries, list) else 0
        return len(raw) if isinstance(raw, list) else 0
    except Exception:
        return 0


def _actions_from_verdict(verdict: str, tier: str) -> list[str]:
    t = tier.upper()
    if verdict == "GO" and t == "A":
        return [
            "Proceed with Tier-A live-tiny (1 MNQ + 1 NQ)",
            "Monitor daily; escalate if DD exceeds 10%",
            "Schedule next weekly review (T+7)",
        ]
    if verdict == "GO" and t == "B":
        return [
            "Promote Tier-B to live-tiny at 50% of Tier-A size",
            "Enable correlation kill-switch across BTC/ETH/SOL/XRP",
            "Schedule next weekly review (T+7)",
        ]
    if verdict == "MODIFY":
        return [
            f"Hold Tier-{t} at current phase",
            "Acquire real-venue data (Bybit paper) before next cycle",
            "Re-run harness with --bar-mode paper",
            "Re-engage Firm board at T+7",
        ]
    if verdict == "KILL":
        return [
            f"Kill Tier-{t}",
            "Pull offending bots from run_manifest",
            "Dump incident to kill_log",
            "Quant+RedTeam draft corrective spec within 48h",
        ]
    return [f"Verdict {verdict} unrecognized — manual triage required"]


def _write(
    entry: ReviewEntry,
    out_dir: Path,
    raw_firm_blob: str,
) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "weekly_review_log.json"
    latest_json = out_dir / "weekly_review_latest.json"
    latest_txt = out_dir / "weekly_review_latest.txt"

    # Append-only log
    history: list[dict] = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text())
            if isinstance(existing, list):
                history = existing
        except Exception:
            history = []
    history.append(asdict(entry))
    log_path.write_text(json.dumps(history, indent=2))
    latest_json.write_text(json.dumps(asdict(entry), indent=2))

    lines: list[str] = []
    lines.append("EVOLUTIONARY TRADING ALGO -- Weekly Firm Review")
    lines.append("=" * 80)
    lines.append(f"Week of: {entry.week_of}    Generated: {entry.generated_at_utc}")
    lines.append(f"Spec:    {entry.spec_id}  ({entry.spec_path})")
    lines.append(f"Tier:    {entry.tier}   Bots: {', '.join(entry.bots_in_scope)}")
    lines.append("-" * 80)
    lines.append(
        f"Metrics: trades={entry.trades}  "
        f"expectancy={entry.blended_expectancy_r:+.3f}R  "
        f"dd={entry.blended_dd_pct:.2f}%",
    )
    lines.append("-" * 80)
    lines.append(f"Firm verdict: {entry.firm_verdict}")
    lines.append(
        f"Votes: Quant={entry.quant_vote}  RedTeam={entry.redteam_vote}  "
        f"Risk={entry.risk_vote}  Macro={entry.macro_vote}  "
        f"Micro={entry.micro_vote}  PM={entry.pm_vote}",
    )
    lines.append("-" * 80)
    lines.append("Actions required:")
    for a in entry.actions_required:
        lines.append(f"  - {a}")
    lines.append("-" * 80)
    lines.append(f"Kill log entries at time of review: {entry.kill_log_entries_at_time}")
    lines.append("=" * 80)
    if raw_firm_blob:
        lines.append("Raw Firm board output (last 400 chars):")
        lines.append(raw_firm_blob[-400:])
    latest_txt.write_text("\n".join(lines) + "\n")
    return log_path, latest_json, latest_txt


def main() -> int:
    p = argparse.ArgumentParser(description="Apex Weekly Firm Review")
    p.add_argument("--spec", type=Path, default=None)
    p.add_argument("--tier", choices=("A", "B", "BOTH"), default="BOTH")
    p.add_argument("--auto", action="store_true", help="Pick newest firm_spec_paper_results_*.json under docs/")
    p.add_argument("--out-dir", type=Path, default=ROOT / "docs")
    p.add_argument("--skip-engage", action="store_true", help="Do not call engage_firm_board; read verdict from spec")
    p.add_argument(
        "--checklist-answers",
        type=Path,
        default=None,
        help="Path to JSON of 10 ChecklistAnswer rows (indices 0..9). If omitted, a stub template is written.",
    )
    args = p.parse_args()

    spec_path = args.spec or (_pick_latest_spec() if args.auto else None)
    if spec_path is None:
        print("Must supply --spec PATH or --auto", file=sys.stderr)
        return 2
    spec_path = Path(spec_path).resolve()
    if not spec_path.exists():
        print(f"Spec not found: {spec_path}", file=sys.stderr)
        return 2
    spec = json.loads(spec_path.read_text())
    spec_id = str(spec.get("spec_id", spec_path.stem))

    tiers = ("A", "B") if args.tier == "BOTH" else (args.tier,)
    raw_blob = ""
    if not args.skip_engage:
        firm_result = _engage_firm(spec_path)
        raw_blob = firm_result.get("raw_stdout", "") + "\n" + firm_result.get("raw_stderr", "")
        votes = firm_result.get("votes", {})
        verdict = firm_result.get("verdict", "UNKNOWN")
    else:
        votes = {
            "Quant": "UNKNOWN",
            "RedTeam": "UNKNOWN",
            "Risk": "UNKNOWN",
            "Macro": "UNKNOWN",
            "Micro": "UNKNOWN",
            "PM": "UNKNOWN",
        }
        verdict = "SKIPPED"

    kl_count = _kill_log_count()
    now = datetime.now(UTC)
    week_of = now.strftime("%Y-W%V")

    emitted: list[Path] = []
    for tier in tiers:
        bots = _tier_bots(spec, tier)
        trades, exp_r, dd = _metrics_from_spec(spec, tier)
        entry = ReviewEntry(
            generated_at_utc=now.isoformat(),
            week_of=week_of,
            spec_id=spec_id,
            spec_path=str(spec_path),
            tier=tier,
            bots_in_scope=bots,
            trades=trades,
            blended_expectancy_r=round(exp_r, 4),
            blended_dd_pct=round(dd, 4),
            firm_verdict=verdict,
            quant_vote=votes.get("Quant", "UNKNOWN"),
            redteam_vote=votes.get("RedTeam", "UNKNOWN"),
            risk_vote=votes.get("Risk", "UNKNOWN"),
            macro_vote=votes.get("Macro", "UNKNOWN"),
            micro_vote=votes.get("Micro", "UNKNOWN"),
            pm_vote=votes.get("PM", "UNKNOWN"),
            actions_required=_actions_from_verdict(verdict, tier),
            kill_log_entries_at_time=kl_count,
        )
        log_p, latest_j, latest_t = _write(entry, args.out_dir, raw_blob)
        emitted.append(latest_t)
        print(
            f"Tier {tier}: {verdict}  "
            f"exp={entry.blended_expectancy_r:+.3f}R  dd={entry.blended_dd_pct:.2f}%  "
            f"trades={entry.trades}"
        )

    print("-" * 80)
    print(f"Log:    {args.out_dir / 'weekly_review_log.json'}")
    print(f"Latest: {args.out_dir / 'weekly_review_latest.json'}")
    for e in emitted:
        print(f"  {e}")

    # Principles checklist: if answers supplied -> build report, else write stub
    checklist = _load_checklist(args.checklist_answers, week_of)
    if checklist is not None:
        cpath = _write_checklist_report(checklist, args.out_dir)
        print(f"Checklist: {checklist.letter_grade} ({checklist.discipline_score}/10) -> {cpath}")
    else:
        stub = _write_checklist_stub(args.out_dir)
        print(f"Checklist: no answers supplied -- stub template at {stub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
