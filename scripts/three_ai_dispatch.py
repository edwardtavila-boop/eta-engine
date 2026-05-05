"""One-shot three-AI coordination dispatch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from eta_engine.scripts import three_ai_daemon, workspace_roots  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=workspace_roots.ETA_RUNTIME_STATE_DIR)
    parser.add_argument("--max-tokens", type=int, default=three_ai_daemon.DEFAULT_MAX_TOKENS)
    args = parser.parse_args(argv)

    report = three_ai_daemon.run_coordination_cycle(max_tokens=args.max_tokens)
    paths = three_ai_daemon.write_report(report, state_root=args.state_root)

    print("=" * 60)
    print("  THREE-AI PARALLEL DISPATCH")
    print("=" * 60)
    three_ai_daemon.print_cycle_summary(report)
    print(f"  Latest: {paths['latest']}")
    print("=" * 60)
    return 0 if report["status"] in {"complete", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
