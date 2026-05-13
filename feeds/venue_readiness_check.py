"""Layer 8: Venue bridge readiness check. Audits the venue connectors
available for paper/live trading and reports broker readiness.

Covers: IBKR (primary), Tastytrade (secondary), PaperSim (always ready).
Tradovate is dormant per AGENTS.md — listed but not promoted.

Usage
-----
    python -m eta_engine.scripts.venue_readiness_check
    python -m eta_engine.scripts.venue_readiness_check --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

VENUES_DIR = ROOT / "venues"


@dataclass
class VenueStatus:
    venue: str
    connector_present: bool
    sim_supported: bool
    live_supported: bool
    status: str  # READY / DORMANT / MISSING
    path: str


def check_venues() -> list[VenueStatus]:
    results: list[VenueStatus] = []
    venues_dir = VENUES_DIR
    if not venues_dir.exists():
        return results

    # IBKR
    ibkr_files = list(venues_dir.glob("**/ibkr*.py")) + list(venues_dir.glob("**/IBKR*.py"))
    ibkr_connector = any("connector" in f.stem.lower() or "router" in f.stem.lower() for f in ibkr_files)
    results.append(
        VenueStatus(
            "IBKR",
            bool(ibkr_files),
            ibkr_connector,
            ibkr_connector,
            "READY" if ibkr_connector else ("PARTIAL" if ibkr_files else "MISSING"),
            str(ibkr_files[0]) if ibkr_files else "—",
        )
    )

    # Tastytrade
    tt_files = list(venues_dir.glob("**/tastytrade*.py")) + list(venues_dir.glob("**/tasty*.py"))
    tt_connector = bool(tt_files)
    results.append(
        VenueStatus(
            "Tastytrade",
            tt_connector,
            tt_connector,
            tt_connector,
            "READY" if tt_connector else "MISSING",
            str(tt_files[0]) if tt_files else "—",
        )
    )

    # Paper sim — always ready
    results.append(VenueStatus("PaperSim", True, True, False, "READY", "built-in (no venue needed)"))

    # Tradovate — dormant
    tv_files = list(venues_dir.glob("**/tradovate*.py"))
    results.append(
        VenueStatus(
            "Tradovate",
            bool(tv_files),
            bool(tv_files),
            False,
            "DORMANT",
            str(tv_files[0]) if tv_files else "—",
        )
    )

    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="venue_readiness_check")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    venues = check_venues()
    if args.json:
        out = {
            "venues": [
                {
                    "venue": v.venue,
                    "connector_present": v.connector_present,
                    "sim_supported": v.sim_supported,
                    "live_supported": v.live_supported,
                    "status": v.status,
                    "path": v.path,
                }
                for v in venues
            ],
            "generated": datetime.now(tz=UTC).isoformat(),
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"{'Venue':<16} {'Status':<10} {'Sim':<6} {'Live':<6} {'Path'}")
        print("-" * 80)
        for v in venues:
            print(
                f"{v.venue:<16} {v.status:<10} {'YES' if v.sim_supported else 'no':<6} {'YES' if v.live_supported else 'no':<6} {v.path}"
            )
        ready = sum(1 for v in venues if v.status == "READY")
        print(f"\n{ready} venue(s) ready for paper trading")
    return 0


if __name__ == "__main__":
    sys.exit(main())
