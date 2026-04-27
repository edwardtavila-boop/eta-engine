"""Tests for obs.supabase_sink — Supabase mirror for the decision journal.

Network is mocked. We verify:
  * is_configured() respects env vars
  * post_event() returns False when not configured (no network call)
  * post_event() builds the right payload + headers
  * post_event() swallows errors (fire-and-forget contract)
  * DecisionJournal.append() invokes the sink (when supabase_mirror=True)
  * DecisionJournal.append() does NOT invoke the sink when mirror disabled
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest import mock

import pytest

from eta_engine.obs import supabase_sink
from eta_engine.obs.decision_journal import (
    Actor,
    DecisionJournal,
    JournalEvent,
    Outcome,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETA_SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("ETA_SUPABASE_ANON_KEY", "test_key_xyz")


@pytest.fixture()
def env_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETA_SUPABASE_URL", raising=False)
    monkeypatch.delenv("ETA_SUPABASE_ANON_KEY", raising=False)


@pytest.fixture()
def sample_event() -> JournalEvent:
    return JournalEvent(
        ts=datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC),
        actor=Actor.TRADE_ENGINE,
        intent="open_mnq_long",
        rationale="confluence_score=0.82 above threshold",
        gate_checks=["+confluence", "+session_open", "-no_macro_event"],
        outcome=Outcome.EXECUTED,
        links=["trade_id:t-1234"],
        metadata={"strategy": "mnq_v3", "lot_size": 1},
    )


# ---------------------------------------------------------------------------
# is_configured
# ---------------------------------------------------------------------------


def test_is_configured_true_when_both_env_set(env_configured: None) -> None:
    assert supabase_sink.is_configured() is True


def test_is_configured_false_when_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETA_SUPABASE_URL", raising=False)
    monkeypatch.setenv("ETA_SUPABASE_ANON_KEY", "key")
    assert supabase_sink.is_configured() is False


def test_is_configured_false_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETA_SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.delenv("ETA_SUPABASE_ANON_KEY", raising=False)
    assert supabase_sink.is_configured() is False


# ---------------------------------------------------------------------------
# post_event
# ---------------------------------------------------------------------------


def test_post_event_no_op_when_unconfigured(
    env_unconfigured: None,
    sample_event: JournalEvent,
) -> None:
    with mock.patch("urllib.request.urlopen") as urlopen:
        result = supabase_sink.post_event(sample_event)
    assert result is False
    urlopen.assert_not_called()


def test_post_event_success_path(
    env_configured: None,
    sample_event: JournalEvent,
) -> None:
    fake_resp = mock.MagicMock()
    fake_resp.status = 201
    fake_resp.read.return_value = b""
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with mock.patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        result = supabase_sink.post_event(sample_event)

    assert result is True
    urlopen.assert_called_once()
    req = urlopen.call_args[0][0]
    assert req.method == "POST"
    assert req.full_url == "https://test.supabase.co/rest/v1/decision_journal"
    assert req.headers["Apikey"] == "test_key_xyz"
    body = json.loads(req.data.decode("utf-8"))
    assert body["actor"] == "TRADE_ENGINE"
    assert body["intent"] == "open_mnq_long"
    assert body["outcome"] == "EXECUTED"
    assert body["gate_checks"] == ["+confluence", "+session_open", "-no_macro_event"]
    assert body["metadata"] == {"strategy": "mnq_v3", "lot_size": 1}


def test_post_event_swallows_http_error(
    env_configured: None,
    sample_event: JournalEvent,
) -> None:
    import urllib.error
    err = urllib.error.HTTPError(
        url="https://test.supabase.co/rest/v1/decision_journal",
        code=500,
        msg="Internal Server Error",
        hdrs={},  # type: ignore[arg-type]
        fp=None,
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        result = supabase_sink.post_event(sample_event)
    assert result is False  # error was swallowed; no exception bubbled up


def test_post_event_swallows_url_error(
    env_configured: None,
    sample_event: JournalEvent,
) -> None:
    import urllib.error
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Network unreachable"),
    ):
        result = supabase_sink.post_event(sample_event)
    assert result is False


def test_post_event_swallows_unexpected_exception(
    env_configured: None,
    sample_event: JournalEvent,
) -> None:
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        result = supabase_sink.post_event(sample_event)
    assert result is False


# ---------------------------------------------------------------------------
# DecisionJournal integration
# ---------------------------------------------------------------------------


def test_decision_journal_append_invokes_sink_by_default(
    env_configured: None,
    tmp_path,
    sample_event: JournalEvent,
) -> None:
    journal = DecisionJournal(tmp_path / "j.jsonl")
    with mock.patch.object(supabase_sink, "post_event") as post:
        journal.append(sample_event)
    post.assert_called_once_with(sample_event)


def test_decision_journal_append_skips_sink_when_disabled(
    env_configured: None,
    tmp_path,
    sample_event: JournalEvent,
) -> None:
    journal = DecisionJournal(tmp_path / "j.jsonl", supabase_mirror=False)
    with mock.patch.object(supabase_sink, "post_event") as post:
        journal.append(sample_event)
    post.assert_not_called()


def test_decision_journal_local_write_independent_of_sink(
    env_configured: None,
    tmp_path,
    sample_event: JournalEvent,
) -> None:
    """Sink failure must never lose local JSONL writes."""
    journal = DecisionJournal(tmp_path / "j.jsonl")
    with mock.patch.object(supabase_sink, "post_event", side_effect=RuntimeError("won't bubble")):
        with pytest.raises(RuntimeError):
            journal.append(sample_event)
    # Even with sink raising, the local line was written before the sink call.
    lines = (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["intent"] == "open_mnq_long"
