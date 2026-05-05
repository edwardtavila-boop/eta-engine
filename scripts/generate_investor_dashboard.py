"""Inner-circle / investor / beta-tester dashboard generator
(Tier-4 #15, 2026-04-27).

Generates a static HTML dashboard for inner circle, investors, and
beta-testers of the ETA Engine. Read-only, share via a public Cloudflare
hostname (e.g. ``investors.evolutionarytradingalgo.com``).

What it shows
-------------
  * Fleet status: 7 bots, current mode (PAPER SIM / LIVE), uptime
  * Today's verdict-stream summary (from JARVIS audit aggregation)
  * Last 7 kaizen tickets (titles + status; rationale hidden by default)
  * 30-day P&L curve (from broker fills, not paper sim)
  * Strategy roster: which bots are active, which are deferred
  * Resend-style "what's the system doing right now" timeline

What it does NOT show
---------------------
  * Per-trade entry/exit details (would leak strategy edge)
  * Position sizes (would leak account size)
  * Any creds, env vars, broker session info
  * Internal kill-switch reasons (just "armed" / "not armed")

The dashboard is regenerated nightly by ``Eta-Investor-Dashboard-Daily``
and pushed to ``state/investor_dashboard/index.html``. The Cloudflare
tunnel serves that file behind basic auth (operator-controlled).

Usage::

    python scripts/generate_investor_dashboard.py
    python scripts/generate_investor_dashboard.py --output state/investor_dashboard/index.html
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("generate_investor_dashboard")

ROOT = Path(__file__).resolve().parents[1]


def gather_payload() -> dict[str, Any]:
    """Pull non-sensitive state from various sources."""
    import os
    mode = os.environ.get("ETA_MODE", "PAPER").upper()
    mode_display = "LIVE" if mode == "LIVE" else "PAPER SIM"

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "fleet": {
            "size": 7,
            "names": ["MnqBot", "NqBot", "CryptoSeedBot", "EthPerpBot",
                      "SolPerpBot", "XrpPerpBot", "BtcHybridBot"],
            "mode": mode_display,
        },
        "kaizen_recent": [],
        "todays_verdicts": {},
        "pnl_30d": [],
        "policy_version": 0,
    }

    # Recent kaizen tickets (titles only; rationale hidden)
    ledger_path = ROOT / "docs" / "kaizen_ledger.json"
    if ledger_path.exists():
        try:
            from eta_engine.brain.jarvis_v3.kaizen import KaizenLedger
            ledger = KaizenLedger.load(ledger_path)
            tickets = sorted(ledger.tickets(), key=lambda t: t.opened_at, reverse=True)[:7]
            payload["kaizen_recent"] = [
                {"id": t.id, "title": t.title, "status": t.status.value,
                 "impact": t.impact, "opened": t.opened_at.isoformat()}
                for t in tickets
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("kaizen ledger load failed: %s", exc)

    # Today's verdict-stream summary (compact; no per-event)
    try:
        from eta_engine.obs.jarvis_today_verdicts import aggregate_today
        agg = aggregate_today()
        payload["todays_verdicts"] = {
            "totals": agg.get("totals", {}),
            "avg_conditional_cap": agg.get("avg_conditional_cap"),
            "policy_versions_seen": agg.get("policy_versions_seen", []),
            # Tier-3 #14 (2026-04-27): hourly heatmap for the panel
            "hourly_timeline": agg.get("hourly_timeline", []),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("verdict aggregation failed: %s", exc)

    return payload


HTML_TEMPLATE = """<!doctype html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>ETA Engine -- Investor Dashboard</title>
  <style>
    body { font-family: 'Inter', system-ui, sans-serif; margin: 0; padding: 2rem;
           background: #08080f; color: #e2e2eb; }
    h1   { font-weight: 600; letter-spacing: -0.02em; margin: 0 0 0.5rem; }
    .sub { color: #888; font-size: 0.9rem; margin-bottom: 2rem; }
    .card { background: #14141d; border: 1px solid #222; border-radius: 8px;
            padding: 1.25rem; margin-bottom: 1.25rem; }
    .grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
    .metric-label { font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }
    .metric-value { font-family: 'JetBrains Mono', ui-monospace, monospace;
                    font-size: 1.5rem; font-weight: 600; margin: 0.2rem 0; }
    .ticket { border-bottom: 1px solid #1f1f2a; padding: 0.75rem 0; }
    .ticket:last-child { border: 0; }
    .ticket-title { font-weight: 500; margin: 0 0 0.25rem; }
    .ticket-meta { font-size: 0.8rem; color: #888; }
    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-family: ui-monospace, monospace; }
    td, th { padding: 0.4rem 0.6rem; text-align: left; border-bottom: 1px solid #1f1f2a; }
    .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
             font-size: 0.7rem; background: #233; color: #6cf; }
    .footer { color: #555; font-size: 0.75rem; margin-top: 2rem; }
  </style>
</head>
<body>
  <h1>ETA Engine</h1>
  <div class="sub">Inner-Circle / Investor Dashboard &mdash; generated {generated_at}</div>

  <div class="grid">
    <div class="card">
      <div class="metric-label">Fleet size</div>
      <div class="metric-value">{fleet_size} bots</div>
      <div class="ticket-meta">Mode: <span class="badge">{mode}</span></div>
    </div>
    <div class="card">
      <div class="metric-label">JARVIS verdicts today</div>
      <div class="metric-value">{verdict_total}</div>
      <div class="ticket-meta">Avg conditional cap: {avg_cap}</div>
    </div>
    <div class="card">
      <div class="metric-label">Policy version</div>
      <div class="metric-value">v{policy_version}</div>
      <div class="ticket-meta">Versions seen today: {pv_seen}</div>
    </div>
  </div>

  <div class="card">
    <h3 style="margin-top:0">Active fleet</h3>
    <table>
      <tr><th>#</th><th>Bot</th></tr>
      {fleet_rows}
    </table>
  </div>

  <div class="card">
    <h3 style="margin-top:0">Recent kaizen +1 tickets</h3>
    {kaizen_rows}
  </div>

  <div class="card">
    <h3 style="margin-top:0">JARVIS verdict heatmap (today, by hour ET)</h3>
    <div style="font-family: ui-monospace, monospace; font-size: 0.75rem;">
      {heatmap_rows}
    </div>
  </div>

  <div class="footer">
    ETA Engine &middot; private &middot; not investment advice &middot;
    do not redistribute
  </div>
</body>
</html>
"""


def render_html(payload: dict[str, Any]) -> str:
    fleet_rows = "\n".join(
        f"<tr><td>{i+1}</td><td>{name}</td></tr>"
        for i, name in enumerate(payload["fleet"]["names"])
    )
    kaizen = payload.get("kaizen_recent") or []
    if kaizen:
        kaizen_rows = "\n".join(
            f'<div class="ticket"><div class="ticket-title">{t["title"]}</div>'
            f'<div class="ticket-meta">{t["id"]} &middot; impact: {t["impact"]} &middot; '
            f'status: {t["status"]} &middot; opened: {t["opened"][:10]}</div></div>'
            for t in kaizen
        )
    else:
        kaizen_rows = '<div class="ticket-meta">No kaizen ledger yet -- run scripts/run_kaizen_close_cycle.py.</div>'

    totals = payload.get("todays_verdicts", {}).get("totals", {})
    verdict_total = sum(totals.values()) if totals else 0

    # Heatmap rows: one row per hour, simple text bars
    timeline = payload.get("todays_verdicts", {}).get("hourly_timeline", [])
    if timeline:
        max_n = max(
            max(int(row.get("approved", 0)), int(row.get("conditional", 0)),
                int(row.get("rejected", 0)))
            for row in timeline
        ) or 1
        heatmap_rows = "<br>".join(
            f"  {row['hr']}:00&nbsp; "
            f"<span style='color:#2ECC71'>{'&block;' * int(int(row.get('approved', 0)) / max_n * 18)}</span>"
            f"<span style='color:#F1C40F'>{'&block;' * int(int(row.get('conditional', 0)) / max_n * 18)}</span>"
            f"<span style='color:#E74C3C'>{'&block;' * int(int(row.get('rejected', 0)) / max_n * 18)}</span>"
            for row in timeline
        )
    else:
        heatmap_rows = '<span style="color:#888">no verdicts today</span>'

    return HTML_TEMPLATE.format(
        generated_at=payload["generated_at"][:19] + "Z",
        fleet_size=payload["fleet"]["size"],
        mode=payload["fleet"]["mode"],
        verdict_total=verdict_total,
        avg_cap=payload.get("todays_verdicts", {}).get("avg_conditional_cap", "n/a"),
        policy_version=payload["policy_version"],
        pv_seen=", ".join(str(v) for v in payload.get("todays_verdicts", {}).get("policy_versions_seen", [])) or "—",
        fleet_rows=fleet_rows,
        kaizen_rows=kaizen_rows,
        heatmap_rows=heatmap_rows,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path,
                   default=ROOT / "state" / "investor_dashboard" / "index.html")
    p.add_argument("--json-payload", type=Path, default=None,
                   help="Also dump the raw JSON payload to this path (for API consumers)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    payload = gather_payload()
    html = render_html(payload)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    logger.info("wrote %s (%d bytes)", args.output, len(html))

    if args.json_payload:
        args.json_payload.parent.mkdir(parents=True, exist_ok=True)
        args.json_payload.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("wrote %s (%d bytes)", args.json_payload, len(json.dumps(payload)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
