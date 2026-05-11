from __future__ import annotations

from eta_engine.scripts import l2_cpcv


def test_build_fold_indices_distributes_remainder() -> None:
    assert l2_cpcv._build_fold_indices(10, 3) == [(0, 4), (4, 7), (7, 10)]


def test_purged_train_indices_remove_test_purge_and_embargo() -> None:
    train = l2_cpcv._purged_train_indices(
        12,
        [(4, 6)],
        purge_size=1,
        embargo_size=2,
    )

    assert train == [0, 1, 2, 8, 9, 10, 11]


def test_cpcv_returns_distribution_for_enough_samples() -> None:
    returns = [0.25 if i % 3 else -0.1 for i in range(60)]

    report = l2_cpcv.cpcv(
        returns,
        n_folds=5,
        k_test=2,
        purge_size=1,
        embargo_size=1,
        metric="mean",
    )

    assert report.n_splits == 10
    assert report.test_score_mean is not None
    assert report.test_score_stddev is not None
    assert len(report.splits) == 10
    assert all(split.n_test > 0 for split in report.splits)


def test_cpcv_fails_closed_for_tiny_sample() -> None:
    report = l2_cpcv.cpcv([0.1, -0.1, 0.2], n_folds=5, k_test=2)

    assert report.n_splits == 0
    assert report.test_score_mean is None
    assert "sample too small" in " ".join(report.notes)
