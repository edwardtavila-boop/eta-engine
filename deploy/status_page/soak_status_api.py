"""Paper Soak Status API — serves HTML dashboard and JSON endpoint for fleet soak data.

Port 8424 (adjacent to proxy 8421 and FM status 8422).
Start with: python soak_status_api.py
"""

import json
import re
import statistics
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKSPACE_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from eta_engine.scripts import workspace_roots

LEDGER_PATH = workspace_roots.ETA_PAPER_SOAK_LEDGER_PATH
REGISTRY_PATH = workspace_roots.ETA_ENGINE_ROOT / "strategies" / "per_bot_registry.py"
STATUS_PAGE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = workspace_roots.WORKSPACE_ROOT
ELITE_DASHBOARD_PATH = WORKSPACE_ROOT / "firm_command_center" / "var" / "elite_dashboard.html"
SOAK_DASHBOARD_PATH = STATUS_PAGE_DIR / "soak_dashboard.html"
HTML_PATHS = (ELITE_DASHBOARD_PATH, SOAK_DASHBOARD_PATH)
OPS_DASHBOARD_ROUTE = "Ops Dashboard: use the 8421 operator route for broker-backed IBKR/Tradovate PnL."

app = FastAPI(title="ETA Paper Soak Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _route_context_banner(source: Path) -> str:
    """Small trust banner so the soak page is not mistaken for the ops deck."""
    return (
        '<div style="position:sticky;top:0;z-index:9999;'
        "display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap;"
        "padding:10px 14px;background:rgba(2,6,14,.92);"
        "border-bottom:1px solid rgba(96,165,250,.32);"
        "box-shadow:0 12px 34px rgba(0,0,0,.28);"
        "font:700 12px/1.35 Segoe UI,system-ui,sans-serif;"
        'letter-spacing:.03em;color:#dbeafe;text-align:center">'
        '<span style="color:#67e8f9;text-transform:uppercase">Paper Soak / Diamond Factory</span>'
        '<span style="color:#94a3b8">broker-backed ops truth lives on the 8421 operator route</span>'
        f'<span style="color:#64748b">source: {source.name}</span>'
        "</div>"
    )


def _decorate_dashboard_html(html: str, source: Path) -> str:
    banner = _route_context_banner(source)
    lower = html.lower()
    body_idx = lower.find("<body")
    if body_idx >= 0:
        body_close = html.find(">", body_idx)
        if body_close >= 0:
            return html[: body_close + 1] + banner + html[body_close + 1 :]
    return banner + html


def read_registry_map() -> dict[str, dict[str, str]]:
    reg_map: dict[str, dict[str, str]] = {}
    if not REGISTRY_PATH.exists():
        return reg_map
    content = REGISTRY_PATH.read_text(encoding="utf-8")
    for m in re.finditer(
        r'"(\w+)"\s*:\s*StrategyAssignment\(\s*symbol\s*=\s*"(\w+)"[^)]*timeframe\s*=\s*"(\w+)"[^)]*strategy_kind\s*=\s*"([^"]+)"',
        content,
    ):
        reg_map[m.group(1)] = {
            "symbol": m.group(2),
            "tf": m.group(3),
            "strategy": m.group(4),
        }
    return reg_map


def compute_sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    stdev = statistics.stdev(returns) if len(returns) >= 2 else 0.01
    if stdev < 1e-9:
        return 0.0
    return mean / stdev


def compute_sessions(ledger: dict) -> list[dict]:
    sessions_by_bot = ledger.get("bot_sessions", {})
    registry = read_registry_map()
    results = []

    for bot_id in sorted(sessions_by_bot.keys()):
        sessions = sessions_by_bot[bot_id]
        reg = registry.get(bot_id, {})
        symbol = reg.get("symbol", "?")
        strategy = reg.get("strategy", "?")

        pnls = [s.get("pnl", 0.0) for s in sessions if abs(s.get("pnl", 0.0)) > 0.01]
        total_pnl = sum(pnls)
        n_sessions = len(sessions)
        n_trades = sum(s.get("trades", 0) for s in sessions)
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100 if pnls else 0.0
        sharpe = compute_sharpe(pnls) if len(pnls) >= 2 else 0.0

        flag = ""
        if n_sessions < 2:
            flag = "THIN"
        elif total_pnl > 0 and sharpe > 0.5:
            flag = "DIAMOND"
        elif total_pnl > 0:
            flag = "GREEN"
        elif total_pnl < 0:
            flag = "RED"

        results.append(
            {
                "bot_id": bot_id,
                "symbol": symbol,
                "strategy": strategy,
                "total_pnl": round(total_pnl, 2),
                "wr": round(wr, 1),
                "sharpe": round(sharpe, 2),
                "n_sessions": n_sessions,
                "n_trades": n_trades,
                "flag": flag,
            }
        )

    return results


def build_summary(bots: list[dict]) -> dict:
    fleet_pnl = sum(b["total_pnl"] for b in bots)
    diamonds = [b for b in bots if b["flag"] == "DIAMOND"]
    profitable = [b for b in bots if b["total_pnl"] > 0]
    losers = [b for b in bots if b["total_pnl"] < 0]
    thin = [b for b in bots if b["n_sessions"] < 2]
    return {
        "total_bots": len(bots),
        "diamond_count": len(diamonds),
        "profitable_count": len(profitable),
        "losing_count": len(losers),
        "thin_count": len(thin),
        "fleet_pnl": round(fleet_pnl, 2),
        "total_trades": sum(b["n_trades"] for b in bots),
        "diamonds": [b["bot_id"] for b in diamonds],
    }


@app.get("/api/soak/status")
async def soak_status() -> dict[str, object]:
    if not LEDGER_PATH.exists():
        return JSONResponse(content={"error": "no_ledger", "detail": str(LEDGER_PATH)})
    try:
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        bots = compute_sessions(ledger)
        summary = build_summary(bots)
        return {"status": "ok", "summary": summary, "bots": bots}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": "internal", "detail": str(e)[:200]})


@app.get("/api/soak/data")
async def soak_data() -> dict[str, object]:
    if not LEDGER_PATH.exists():
        return JSONResponse(content={"error": "no_ledger", "detail": str(LEDGER_PATH), "bots": []})
    try:
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        bots = compute_sessions(ledger)
        return {"bots": bots}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": "internal", "detail": str(e)[:200], "bots": []})


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    for html_path in HTML_PATHS:
        if html_path.exists():
            return HTMLResponse(
                content=_decorate_dashboard_html(
                    html_path.read_text(encoding="utf-8"),
                    html_path,
                )
            )
    return HTMLResponse(content=f"<h1>Dashboard not found</h1><p>{OPS_DASHBOARD_ROUTE}</p>", status_code=404)


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "ledger_exists": LEDGER_PATH.exists()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8424)
