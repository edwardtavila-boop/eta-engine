"""Daily roll-up exporter to Notion / Airtable (Tier-3 #13, 2026-04-27).

Daily 23:30 ET (after kaizen + critique + bandit promotion all run).
Builds a structured digest of the day's:
  * verdict counts by subsystem
  * kaizen +1 ticket title + impact
  * top 3 denial reason codes
  * critique severity + summary
  * bandit promotable candidates (if any)

POSTs to whatever exporter env vars are configured:
  ETA_NOTION_TOKEN + ETA_NOTION_DATABASE_ID
  ETA_AIRTABLE_TOKEN + ETA_AIRTABLE_BASE_ID + ETA_AIRTABLE_TABLE_NAME

Both are optional. When neither is set, this script prints the digest
to stdout + writes it to ``state/notion_export/<date>.json`` so the
operator can copy/paste manually.

Operator sets up Notion via:
  1. Create an Internal Integration at notion.so/my-integrations
  2. Copy the Internal Integration Token -> ETA_NOTION_TOKEN
  3. Create a database, share it with the integration
  4. Copy the database id from the URL -> ETA_NOTION_DATABASE_ID
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

logger = logging.getLogger("export_to_notion")


def _build_digest(*, audit_dir: Path, kaizen_ledger: Path,
                  critique_dir: Path, bandit_dir: Path) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    digest: dict[str, Any] = {
        "date": today.isoformat(),
        "ts": datetime.now(UTC).isoformat(),
        "verdicts_today": {},
        "kaizen_plus_one": None,
        "critique_severity": None,
        "promotable_candidates": [],
    }

    # Verdicts today
    try:
        from eta_engine.obs.jarvis_today_verdicts import aggregate_today
        agg = aggregate_today()
        digest["verdicts_today"] = {
            "totals": agg.get("totals", {}),
            "top_denial_reasons": agg.get("top_denial_reasons", [])[:3],
            "avg_conditional_cap": agg.get("avg_conditional_cap"),
        }
    except Exception as exc:  # noqa: BLE001
        digest["verdicts_today"] = {"error": str(exc)}

    # Latest kaizen ticket
    try:
        from eta_engine.brain.jarvis_v3.kaizen import KaizenLedger
        if kaizen_ledger.exists():
            ledger = KaizenLedger.load(kaizen_ledger)
            tickets = sorted(ledger.tickets(), key=lambda t: t.opened_at, reverse=True)
            if tickets:
                t = tickets[0]
                digest["kaizen_plus_one"] = {
                    "id": t.id,
                    "title": t.title,
                    "impact": t.impact,
                    "status": t.status.value,
                }
    except Exception as exc:  # noqa: BLE001
        digest["kaizen_plus_one"] = {"error": str(exc)}

    # Today's critique
    today_critique = critique_dir / f"{today.isoformat()}.json"
    if today_critique.exists():
        try:
            data = json.loads(today_critique.read_text(encoding="utf-8"))
            digest["critique_severity"] = {
                "severity": data.get("severity"),
                "summary": data.get("summary"),
            }
        except (json.JSONDecodeError, OSError):
            pass

    # Today's bandit promotion check
    today_bandit = bandit_dir / f"promotion_check_{today.isoformat()}.json"
    if today_bandit.exists():
        try:
            data = json.loads(today_bandit.read_text(encoding="utf-8"))
            digest["promotable_candidates"] = [
                pr["candidate"] for pr in data.get("promotable", [])
            ]
        except (json.JSONDecodeError, OSError):
            pass

    return digest


def _post_notion(token: str, database_id: str, digest: dict[str, Any]) -> bool:
    """POST one row to a Notion database."""
    body = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name": {
                "title": [{"text": {"content": f"ETA Engine — {digest['date']}"}}]
            },
            "Date": {"date": {"start": digest["date"]}},
            "Kaizen +1": {
                "rich_text": [{
                    "text": {
                        "content": (digest.get("kaizen_plus_one") or {}).get("title", "(none)")[:1900]
                    }
                }]
            },
            "Critique Severity": {
                "rich_text": [{
                    "text": {
                        "content": str((digest.get("critique_severity") or {}).get("severity", "—"))
                    }
                }]
            },
            "Promotable": {
                "rich_text": [{
                    "text": {
                        "content": ", ".join(digest.get("promotable_candidates", [])) or "—"
                    }
                }]
            },
            "Verdicts Total": {
                "number": sum((digest.get("verdicts_today") or {}).get("totals", {}).values())
            },
        },
    }
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Notion-Version": "2022-06-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        logger.warning("Notion POST failed: %s", exc)
        return False


def _post_airtable(token: str, base_id: str, table_name: str,
                   digest: dict[str, Any]) -> bool:
    body = {
        "fields": {
            "Date": digest["date"],
            "Kaizen +1": (digest.get("kaizen_plus_one") or {}).get("title", "(none)"),
            "Critique": str((digest.get("critique_severity") or {}).get("severity", "—")),
            "Promotable": ", ".join(digest.get("promotable_candidates", [])) or "—",
            "Verdicts Total": sum((digest.get("verdicts_today") or {}).get("totals", {}).values()),
        }
    }
    url = f"https://api.airtable.com/v0/{base_id}/{table_name}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        logger.warning("Airtable POST failed: %s", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit-dir", type=Path, default=ROOT / "state" / "jarvis_audit")
    p.add_argument("--kaizen-ledger", type=Path, default=ROOT / "docs" / "kaizen_ledger.json")
    p.add_argument("--critique-dir", type=Path, default=ROOT / "state" / "kaizen_critique")
    p.add_argument("--bandit-dir", type=Path, default=ROOT / "state" / "bandit")
    p.add_argument("--out-dir", type=Path, default=ROOT / "state" / "notion_export")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    digest = _build_digest(
        audit_dir=args.audit_dir,
        kaizen_ledger=args.kaizen_ledger,
        critique_dir=args.critique_dir,
        bandit_dir=args.bandit_dir,
    )

    if not args.dry_run:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = args.out_dir / f"{digest['date']}.json"
        out_path.write_text(json.dumps(digest, indent=2), encoding="utf-8")
        logger.info("wrote local digest -> %s", out_path)

    # Notion
    notion_token = os.environ.get("ETA_NOTION_TOKEN", "")
    notion_db    = os.environ.get("ETA_NOTION_DATABASE_ID", "")
    if notion_token and notion_db:
        ok = True if args.dry_run else _post_notion(notion_token, notion_db, digest)
        logger.info("notion: %s", "OK" if ok else "FAIL")
    else:
        logger.info("notion: not configured (set ETA_NOTION_TOKEN + ETA_NOTION_DATABASE_ID)")

    # Airtable
    air_token = os.environ.get("ETA_AIRTABLE_TOKEN", "")
    air_base  = os.environ.get("ETA_AIRTABLE_BASE_ID", "")
    air_table = os.environ.get("ETA_AIRTABLE_TABLE_NAME", "")
    if air_token and air_base and air_table:
        ok = True if args.dry_run else _post_airtable(air_token, air_base, air_table, digest)
        logger.info("airtable: %s", "OK" if ok else "FAIL")
    else:
        logger.info("airtable: not configured")

    if args.dry_run:
        print(json.dumps(digest, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
