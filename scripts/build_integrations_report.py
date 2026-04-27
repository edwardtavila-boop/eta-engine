"""
EVOLUTIONARY TRADING ALGO  //  scripts.build_integrations_report
====================================================
Produce the integration topology snapshot consumed by the
firm-tracker Command Center artifact.

What it does
------------
Invokes :func:`eta_engine.funnel.integrations.build_integrations_report`
with an optional live-status overlay (``--live-status FILE``) and writes
two outputs under ``docs/``:

  * ``integrations_latest.json``   -- full IntegrationsReport (JSON)
  * ``integrations_latest.txt``    -- 80-col human summary

The JSON file is the single source of truth for the Command Center's
``Integrations`` tab -- it spells out what venues exist, which bot
routes to which funnel layer, the sweep / kill thresholds on each
layer, the onramp triples, and which observability surfaces are live.

Usage
-----
    python -m eta_engine.scripts.build_integrations_report
    python -m eta_engine.scripts.build_integrations_report \
        --out-dir docs/ \
        --live-status docs/integrations_live_status.json

The ``--live-status`` file is an optional JSON object merged on top of
the canonical topology. Known keys (all optional):

    {
      "bots":          { "<bot_name>":   {"status": "...", "notes": "..."} },
      "venues":        { "<venue_name>": {"status": "...", "notes": "..."} },
      "observability": { "<obs_name>":   {"status": "..."} },
      "summary":       { "<key>": <value> }
    }

If the live-status file is missing or unreadable the script writes
the canonical topology unchanged and exits 0 (safe for cron).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.funnel.integrations import (  # noqa: E402
    IntegrationsReport,
    build_integrations_report,
    render_text,
)

DEFAULT_OUT_DIR = ROOT / "docs"
DEFAULT_LIVE_STATUS = ROOT / "docs" / "integrations_live_status.json"

logger = logging.getLogger("build_integrations_report")


def _load_live_status(path: Path) -> dict[str, Any] | None:
    """Load optional live-status overlay; None on any failure."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "live-status file %s unreadable (%s); using canonical topology only",
            path,
            exc,
        )
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "live-status file %s is not a JSON object; ignored",
            path,
        )
        return None
    return raw


def _write_outputs(report: IntegrationsReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "integrations_latest.json"
    txt_path = out_dir / "integrations_latest.txt"
    payload = report.model_dump(mode="json")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    txt_path.write_text(render_text(report), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eta_engine.scripts.build_integrations_report",
        description=(
            "Emit the integration topology snapshot "
            "(venues / bots / funnel layers / onramps / staking / "
            "observability) for the Command Center."
        ),
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Directory for integrations_latest.json + .txt (default: docs/).",
    )
    p.add_argument(
        "--live-status",
        type=Path,
        default=DEFAULT_LIVE_STATUS,
        help="Optional JSON overlay with live venue/bot/obs statuses.",
    )
    p.add_argument(
        "--onramp-per-txn-usd",
        type=float,
        default=10_000.0,
        help="Per-transaction USD cap surfaced on onramp rows (default: 10000).",
    )
    p.add_argument(
        "--onramp-monthly-usd",
        type=float,
        default=50_000.0,
        help="Monthly USD cap surfaced on onramp rows (default: 50000).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="logging level (default: INFO).",
    )
    p.add_argument(
        "--print",
        dest="print_text",
        action="store_true",
        help="Also print the text rendering to stdout after writing.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    live_status = _load_live_status(args.live_status)
    report = build_integrations_report(
        live_status=live_status,
        onramp_per_txn_limit_usd=float(args.onramp_per_txn_usd),
        onramp_monthly_limit_usd=float(args.onramp_monthly_usd),
    )

    _write_outputs(report, args.out_dir)

    logger.info(
        "wrote %s + %s (venues=%d bots=%d layers=%d onramps=%d staking=%d obs=%d)",
        (args.out_dir / "integrations_latest.json").as_posix(),
        (args.out_dir / "integrations_latest.txt").as_posix(),
        len(report.venues),
        len(report.bots),
        len(report.funnel_layers),
        len(report.onramp_routes),
        len(report.staking),
        len(report.observability),
    )

    if args.print_text:
        sys.stdout.write(render_text(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
