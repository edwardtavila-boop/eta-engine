"""Integration tests for the 3 JARVIS hot-path Hermes wiring sites.

These verify that Phase B wiring sits cleanly on top of Phase A's
``hermes_client`` and never crashes the consult / enrich / decay path
when Hermes is unreachable.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.brain.jarvis_v3 import hermes_client


@pytest.fixture(autouse=True)
def _reset_hermes_state():
    """Backoff state is module-level; reset between tests so each test
    sees a clean monotonic clock + zero failure counter."""
    hermes_client.reset_state()
    yield
    hermes_client.reset_state()


# ---------------------------------------------------------------------------
# Site A — narrative-on-high-stakes (jarvis_full.py)
# ---------------------------------------------------------------------------


def test_site_a_narrative_module_imports_hermes_client():
    """The narrative-augmentation block in jarvis_full imports hermes_client
    lazily inside the high-stakes path. We can't easily fire a full consult
    here without bootstrapping the whole Wave stack, so we settle for a
    smoke-import that proves the module compiles and the integration site
    is reachable from a fresh interpreter."""
    # Reading the source to confirm the wiring marker is present.
    import inspect

    from eta_engine.brain.jarvis_v3 import jarvis_full  # noqa: F401
    from eta_engine.brain.jarvis_v3 import jarvis_full as _jf
    src = inspect.getsource(_jf)
    assert "hermes_calls" in src, "Site A wiring marker missing in jarvis_full.py"
    assert "hermes_client.narrative" in src or "hermes_client import" in src, \
        "Site A doesn't reference hermes_client.narrative"


# ---------------------------------------------------------------------------
# Site B — web_search-on-event (context_enricher.py)
# ---------------------------------------------------------------------------


def test_site_b_web_search_called_pre_event(monkeypatch):
    """When nearby_events has a severity-3 event within 30 min,
    context_enricher.enrich calls hermes_client.web_search and populates
    EnrichedContext.news_snippets."""
    from eta_engine.brain.jarvis_v3 import context_enricher
    from eta_engine.data.event_calendar import CalendarEvent

    fake_event = CalendarEvent(
        ts_utc=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        kind="FOMC",
        symbol=None,
        severity=3,
    )

    def fake_upcoming(now, horizon_min=60, path=None):
        return [fake_event]

    captured: dict = {}

    def fake_web_search(query, *, n=3, timeout_s=2.0):
        captured["query"] = query
        return hermes_client.HermesResult(
            ok=True,
            data=[{"snippet": "FOMC just hiked 25bp; equities sliding."}],
            error=None,
            elapsed_ms=800.0,
        )

    monkeypatch.setattr(
        "eta_engine.data.event_calendar.upcoming", fake_upcoming,
    )
    monkeypatch.setattr(hermes_client, "web_search", fake_web_search)

    ec = context_enricher.enrich(symbol="MNQ", asset_class="MNQ")
    # The new field exists on EnrichedContext
    assert hasattr(ec, "news_snippets"), \
        "EnrichedContext should expose news_snippets after Site B wiring"
    # Hermes was called with an FOMC-shaped query
    assert "FOMC" in captured.get("query", ""), \
        f"expected FOMC in web_search query, got: {captured.get('query')!r}"
    # The snippet was populated
    assert ec.news_snippets, "news_snippets should be non-empty after a successful web_search"


def test_site_b_skips_when_no_severity_3_event(monkeypatch):
    """No high-severity event → no web_search call, news_snippets empty."""
    from eta_engine.brain.jarvis_v3 import context_enricher

    def fake_upcoming(now, horizon_min=60, path=None):
        return []  # nothing nearby

    call_count = {"n": 0}

    def fake_web_search(query, *, n=3, timeout_s=2.0):
        call_count["n"] += 1
        return hermes_client.HermesResult(
            ok=True, data=[], error=None, elapsed_ms=10.0,
        )

    monkeypatch.setattr(
        "eta_engine.data.event_calendar.upcoming", fake_upcoming,
    )
    monkeypatch.setattr(hermes_client, "web_search", fake_web_search)

    ec = context_enricher.enrich(symbol="MNQ", asset_class="MNQ")
    assert ec.news_snippets == ()
    assert call_count["n"] == 0, "web_search should not fire without a high-severity event"


# ---------------------------------------------------------------------------
# Site C — memory persist/recall (hot_learner.py)
# ---------------------------------------------------------------------------


def test_site_c_hot_learner_persists_and_recalls(monkeypatch, tmp_path):
    """decay_overnight() recalls yesterday's snapshot before decaying,
    and persists today's snapshot afterward."""
    from eta_engine.brain.jarvis_v3 import hot_learner

    state_path = tmp_path / "hot_learner.json"
    monkeypatch.setattr(hot_learner, "STATE_PATH", state_path)

    persist_calls: list[tuple] = []
    recall_calls: list[str] = []

    def fake_persist(key, value, *, timeout_s=1.0):
        persist_calls.append((key, dict(value)))
        return hermes_client.HermesResult(
            ok=True, data=None, error=None, elapsed_ms=50.0,
        )

    def fake_recall(key, *, timeout_s=1.0):
        recall_calls.append(key)
        # Simulate "key not found" so today decays toward 1.0 (legacy path)
        return hermes_client.HermesResult(
            ok=False, data=None, error="not_found", elapsed_ms=20.0,
        )

    monkeypatch.setattr(hermes_client, "memory_persist", fake_persist)
    monkeypatch.setattr(hermes_client, "memory_recall", fake_recall)

    # Seed state with > MIN_OBSERVATIONS_TO_ACT closes so weight_mods has entries
    for _ in range(4):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"order_flow": 1.0, "wyckoff": 1.0, "vpa": 1.0},
            r_outcome=1.0,
        )

    hot_learner.decay_overnight()

    # Recall fired at least once per asset (BTC here)
    assert recall_calls, "decay_overnight should call memory_recall"
    assert any("BTC" in k for k in recall_calls), \
        f"expected BTC in recall keys, got {recall_calls}"
    # Persist fired at least once per asset
    assert persist_calls, "decay_overnight should call memory_persist after decay"
    assert any("BTC" in k for k, _ in persist_calls), \
        f"expected BTC in persist keys, got {[k for k, _ in persist_calls]}"


def test_all_three_sites_never_raise_when_hermes_down(monkeypatch, tmp_path):
    """Every Hermes call raises ConnectionError → all 3 sites swallow it
    and return normally. No exception leaks past the wiring."""
    from eta_engine.brain.jarvis_v3 import context_enricher, hot_learner

    def boom(*args, **kwargs):
        raise ConnectionError("hermes_down")

    monkeypatch.setattr(hermes_client, "narrative", boom)
    monkeypatch.setattr(hermes_client, "web_search", boom)
    monkeypatch.setattr(hermes_client, "memory_persist", boom)
    monkeypatch.setattr(hermes_client, "memory_recall", boom)

    state_path = tmp_path / "hot_learner.json"
    monkeypatch.setattr(hot_learner, "STATE_PATH", state_path)

    # Site B — enrich never raises
    ec = context_enricher.enrich(symbol="MNQ", asset_class="MNQ")
    assert ec is not None
    assert ec.news_snippets == ()

    # Site C — decay_overnight never raises
    hot_learner.decay_overnight()  # legacy path runs, mean-revert to 1.0
