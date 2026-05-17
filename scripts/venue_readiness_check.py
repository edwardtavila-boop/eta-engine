"""Layer 8: Venue bridge readiness check.

Audit the venue connectors available for paper/live trading and report broker
readiness.

Covers: IBKR (primary), Tastytrade (secondary), PaperSim (always ready).
Tradovate is dormant per AGENTS.md and listed but not promoted.

Usage
-----
    python -m eta_engine.scripts.venue_readiness_check
    python -m eta_engine.scripts.venue_readiness_check --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

VENUES_DIR = ROOT / "venues"
EMPTY_MARKER = "-"
PAPERSIM_LABEL = "built-in (no venue needed)"


@dataclass
class VenueStatus:
    venue: str
    connector_present: bool
    sim_supported: bool
    live_supported: bool
    status: str  # READY / DORMANT / PARTIAL / MISSING
    path: str


def _first_path_or_empty(paths: list[Path]) -> str:
    return str(paths[0]) if paths else EMPTY_MARKER


def check_venues() -> list[VenueStatus]:
    venues_dir = VENUES_DIR
    if not venues_dir.exists():
        return []

    results: list[VenueStatus] = []

    ibkr_files = list(venues_dir.glob("**/ibkr*.py")) + list(venues_dir.glob("**/IBKR*.py"))
    ibkr_connector = any("connector" in path.stem.lower() or "router" in path.stem.lower() for path in ibkr_files)
    results.append(
        VenueStatus(
            venue="IBKR",
            connector_present=bool(ibkr_files),
            sim_supported=ibkr_connector,
            live_supported=ibkr_connector,
            status="READY" if ibkr_connector else ("PARTIAL" if ibkr_files else "MISSING"),
            path=_first_path_or_empty(ibkr_files),
        )
    )

    tasty_files = list(venues_dir.glob("**/tastytrade*.py")) + list(venues_dir.glob("**/tasty*.py"))
    tasty_connector = bool(tasty_files)
    results.append(
        VenueStatus(
            venue="Tastytrade",
            connector_present=tasty_connector,
            sim_supported=tasty_connector,
            live_supported=tasty_connector,
            status="READY" if tasty_connector else "MISSING",
            path=_first_path_or_empty(tasty_files),
        )
    )

    results.append(
        VenueStatus(
            venue="PaperSim",
            connector_present=True,
            sim_supported=True,
            live_supported=False,
            status="READY",
            path=PAPERSIM_LABEL,
        )
    )

    tradovate_files = list(venues_dir.glob("**/tradovate*.py"))
    results.append(
        VenueStatus(
            venue="Tradovate",
            connector_present=bool(tradovate_files),
            sim_supported=bool(tradovate_files),
            live_supported=False,
            status="DORMANT",
            path=_first_path_or_empty(tradovate_files),
        )
    )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="venue_readiness_check")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    venues = check_venues()
    if args.json:
        payload = {
            "venues": [asdict(venue) for venue in venues],
            "generated": datetime.now(tz=UTC).isoformat(),
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"{'Venue':<16} {'Status':<10} {'Sim':<6} {'Live':<6} {'Path'}")
    print("-" * 80)
    for venue in venues:
        print(
            f"{venue.venue:<16} {venue.status:<10} "
            f"{'YES' if venue.sim_supported else 'no':<6} "
            f"{'YES' if venue.live_supported else 'no':<6} "
            f"{venue.path}"
        )

    ready = sum(1 for venue in venues if venue.status == "READY")
    print(f"\n{ready} venue(s) ready for paper trading")
    return 0


if __name__ == "__main__":
    sys.exit(main())
