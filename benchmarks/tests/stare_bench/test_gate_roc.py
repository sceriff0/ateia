import pytest

from benchmarks.stare_bench.metrics.gate_roc import gate_auc, gate_roc


def _labels(flags):
    return [{"ix": i, "iy": 0, "registrable": f} for i, f in enumerate(flags)]


def test_perfect_gate_scores_one():
    labels = _labels([True, True, False, False])
    accepted = {(0, 0): True, (1, 0): True, (2, 0): False, (3, 0): False}
    got = gate_roc(labels, accepted)
    assert got["precision"] == 1.0 and got["recall"] == 1.0 and got["f1"] == 1.0
    assert (got["tp"], got["fp"], got["tn"], got["fn"]) == (2, 0, 2, 0)


def test_a_gate_that_accepts_everything_has_perfect_recall_and_poor_precision():
    labels = _labels([True, False, False, False])
    accepted = {(i, 0): True for i in range(4)}
    got = gate_roc(labels, accepted)
    assert got["recall"] == 1.0
    assert got["precision"] == pytest.approx(0.25)


def test_a_missing_tile_counts_as_rejected():
    # A tile the method never emitted is an abstention, not an acceptance.
    labels = _labels([True, True])
    got = gate_roc(labels, {(0, 0): True})
    assert got["fn"] == 1


def test_auc_is_one_for_a_perfectly_separating_score():
    labels = _labels([True, True, False, False])
    scores = {(0, 0): 0.01, (1, 0): 0.02, (2, 0): 0.98, (3, 0): 0.99}
    assert gate_auc(labels, scores) == pytest.approx(1.0)


def test_auc_is_half_for_an_uninformative_score():
    labels = _labels([True, False, True, False])
    scores = {(i, 0): 0.5 for i in range(4)}
    assert gate_auc(labels, scores) == pytest.approx(0.5)


def test_nan_scores_are_treated_as_least_confident():
    # skimage returns NaN when a crop is empty (issue #7078). A NaN must never
    # rank as confident -- that is the exact bug the NaN-safe gate in
    # bin/tiled_solve.py:_accept exists to prevent.
    labels = _labels([True, False])
    scores = {(0, 0): 0.01, (1, 0): float("nan")}
    assert gate_auc(labels, scores) == pytest.approx(1.0)
