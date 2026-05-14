"""One-shot: backfill data_source='paper' on untagged trade_closes.

Wave-25 added the data_source field but the supervisor writer never
populated it on existing records. This leaves 3,180 paper-mode trades
classified as live_unverified and excluded from production audits,
which blocks PROP_READY designation and the launch readiness gate.

This script tags records as data_source='paper' EXCEPT:
  - records that already have a non-empty data_source field
  - records whose bot_id is in TEST_BOT_IDS (so the classifier still
    flags them test_fixture)

The original file is preserved at the path specified by --backup.
Replacement is atomic (write tmp, os.replace).

Idempotent: re-running adds zero new tags since already-tagged records
are skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

TEST_BOT_IDS = frozenset(
    {
        "t1",
        "t2",
        "t3",
        "propagate_bot",
        "test_bot",
        "fake_bot",
        "mock_bot",
        "fixture_bot",
        "smoke_bot",
        "demo_bot",
    }
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ledger",
        default=r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl",
    )
    ap.add_argument("--backup", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.ledger)
    if not src.exists():
        print(f"ERROR: ledger missing: {src}", file=sys.stderr)
        return 2

    if args.backup is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = src.with_name(src.name + f".preBackfill.{ts}.bak")
    else:
        backup = Path(args.backup)

    if not args.dry_run and not backup.exists():
        shutil.copy2(src, backup)

    tmp = src.with_name(src.name + ".backfill.tmp")

    n_total = 0
    n_tagged = 0
    n_already = 0
    n_skipped_test = 0
    n_skipped_unparseable = 0
    per_bot_tagged: dict[str, int] = {}

    with src.open("r", encoding="utf-8") as fin, tmp.open(
        "w", encoding="utf-8", newline="\n"
    ) as fout:
        for raw in fin:
            n_total += 1
            line = raw.rstrip("\n").rstrip("\r")
            if not line:
                fout.write("\n")
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                n_skipped_unparseable += 1
                fout.write(line + "\n")
                continue
            existing = str(obj.get("data_source") or "").strip().lower()
            if existing:
                n_already += 1
                fout.write(line + "\n")
                continue
            bid = str(obj.get("bot_id") or "").strip()
            if bid in TEST_BOT_IDS:
                n_skipped_test += 1
                fout.write(line + "\n")
                continue
            obj["data_source"] = "paper"
            n_tagged += 1
            per_bot_tagged[bid] = per_bot_tagged.get(bid, 0) + 1
            # Use compact JSONL separators (",", ":") to match the rest of the
            # ledger. The default (", ", ": ") would write spaces after commas
            # and colons, breaking string-equality dedup tools and producing
            # inconsistent format vs the canonical-writer output.
            fout.write(json.dumps(obj, separators=(",", ":")) + "\n")

    if args.dry_run:
        tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, src)

    print(f"ledger      : {src}")
    print(f"backup      : {backup}")
    print(f"total       : {n_total}")
    print(f"newly_tagged: {n_tagged}")
    print(f"already_tag : {n_already}")
    print(f"skipped_test: {n_skipped_test}")
    print(f"unparseable : {n_skipped_unparseable}")
    print(f"dry_run     : {args.dry_run}")
    print()
    print("top 15 tagged bots:")
    for bid, n in sorted(per_bot_tagged.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {bid:30s} {n:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
