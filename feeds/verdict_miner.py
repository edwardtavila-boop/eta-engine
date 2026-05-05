"""JARVIS Verdict Pattern Miner — surfaces patterns from ``verdicts.jsonl``.

Analyzes verdict history to find:

- Hourly DENIED rates per bot
- ``reduce_size_cap`` patterns
- Conditional/approve ratios
- Time-of-day APPROVED concentration
- Bot-level calibration drift

Output: ``reports/verdict_patterns/daily_report.json``.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("verdict_miner")


class VerdictPatternMiner:
    """Read ``verdicts.jsonl`` and produce a structured pattern report."""

    def __init__(self, verdicts_path: str | Path, output_path: str | Path) -> None:
        self.verdicts_path = Path(verdicts_path)
        self.output_path = Path(output_path)

    def run(self) -> dict[str, Any]:
        verdicts = self._load_verdicts()
        if not verdicts:
            return {"status": "empty", "message": "No verdicts to analyze"}

        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "total_verdicts": len(verdicts),
            "period_hours": self._compute_period(verdicts),
            "by_bot": self._analyze_by_bot(verdicts),
            "by_hour": self._analyze_by_hour(verdicts),
            "by_strategy": self._analyze_by_strategy(verdicts),
            "denied_hotspots": self._denied_hotspots(verdicts),
            "size_cap_patterns": self._size_cap_patterns(verdicts),
            "overall_approval_rate": self._overall_rate(verdicts),
            "recommendations": self._generate_recommendations(verdicts),
        }

        self._write(report)
        log.info(
            "Verdict mine: %d verdicts, %.1f%% approve, %d bots",
            report["total_verdicts"],
            report["overall_approval_rate"]["approve_pct"],
            len(report["by_bot"]),
        )
        return report

    def _load_verdicts(self) -> list[dict[str, Any]]:
        if not self.verdicts_path.is_file():
            return []
        verdicts: list[dict[str, Any]] = []
        for raw in self.verdicts_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                verdicts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return verdicts

    @staticmethod
    def _compute_period(verdicts: list[dict]) -> float:
        timestamps = [v.get("ts") or v.get("timestamp") or "" for v in verdicts]
        parsed: list[datetime] = []
        for ts in timestamps:
            try:
                parsed.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except (ValueError, AttributeError):
                continue
        if len(parsed) < 2:
            return 0.0
        return (max(parsed) - min(parsed)).total_seconds() / 3600

    def _analyze_by_bot(self, verdicts: list[dict]) -> dict[str, dict]:
        bots: dict[str, dict] = {}
        for v in verdicts:
            bot = v.get("bot_id", v.get("id", "unknown"))
            if bot not in bots:
                bots[bot] = {"total": 0, "approved": 0, "denied": 0, "conditional": 0, "size_caps": 0}
            bots[bot]["total"] += 1
            verdict = str(v.get("verdict", v.get("decision", ""))).upper()
            if verdict == "APPROVED":
                bots[bot]["approved"] += 1
            elif verdict == "DENIED":
                bots[bot]["denied"] += 1
            elif verdict in ("CONDITIONAL", "CONDITIONAL_APPROVED"):
                bots[bot]["conditional"] += 1
            if str(v.get("action", "")).lower() == "reduce_size_cap":
                bots[bot]["size_caps"] += 1
        for stats in bots.values():
            total = stats["total"]
            stats["approve_pct"] = round(stats["approved"] / total * 100, 1) if total else 0
            stats["deny_pct"] = round(stats["denied"] / total * 100, 1) if total else 0
        return bots

    def _analyze_by_hour(self, verdicts: list[dict]) -> dict[int, dict]:
        hourly: dict[int, dict] = {}
        for v in verdicts:
            ts = v.get("ts") or v.get("timestamp") or ""
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                h = dt.hour
            except (ValueError, AttributeError):
                continue
            if h not in hourly:
                hourly[h] = {"total": 0, "approved": 0, "denied": 0}
            hourly[h]["total"] += 1
            verdict = str(v.get("verdict", v.get("decision", ""))).upper()
            if verdict == "APPROVED":
                hourly[h]["approved"] += 1
            elif verdict == "DENIED":
                hourly[h]["denied"] += 1
        return hourly

    def _analyze_by_strategy(self, verdicts: list[dict]) -> dict[str, dict]:
        strategies: dict[str, dict] = {}
        for v in verdicts:
            strat = v.get("strategy", v.get("strategy_kind", v.get("kind", "unknown")))
            if strat not in strategies:
                strategies[strat] = {"total": 0, "approved": 0, "denied": 0}
            strategies[strat]["total"] += 1
            verdict = str(v.get("verdict", v.get("decision", ""))).upper()
            if verdict == "APPROVED":
                strategies[strat]["approved"] += 1
            elif verdict == "DENIED":
                strategies[strat]["denied"] += 1
        return strategies

    def _denied_hotspots(self, verdicts: list[dict]) -> list[dict]:
        """Find bots with highest deny rates and repeated denials."""
        by_bot = self._analyze_by_bot(verdicts)
        hotspots: list[dict] = []
        for bot, stats in sorted(by_bot.items(), key=lambda x: x[1]["deny_pct"], reverse=True):
            if stats["denied"] >= 3:
                hotspots.append({
                    "bot": bot,
                    "denied": stats["denied"],
                    "deny_pct": stats["deny_pct"],
                    "total": stats["total"],
                })
        return hotspots[:10]

    def _size_cap_patterns(self, verdicts: list[dict]) -> list[dict]:
        """Find bots that frequently get size caps."""
        by_bot = self._analyze_by_bot(verdicts)
        patterns: list[dict] = []
        for bot, stats in sorted(by_bot.items(), key=lambda x: x[1]["size_caps"], reverse=True):
            if stats["size_caps"] > 0:
                patterns.append({
                    "bot": bot,
                    "size_caps": stats["size_caps"],
                    "size_cap_rate": round(stats["size_caps"] / stats["total"] * 100, 1),
                    "total": stats["total"],
                })
        return patterns[:10]

    def _overall_rate(self, verdicts: list[dict]) -> dict:
        total = len(verdicts)
        approved = sum(
            1 for v in verdicts
            if str(v.get("verdict", v.get("decision", ""))).upper() == "APPROVED"
        )
        denied = sum(
            1 for v in verdicts
            if str(v.get("verdict", v.get("decision", ""))).upper() == "DENIED"
        )
        return {
            "total": total,
            "approved": approved,
            "denied": denied,
            "approve_pct": round(approved / total * 100, 1) if total else 0,
            "deny_pct": round(denied / total * 100, 1) if total else 0,
        }

    def _generate_recommendations(self, verdicts: list[dict]) -> list[str]:
        recs: list[str] = []
        hotspots = self._denied_hotspots(verdicts)
        if hotspots:
            recs.append(
                f"Review {hotspots[0]['bot']}: "
                f"{hotspots[0]['denied']} denials ({hotspots[0]['deny_pct']}%)",
            )
        hourly = self._analyze_by_hour(verdicts)
        if hourly:
            worst_hour = min(hourly, key=lambda h: hourly[h]["approved"] / max(hourly[h]["total"], 1))
            recs.append(f"Worst approval hour: {worst_hour}:00 UTC")
        caps = self._size_cap_patterns(verdicts)
        if caps:
            recs.append(f"Size cap leader: {caps[0]['bot']} ({caps[0]['size_caps']} caps)")
        return recs

    def _write(self, report: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8",
        )


def main() -> None:
    from argparse import ArgumentParser
    parser = ArgumentParser(description="JARVIS Verdict Pattern Miner")
    parser.add_argument(
        "--verdicts", type=Path,
        default=Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_live_log.jsonl"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("C:/EvolutionaryTradingAlgo/reports/verdict_patterns/daily_report.json"),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    miner = VerdictPatternMiner(verdicts_path=args.verdicts, output_path=args.output)
    report = miner.run()
    if "overall_approval_rate" in report:
        print(
            f"Verdicts: {report['total_verdicts']} | "
            f"Approve: {report['overall_approval_rate']['approve_pct']}%",
        )
    else:
        print(f"Verdicts: {report.get('message', 'no data')}")


if __name__ == "__main__":
    main()
