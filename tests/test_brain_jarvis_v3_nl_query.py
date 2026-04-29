from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_v3.nl_query import (
    count_verdict,
    dispatch,
    health,
    list_verdict,
    reason_freq,
    subsystem_stats,
    why_verdict,
)


def _write_audit(path, records: list[dict[str, object]]) -> None:
    lines = [json.dumps(record, default=str) for record in records]
    lines.insert(1, "{bad json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_nl_query_intents_ignore_bad_lines_and_answer_by_request(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    now = datetime(2026, 4, 29, tzinfo=UTC)
    _write_audit(
        audit,
        [
            {
                "ts": now.isoformat(),
                "request_id": "abc123",
                "verdict": "DENIED",
                "reason": "kill switch active",
                "reason_code": "kill_blocks_all",
                "stress_composite": 0.9,
                "subsystem": "bot.mnq",
            },
            {
                "ts": (now - timedelta(hours=2)).isoformat(),
                "request_id": "def456",
                "verdict": "APPROVED",
                "reason": "routine",
                "reason_code": "ok",
                "subsystem": "bot.mnq",
            },
        ],
    )

    why = why_verdict(audit, "abc123")
    counts = count_verdict(audit, "DENIED", hours=24)
    listed = list_verdict(audit, "APPROVED", subsystem="bot.mnq", hours=24)

    assert why.summary.startswith("DENIED -- kill switch active")
    assert why.stats["stress_composite"] == 0.9
    assert counts.stats == {"count": 1, "total": 2}
    assert [record["request_id"] for record in listed.records] == ["def456"]


def test_nl_query_rollups_and_dispatch_grammar(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    now = datetime(2026, 4, 29, tzinfo=UTC)
    _write_audit(
        audit,
        [
            {
                "ts": now.isoformat(),
                "request_id": "aaa111",
                "verdict": "DENIED",
                "reason_code": "risk",
                "subsystem": "bot.btc",
                "binding_constraint": "macro",
            },
            {
                "ts": now.isoformat(),
                "request_id": "bbb222",
                "verdict": "DENIED",
                "reason_code": "risk",
                "subsystem": "bot.btc",
                "binding_constraint": "macro",
            },
        ],
    )

    reasons = reason_freq(audit)
    stats = subsystem_stats(audit, "bot.btc")
    parsed = dispatch(audit, "list DENIED from bot.btc")
    fallback = dispatch(audit, "what pizza topping is best?")

    assert reasons.records[0] == {"reason_code": "risk", "count": 2}
    assert stats.stats["DENIED"] == 2.0
    assert parsed.intent == "LIST_VERDICT"
    assert len(parsed.records) == 2
    assert fallback.intent == "UNPARSED"


def test_nl_query_health_handles_empty_log(tmp_path) -> None:
    audit = tmp_path / "missing.jsonl"

    result = health(audit)

    assert result.intent == "HEALTH"
    assert result.summary == "no audit records -- JARVIS has not decided anything yet"
