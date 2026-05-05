"""Adversarial Strategy Reviewer — Claude API pre-mortem reports.

Takes a strategy YAML + backtest results, produces structured failure-mode analysis.
Uses the Claude API (``ANTHROPIC_API_KEY`` env) for deep pre-mortem when available,
falling back to heuristic-only analysis when no key is configured.

Output: ``reports/strategy_reviews/{strategy_id}_review.json``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Load .env from canonical workspace paths so the reviewer works from any CWD.
_ENV_PATHS = [
    Path(__file__).resolve().parents[1] / ".env",
    Path(r"C:\EvolutionaryTradingAlgo\eta_engine\.env"),
]
for _ep in _ENV_PATHS:
    if _ep.exists():
        with _ep.open(encoding="utf-8") as _f:
            for _raw in _f:
                _line = _raw.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _key, _, _val = _line.partition("=")
                    _key = _key.strip()
                    _val = _val.strip().strip('"').strip("'")
                    if _key and _key not in os.environ:
                        os.environ[_key] = _val
        break

log = logging.getLogger("adversarial_reviewer")

_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip()


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from a Claude response that may contain markdown fences."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    json_str = text[start:end]
    json_str = re.sub(r",\s*}", "}", json_str)
    json_str = re.sub(r",\s*]", "]", json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    json_str = re.sub(r"//.*?$", "", json_str, flags=re.MULTILINE)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


class AdversarialStrategyReviewer:
    """Produce a structured pre-mortem review for a single strategy."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    def review(
        self,
        strategy_id: str,
        strategy_yaml: str,
        backtest_results: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        heuristic = {
            "failure_modes": self._analyze_failure_modes(strategy_yaml, backtest_results),
            "regime_sensitivity": self._assess_regime_sensitivity(strategy_yaml),
            "overfitting_flags": self._check_overfitting(backtest_results),
            "sample_size_warnings": self._check_sample_size(backtest_results),
            "slippage_fee_sanity": self._slippage_fee_check(strategy_yaml),
        }
        claude_analysis = self._claude_review(strategy_id, strategy_yaml, backtest_results)
        recommendations: list[str] = []
        for mode in heuristic["failure_modes"]:
            if mode["severity"] in ("high", "critical"):
                recommendations.append(f"Fix {mode['name']}: {mode['mitigation']}")
        if claude_analysis and claude_analysis.get("recommendations"):
            recommendations.extend(claude_analysis["recommendations"])
        report = {
            "strategy_id": strategy_id,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "reviewer": "AdversarialStrategyReviewer v2",
            "ai_enhanced": bool(claude_analysis),
            "failure_modes": heuristic["failure_modes"],
            "regime_sensitivity": heuristic["regime_sensitivity"],
            "overfitting_flags": heuristic["overfitting_flags"],
            "sample_size_warnings": heuristic["sample_size_warnings"],
            "slippage_fee_sanity": heuristic["slippage_fee_sanity"],
            "claude_premortem": claude_analysis.get("premortem") if claude_analysis else None,
            "claude_edge_cases": claude_analysis.get("edge_cases") if claude_analysis else None,
            "recommendations": recommendations,
        }
        self._write(report)
        return report

    def _claude_review(
        self,
        strategy_id: str,
        yaml_text: str,
        results: dict | None,
    ) -> dict | None:
        if not _ANTHROPIC_API_KEY:
            return None
        try:
            return self._call_claude(strategy_id, yaml_text, results)
        except Exception as exc:  # noqa: BLE001 — network/parse errors degrade to heuristic-only
            log.warning("Claude API call failed for %s: %s", strategy_id, exc)
            return None

    def _call_claude(self, strategy_id: str, yaml_text: str, results: dict | None) -> dict:
        result_summary = json.dumps(results or {}, indent=2, default=str)[:2000]
        prompt = (
            "You are an adversarial trading strategy reviewer. "
            "Analyze this strategy for hidden risks, failure modes, and edge cases.\n\n"
            f"Strategy ID: {strategy_id}\n\n"
            f"YAML Config:\n{yaml_text[:1500]}\n\n"
            f"Backtest Results:\n{result_summary}\n\n"
            "1. PREMORTEM: Imagine this strategy failed badly after 6 months live. "
            "What caused it? List 3-5 specific failure scenarios.\n"
            "2. EDGE CASES: What market conditions would break this strategy? List 2-4.\n"
            "3. RECOMMENDATIONS: Give 2-3 concrete fixes.\n\n"
            "Respond in JSON format:\n"
            '{"premortem": ["scenario1", ...], "edge_cases": ["case1", ...], '
            '"recommendations": ["rec1", ...]}'
        )
        req = urllib.request.Request(  # noqa: S310 — fixed-scheme HTTPS to a configured base URL
            f"{_ANTHROPIC_BASE_URL.rstrip('/')}/v1/messages",
            data=json.dumps({
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": _ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            body = json.loads(resp.read())
            content = body["content"][0]["text"]
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                parsed = _extract_json(content)
                if parsed:
                    return parsed
                return {
                    "premortem": [content[:800]],
                    "edge_cases": [],
                    "recommendations": [],
                }

    def _analyze_failure_modes(self, yaml_text: str, results: dict | None) -> list[dict]:
        modes: list[dict] = []
        lower = yaml_text.lower()
        if "stop_loss" not in lower and "stop" not in lower:
            modes.append({
                "name": "missing_stop_loss", "severity": "critical",
                "detail": "No stop-loss defined", "mitigation": "Add stop-loss based on ATR",
            })
        if "take_profit" not in lower and "target" not in lower:
            modes.append({
                "name": "missing_take_profit", "severity": "high",
                "detail": "No take-profit defined", "mitigation": "Add take-profit at 1.5-2x risk",
            })
        if results and results.get("trades", 0) < 30:
            modes.append({
                "name": "low_sample", "severity": "high",
                "detail": f"Only {results.get('trades', 0)} trades",
                "mitigation": "Require min 30 trades before paper_soak",
            })
        if results and results.get("win_rate", 0) > 0.8:
            modes.append({
                "name": "suspicious_win_rate", "severity": "medium",
                "detail": "Win rate >80% may indicate overfitting",
                "mitigation": "Walk-forward validate",
            })
        return modes

    def _assess_regime_sensitivity(self, yaml_text: str) -> list[dict]:
        regimes: list[dict] = []
        lower = yaml_text.lower()
        if "trend" in lower and "mean_reversion" not in lower:
            regimes.append({
                "regime": "chop", "risk": "high",
                "note": "Trend strategy will whipsaw in chop",
            })
        if "mean_reversion" in lower and "trend" not in lower:
            regimes.append({
                "regime": "trending_up", "risk": "high",
                "note": "Mean reversion fades strong trends",
            })
        return regimes

    def _check_overfitting(self, results: dict | None) -> list[dict]:
        flags: list[dict] = []
        if results:
            if results.get("sharpe", 0) > 3.0:
                flags.append({
                    "flag": "extreme_sharpe",
                    "detail": f"Sharpe {results['sharpe']} > 3.0 — likely overfit",
                })
            if results.get("parameters", 0) > 10:
                flags.append({
                    "flag": "many_parameters",
                    "detail": f"{results['parameters']} params for {results.get('trades', 0)} trades",
                })
        return flags

    def _check_sample_size(self, results: dict | None) -> list[dict]:
        if not results:
            return [{
                "warning": "no_backtest_results",
                "detail": "Cannot validate without backtest data",
            }]
        n = results.get("trades", 0)
        if n < 30:
            return [{
                "warning": "insufficient_trades",
                "detail": f"Only {n} trades; min 30 recommended",
            }]
        return []

    def _slippage_fee_check(self, yaml_text: str) -> list[dict]:
        issues: list[dict] = []
        lower = yaml_text.lower()
        if "slippage" not in lower:
            issues.append({
                "issue": "no_slippage_model",
                "detail": "Slippage not configured; assumes 0-cost execution",
                "recommendation": "Add 1-tick MNQ slippage (~$0.50/contract)",
            })
        if "commission" not in lower and "fee" not in lower:
            issues.append({
                "issue": "no_commission_model",
                "detail": "Commission not configured",
                "recommendation": "Add $2.50/contract MNQ commission",
            })
        return issues

    def _write(self, report: dict[str, Any]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{report['strategy_id']}_review.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        log.info("Review written: %s", path)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--strategy-id", default="test_strategy")
    p.add_argument(
        "--yaml",
        default="entry: close > ema_20\nstop_loss: atr * 1.5\ntake_profit: atr * 3.0\n",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("C:/EvolutionaryTradingAlgo/reports/strategy_reviews"),
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    reviewer = AdversarialStrategyReviewer(output_dir=args.output)
    report = reviewer.review(args.strategy_id, args.yaml)
    print(
        f"Review complete: {len(report['failure_modes'])} failure modes, "
        f"{len(report['recommendations'])} recommendations",
    )


if __name__ == "__main__":
    main()
