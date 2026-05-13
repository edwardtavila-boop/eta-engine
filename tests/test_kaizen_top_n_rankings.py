"""Regression tests for kaizen_loop._build_top_n_rankings.

Operator-reported gap (2026-05-13): ``elite_summary.top5_elite`` and
``elite_summary.top5_dark`` were both empty `[]` even though
``tier_counts.ELITE = 6`` — proving the underlying scoreboard found
elite bots but the ranking aggregator wasn't building the arrays.

Root cause: kaizen_loop forwarded ``total_closes`` and ``tier_counts``
to the report's ``elite_summary`` but never computed the top-5 arrays.
Fix: new ``_build_top_n_rankings(elite, n=5)`` helper computes both
from the per-bot scoreboard metrics, using ``sum_r = expectancy_r × n``
as the ranking score.
"""

from __future__ import annotations

from eta_engine.scripts.kaizen_loop import _build_top_n_rankings


def test_returns_empty_when_no_bots() -> None:
    top, dark = _build_top_n_rankings({"bots": {}})
    assert top == []
    assert dark == []


def test_returns_empty_when_bots_missing() -> None:
    top, dark = _build_top_n_rankings({"total_closes": 0})
    assert top == []
    assert dark == []


def test_returns_empty_when_bots_not_dict() -> None:
    top, dark = _build_top_n_rankings({"bots": "not a dict"})
    assert top == []
    assert dark == []


def test_filters_out_low_sample_bots() -> None:
    """Bots with < 5 trades are excluded — too noisy to rank meaningfully."""
    elite = {
        "bots": {
            "tiny": {"n": 2, "expectancy_r": 5.0, "tier": "ELITE"},  # lottery winner
            "real": {"n": 20, "expectancy_r": 0.3, "tier": "PRODUCER"},
        }
    }
    top, dark = _build_top_n_rankings(elite, n=5)
    assert len(top) == 1
    assert top[0]["bot_id"] == "real"


def test_ranks_by_total_r_not_just_expectancy() -> None:
    """A 0.3R × 30 trades bot ranks higher than a 1.0R × 5 trades bot
    because total R is what actually pays the operator.
    """
    elite = {
        "bots": {
            "high_expect_low_trades": {
                "n": 5,
                "expectancy_r": 1.0,
                "tier": "ELITE",
                "win_rate": 0.6,
            },
            "low_expect_high_trades": {
                "n": 30,
                "expectancy_r": 0.3,
                "tier": "PRODUCER",
                "win_rate": 0.55,
            },
        }
    }
    top, _ = _build_top_n_rankings(elite, n=5)
    # 0.3 * 30 = 9.0 beats 1.0 * 5 = 5.0
    assert top[0]["bot_id"] == "low_expect_high_trades"
    assert top[0]["score"] == 9.0
    assert top[1]["bot_id"] == "high_expect_low_trades"
    assert top[1]["score"] == 5.0


def test_top5_caps_at_n_and_dark_inverts() -> None:
    """8 bots → top5 returns 5, dark returns 5 in reverse order."""
    elite = {
        "bots": {
            f"bot_{i}": {
                "n": 20,
                "expectancy_r": 0.1 * i,
                "tier": "PRODUCER",
            }
            for i in range(1, 9)
        }
    }
    top, dark = _build_top_n_rankings(elite, n=5)
    assert len(top) == 5
    assert len(dark) == 5
    # top[0] should be bot_8 (0.8 * 20 = 16.0)
    assert top[0]["bot_id"] == "bot_8"
    # dark[0] should be the worst — bot_1 (0.1 * 20 = 2.0)
    assert dark[0]["bot_id"] == "bot_1"


def test_dark_excluded_when_total_bots_fits_under_n() -> None:
    """If we have exactly N (or fewer) qualifying bots, top5_dark stays
    empty rather than duplicating the top5_elite list."""
    elite = {
        "bots": {
            f"bot_{i}": {"n": 10, "expectancy_r": 0.2, "tier": "PRODUCER"}
            for i in range(1, 4)
        }
    }
    top, dark = _build_top_n_rankings(elite, n=5)
    assert len(top) == 3
    assert dark == []  # Don't show the same 3 bots as both top and dark


def test_includes_audit_fields_for_dashboard() -> None:
    """The dashboard renders bot_id + tier + score + win_rate + sharpe.

    Verify every returned dict carries those fields so the renderer
    doesn't have to fill blanks.
    """
    elite = {
        "bots": {
            "alpha": {
                "n": 30,
                "expectancy_r": 0.45,
                "win_rate": 0.55,
                "sharpe": 1.85,
                "max_drawdown_r": -3.5,
                "tier": "ELITE",
            }
        }
    }
    top, _dark = _build_top_n_rankings(elite, n=5)
    row = top[0]
    for field in (
        "bot_id",
        "tier",
        "score",
        "n",
        "win_rate",
        "expectancy_r",
        "sharpe",
        "max_drawdown_r",
    ):
        assert field in row, f"missing {field} in top5 row"


def test_skips_non_dict_metric_entries() -> None:
    """A garbage value in the bots dict shouldn't crash the ranker."""
    elite = {
        "bots": {
            "good": {"n": 10, "expectancy_r": 0.5, "tier": "PRODUCER"},
            "garbage": "not a dict",
            "none_metric": None,
        }
    }
    top, _ = _build_top_n_rankings(elite, n=5)
    assert len(top) == 1
    assert top[0]["bot_id"] == "good"


def test_handles_zero_or_missing_expectancy_safely() -> None:
    elite = {
        "bots": {
            "zero": {"n": 10, "expectancy_r": 0.0, "tier": "MARGINAL"},
            "missing": {"n": 10, "tier": "MARGINAL"},  # no expectancy_r at all
            "negative": {"n": 10, "expectancy_r": -0.5, "tier": "DECAY"},
        }
    }
    top, dark = _build_top_n_rankings(elite, n=5)
    # 3 qualify; sort by score descending. Both zero and missing get score 0.
    assert len(top) == 3
    # The negative bot is at the bottom
    assert top[-1]["bot_id"] == "negative"
    assert top[-1]["score"] == -5.0
