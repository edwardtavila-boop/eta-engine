"""
Engage The Firm's 6-agent board on a strategy spec.

Usage:
    python -m eta_engine.scripts.engage_firm_board \\
        --spec eta_engine/docs/firm_spec_crypto_perp.json \\
        [--live --channel crypto_strategies]

The board runs: Quant -> RedTeam -> Risk -> Macro -> Micro -> PM
Output: verdict (GO / HOLD / MODIFY / KILL) persisted to
var/eta_engine/state/kill_log.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import workspace_roots

FIRM_DIR = workspace_roots.WORKSPACE_ROOT / "firm" / "the_firm_complete"

logger = logging.getLogger("firm_engage")


def extract_firm_spec(strategy_spec: dict) -> dict:
    """Extract the firm_eval_fields subset for board input."""
    return {"spec_id": strategy_spec["spec_id"], **strategy_spec["firm_eval_fields"]}


def run_roundtable(firm_spec: dict, live: bool, channel: str) -> dict:
    """Run the firm roundtable orchestrator."""
    spec_tmp = workspace_roots.ensure_parent(workspace_roots.ETA_FIRM_BOARD_TEMP_SPEC_PATH)
    with open(spec_tmp, "w") as f:
        json.dump(firm_spec, f, indent=2)

    cmd = ["python", "scripts/run_roundtable.py", "--spec", str(spec_tmp)]
    if live:
        cmd += ["--live", "--channel", channel]

    logger.info("Invoking Firm board: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=FIRM_DIR, capture_output=True, text=True)

    logger.info("stdout: %s", result.stdout)
    if result.returncode != 0:
        logger.error("Firm board failed: %s", result.stderr)
        raise RuntimeError(result.stderr)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw_output": result.stdout}


def append_kill_log(spec_id: str, verdict: dict) -> None:
    """Persist verdict to the kill log."""
    source = workspace_roots.default_kill_log_path()
    dest = workspace_roots.ensure_parent(workspace_roots.ETA_KILL_LOG_PATH)
    if source.exists():
        with open(source) as f:
            log = json.load(f)
    else:
        log = {"meta": {}, "entries": []}
    if isinstance(log, list):
        log = {"meta": {}, "entries": log}
    elif not isinstance(log, dict):
        log = {"meta": {}, "entries": []}
    entries = log.get("entries")
    if not isinstance(entries, list):
        log["entries"] = []
    log["entries"].append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "spec_id": spec_id,
            "verdict": verdict,
        }
    )
    with open(dest, "w") as f:
        json.dump(log, f, indent=2)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--spec", required=True, help="Path to strategy spec JSON (firm_spec_crypto_perp.json)")
    p.add_argument("--live", action="store_true", help="Post to Discord")
    p.add_argument("--channel", default="crypto_strategies")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    spec_path = Path(args.spec)
    with open(spec_path) as f:
        strategy_spec = json.load(f)

    firm_spec = extract_firm_spec(strategy_spec)
    verdict = run_roundtable(firm_spec, args.live, args.channel)

    append_kill_log(strategy_spec["spec_id"], verdict)
    logger.info("Verdict appended to %s", workspace_roots.ETA_KILL_LOG_PATH)
    logger.info("Verdict summary: %s", json.dumps(verdict, indent=2)[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
