from __future__ import annotations

import importlib
import json
from types import SimpleNamespace


def test_adversarial_reviewer_uses_heuristic_when_deepseek_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    import eta_engine.feeds.adversarial_reviewer as reviewer_module

    reviewer_module = importlib.reload(reviewer_module)
    reviewer = reviewer_module.AdversarialStrategyReviewer(tmp_path)

    report = reviewer.review(
        "mnq_breakout",
        "entry: breakout\nrisk: fixed\n",
        {"trades": 12},
    )

    assert report["ai_enhanced"] is False
    assert report["ai_provider"] == "heuristic"
    assert report["ai_premortem"] is None
    assert report["ai_edge_cases"] is None


def test_adversarial_reviewer_routes_ai_review_through_deepseek_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stub-deepseek-key")

    import eta_engine.brain.llm_provider as llm_provider
    import eta_engine.feeds.adversarial_reviewer as reviewer_module

    reviewer_module = importlib.reload(reviewer_module)
    calls: list[dict[str, object]] = []

    def fake_chat_completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps(
                {
                    "premortem": ["liquidity vanished"],
                    "edge_cases": ["holiday session"],
                    "recommendations": ["tighten no-trade calendar"],
                },
            ),
        )

    monkeypatch.setattr(llm_provider, "chat_completion", fake_chat_completion)
    reviewer = reviewer_module.AdversarialStrategyReviewer(tmp_path)

    report = reviewer.review("mnq_breakout", "stop_loss: atr\nentry: breakout\n", {"trades": 50})

    assert report["ai_enhanced"] is True
    assert report["ai_provider"] == "deepseek"
    assert report["ai_premortem"] == ["liquidity vanished"]
    assert report["ai_edge_cases"] == ["holiday session"]
    assert "tighten no-trade calendar" in report["recommendations"]
    assert calls
    assert calls[0]["provider"] == llm_provider.Provider.DEEPSEEK
    assert calls[0]["tier"] == llm_provider.ModelTier.OPUS
