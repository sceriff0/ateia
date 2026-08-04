import numpy as np
from utils.phenotyping.density import compute_density, mondrian_bins


def test_density_higher_in_a_tight_cluster():
    cluster = np.random.default_rng(0).normal(0, 0.2, (50, 2))
    sparse = np.array([[100.0 + 10 * i, 0.0] for i in range(10)])
    cent = np.vstack([cluster, sparse])
    rho, radius = compute_density(cent, c=1.0)
    assert radius > 0
    assert rho[:50].mean() > rho[50:].mean()


def test_mondrian_bins_respect_min_count_and_are_contiguous():
    rho = np.arange(100, dtype=float)
    bins = mondrian_bins(rho, min_count=20, max_bins=5)
    ids, counts = np.unique(bins, return_counts=True)
    assert list(ids) == list(range(len(ids)))     # contiguous 0..B-1
    assert counts.min() >= 20                       # every bin >= min_count
    assert len(ids) <= 5


def test_mondrian_single_bin_when_min_count_exceeds_half():
    rho = np.arange(30, dtype=float)
    bins = mondrian_bins(rho, min_count=25, max_bins=5)
    assert set(bins.tolist()) == {0}                # cannot split -> one bin
