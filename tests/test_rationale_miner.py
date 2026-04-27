"""Tests for brain.rationale_miner -- cluster rationales -> outcomes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eta_engine.brain.rationale_miner import (
    RationaleMiner,
    RationaleRecord,
    coverage,
    ngrams,
    phrase_frequency,
    tokenize_rationale,
    winners_minus_losers,
)

# --------------------------------------------------------------------------- #
# tokenize_rationale
# --------------------------------------------------------------------------- #


def test_tokenize_lowercases_and_strips_punctuation() -> None:
    assert tokenize_rationale("Fade Open Gap-up!") == ["fade", "open", "gap", "up"]


def test_tokenize_drops_stopwords() -> None:
    toks = tokenize_rationale("This is the fade of a trending day")
    assert "this" not in toks
    assert "is" not in toks
    assert "the" not in toks
    assert "fade" in toks
    assert "trending" in toks


def test_tokenize_drops_single_letters() -> None:
    assert "a" not in tokenize_rationale("a trending day")


def test_tokenize_empty_string() -> None:
    assert tokenize_rationale("") == []


def test_tokenize_only_symbols() -> None:
    assert tokenize_rationale("...!@#$%") == []


# --------------------------------------------------------------------------- #
# ngrams
# --------------------------------------------------------------------------- #


def test_ngrams_bigrams() -> None:
    toks = ["fade", "open", "gap"]
    assert ngrams(toks, n=2) == ["fade open", "open gap"]


def test_ngrams_trigrams() -> None:
    toks = ["fade", "open", "gap", "up"]
    assert ngrams(toks, n=3) == ["fade open gap", "open gap up"]


def test_ngrams_too_short() -> None:
    assert ngrams(["a"], n=2) == []


def test_ngrams_zero_n() -> None:
    assert ngrams(["a", "b"], n=0) == []


# --------------------------------------------------------------------------- #
# RationaleRecord validation
# --------------------------------------------------------------------------- #


def test_record_rejects_empty_trade_id() -> None:
    with pytest.raises(ValidationError):
        RationaleRecord(trade_id="", rationale="x", r_captured=0.0)


def test_record_grade_bounded() -> None:
    with pytest.raises(ValidationError):
        RationaleRecord(
            trade_id="x",
            rationale="y",
            r_captured=1.0,
            grade_total=200.0,
        )


def test_record_grade_optional() -> None:
    r = RationaleRecord(trade_id="x", rationale="y", r_captured=1.0)
    assert r.grade_total is None


# --------------------------------------------------------------------------- #
# Miner validation
# --------------------------------------------------------------------------- #


def test_miner_rejects_bad_min_cluster_size() -> None:
    with pytest.raises(ValueError):
        RationaleMiner(min_cluster_size=0)


def test_miner_rejects_bad_max_ngram() -> None:
    with pytest.raises(ValueError):
        RationaleMiner(max_ngram=4)


# --------------------------------------------------------------------------- #
# Mining empty + trivial
# --------------------------------------------------------------------------- #


def test_mine_empty_returns_empty_report() -> None:
    rep = RationaleMiner().mine([])
    assert rep.n_records == 0
    assert rep.clusters == []
    assert rep.top_winners == []
    assert rep.top_losers == []


def test_mine_with_min_cluster_size_not_met_drops_all_clusters() -> None:
    recs = [RationaleRecord(trade_id=f"t-{i}", rationale=f"unique phrase {i}", r_captured=1.0) for i in range(3)]
    miner = RationaleMiner(min_cluster_size=5)
    rep = miner.mine(recs)
    assert rep.clusters == []
    # None matched any kept cluster
    assert set(rep.uncategorized) == {"t-0", "t-1", "t-2"}


# --------------------------------------------------------------------------- #
# Core clustering
# --------------------------------------------------------------------------- #


def test_mine_finds_shared_phrase_cluster() -> None:
    recs = [
        RationaleRecord(trade_id="t-1", rationale="fade open gap up", r_captured=1.5),
        RationaleRecord(trade_id="t-2", rationale="fade open gap down", r_captured=2.0),
        RationaleRecord(trade_id="t-3", rationale="fade open range", r_captured=-0.5),
    ]
    miner = RationaleMiner(min_cluster_size=3)
    rep = miner.mine(recs)
    # "fade" and "open" and "fade open" all appear in all 3 records
    phrases = {c.phrase for c in rep.clusters}
    assert "fade" in phrases
    assert "open" in phrases


def test_cluster_stats_correct() -> None:
    recs = [
        RationaleRecord(trade_id="t-1", rationale="fade open", r_captured=2.0, grade_total=90.0),
        RationaleRecord(trade_id="t-2", rationale="fade open", r_captured=-1.0, grade_total=50.0),
        RationaleRecord(trade_id="t-3", rationale="fade open", r_captured=1.0, grade_total=70.0),
    ]
    miner = RationaleMiner(min_cluster_size=3, max_ngram=2)
    rep = miner.mine(recs)
    cluster = next(c for c in rep.clusters if c.phrase == "fade open")
    assert cluster.n == 3
    assert cluster.win_rate == pytest.approx(2 / 3, abs=0.01)
    assert cluster.mean_r == pytest.approx(0.667, abs=0.01)
    assert cluster.mean_grade == pytest.approx(70.0)


def test_mine_ranks_clusters_by_expectancy() -> None:
    recs = [RationaleRecord(trade_id=f"win-{i}", rationale="strong winner", r_captured=3.0) for i in range(3)]
    recs += [RationaleRecord(trade_id=f"loss-{i}", rationale="weak loser", r_captured=-1.0) for i in range(3)]
    miner = RationaleMiner(min_cluster_size=3)
    rep = miner.mine(recs)
    # Top cluster by expectancy should be "strong winner" related
    assert rep.clusters[0].mean_r > rep.clusters[-1].mean_r


def test_top_winners_top_losers_populated() -> None:
    recs = [RationaleRecord(trade_id=f"w-{i}", rationale="breakout retest", r_captured=2.0) for i in range(3)]
    recs += [RationaleRecord(trade_id=f"l-{i}", rationale="chase high", r_captured=-1.5) for i in range(3)]
    rep = RationaleMiner(min_cluster_size=3).mine(recs)
    assert len(rep.top_winners) > 0
    assert len(rep.top_losers) > 0
    assert all(c.mean_r > 0 for c in rep.top_winners)
    assert all(c.mean_r < 0 for c in rep.top_losers)


# --------------------------------------------------------------------------- #
# Sharpe + stdev
# --------------------------------------------------------------------------- #


def test_sharpe_zero_when_std_zero() -> None:
    recs = [RationaleRecord(trade_id=f"t-{i}", rationale="flat trade", r_captured=1.0) for i in range(3)]
    rep = RationaleMiner(min_cluster_size=3).mine(recs)
    # All rs = 1.0 -> std = 0 -> sharpe = 0
    assert all(c.sharpe == 0.0 for c in rep.clusters)


def test_std_r_nonzero_when_varied() -> None:
    recs = [
        RationaleRecord(trade_id=f"t-{i}", rationale="varied phrase", r_captured=r)
        for i, r in enumerate([1.0, 2.0, -1.0])
    ]
    rep = RationaleMiner(min_cluster_size=3).mine(recs)
    c = next(c for c in rep.clusters if c.phrase == "varied")
    assert c.std_r > 0


# --------------------------------------------------------------------------- #
# Uncategorized tracking
# --------------------------------------------------------------------------- #


def test_uncategorized_includes_trades_with_no_shared_phrase() -> None:
    recs = [RationaleRecord(trade_id=f"grp-{i}", rationale="shared phrase here", r_captured=1.0) for i in range(3)]
    recs.append(
        RationaleRecord(
            trade_id="orphan",
            rationale="completely unique wording nowhere else",
            r_captured=0.5,
        )
    )
    rep = RationaleMiner(min_cluster_size=3).mine(recs)
    assert "orphan" in rep.uncategorized


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #


def test_winners_minus_losers() -> None:
    recs = [RationaleRecord(trade_id=f"w-{i}", rationale="winner", r_captured=2.0) for i in range(3)]
    recs += [RationaleRecord(trade_id=f"l-{i}", rationale="loser", r_captured=-1.0) for i in range(3)]
    rep = RationaleMiner(min_cluster_size=3).mine(recs)
    ranked = winners_minus_losers(rep)
    assert ranked[0][1] >= ranked[-1][1]


def test_coverage_metric() -> None:
    recs = [RationaleRecord(trade_id=f"t-{i}", rationale="shared", r_captured=1.0) for i in range(3)]
    recs.append(
        RationaleRecord(
            trade_id="orphan",
            rationale="one-off",
            r_captured=0.0,
        )
    )
    rep = RationaleMiner(min_cluster_size=3).mine(recs)
    # 3 of 4 in clusters -> 0.75
    assert coverage(rep) == pytest.approx(0.75, abs=0.01)


def test_coverage_zero_when_empty() -> None:
    rep = RationaleMiner().mine([])
    assert coverage(rep) == 0.0


def test_phrase_frequency_counts() -> None:
    recs = [
        RationaleRecord(trade_id="a", rationale="fade gap open", r_captured=1.0),
        RationaleRecord(trade_id="b", rationale="fade gap close", r_captured=1.0),
        RationaleRecord(trade_id="c", rationale="breakout retest", r_captured=1.0),
    ]
    top = phrase_frequency(recs, top_k=5)
    phrases = dict(top)
    assert phrases.get("fade", 0) == 2
    assert phrases.get("gap", 0) == 2


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_mine_handles_empty_rationales() -> None:
    recs = [RationaleRecord(trade_id=f"t-{i}", rationale="", r_captured=1.0) for i in range(5)]
    rep = RationaleMiner(min_cluster_size=3).mine(recs)
    # No phrases -> no clusters, all uncategorized
    assert rep.clusters == []
    assert len(rep.uncategorized) == 5


def test_mine_handles_single_trade() -> None:
    recs = [RationaleRecord(trade_id="t-1", rationale="solo trade", r_captured=0.5)]
    rep = RationaleMiner(min_cluster_size=1).mine(recs)
    assert rep.n_records == 1
    assert len(rep.clusters) >= 1
