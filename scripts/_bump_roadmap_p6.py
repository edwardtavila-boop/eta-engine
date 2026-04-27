"""One-shot roadmap bump for P3/P4/P6 portfolio-risk wiring."""

import json
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "roadmap_state.json"
with path.open(encoding="utf-8") as f:
    data = json.load(f)

data["last_updated"] = "2026-04-17T05:35:00.000000+00:00"
data["last_updated_utc"] = "2026-04-17T05:35:00.000000+00:00"
data["overall_progress_pct"] = 94
data["shared_artifacts"]["eta_engine_tests_passing"] = 521

for phase in data["phases"]:
    if phase["id"] == "P3_PROOF":
        for t in phase["tasks"]:
            if t["name"] == "Portfolio-level correlation across all bots":
                t["status"] = "done"
        phase["progress_pct"] = 82
    if phase["id"] == "P4_SHIELD":
        for t in phase["tasks"]:
            if t["name"] == "Real-time VaR/CVaR + correlation brake":
                t["status"] = "done"
        phase["progress_pct"] = 87
    if phase["id"] == "P6_FUNNEL":
        for t in phase["tasks"]:
            if t["name"] == "Central equity+baseline+withdrawn dashboard":
                t["status"] = "done"
        phase["progress_pct"] = 78

data["shared_artifacts"]["eta_engine_portfolio_risk_wired"] = {
    "timestamp_utc": "2026-04-17T05:35:00.000000+00:00",
    "modules": [
        "eta_engine/core/portfolio_risk.py",
        "eta_engine/backtest/portfolio_correlation.py",
        "eta_engine/funnel/central_dashboard.py",
    ],
    "tests_new": 35,
    "tests_total": 521,
    "ruff_green": True,
    "pytest_green": True,
    "phase_gap_closed": [
        "P3_PROOF.portfolio_correlation",
        "P4_SHIELD.portfolio_var",
        "P6_FUNNEL.central_dashboard",
    ],
    "api_surface": {
        "portfolio_risk.PortfolioRisk": [
            "var_historical",
            "var_parametric",
            "cvar",
            "portfolio_var",
            "correlation_brake",
            "size_multiplier",
        ],
        "portfolio_correlation.analyze": ("pnl_series -> PortfolioCorrelationReport (eff_n, flags, pairwise)"),
        "central_dashboard.build_snapshot": ("PortfolioState + bot_details + staking -> CentralDashboardSnapshot"),
    },
    "notes": (
        "VaR/CVaR uses scipy.stats.norm for parametric; central_dashboard "
        "consumes equity_monitor.PortfolioState; all 3 modules pure-compute "
        "(no SDK deps)."
    ),
}

with path.open("w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print("roadmap_state.json updated:")
print("  overall_progress_pct=", data["overall_progress_pct"])
print("  eta_engine_tests_passing=", data["shared_artifacts"]["eta_engine_tests_passing"])
for phase in data["phases"]:
    if phase["id"] in ("P3_PROOF", "P4_SHIELD", "P6_FUNNEL"):
        print("  " + phase["id"] + "=", phase["progress_pct"], "%")
