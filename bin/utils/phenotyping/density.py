from __future__ import annotations

from typing import Tuple

import numpy as np
from scipy.spatial import cKDTree


def compute_density(centroids: np.ndarray, c: float = 1.0) -> Tuple[np.ndarray, float]:
    """Local density per cell centroid.

    ``radius = c * median(nearest-neighbour distance)``; ``rho[i]`` is the
    number of other centroids within ``radius`` of centroid ``i`` (self
    excluded).
    """
    centroids = np.asarray(centroids, dtype=float)
    n = len(centroids)
    if n < 2:
        return np.zeros(n), 0.0
    tree = cKDTree(centroids)
    d, _ = tree.query(centroids, k=2)          # col 1 = nearest neighbour distance
    nn = d[:, 1]
    radius = float(c * np.median(nn))
    if radius <= 0:
        radius = float(np.mean(nn) + 1e-9)
    counts = tree.query_ball_point(centroids, r=radius, return_length=True)
    return counts.astype(float) - 1.0, radius   # exclude self


def _merge_small(bins: np.ndarray, min_count: int) -> np.ndarray:
    """Merge any bin holding fewer than ``min_count`` cells into an adjacent
    bin, iterating until every remaining bin satisfies the minimum, then
    relabel to contiguous ids ``0..B-1``.
    """
    bins = bins.copy()
    while True:
        ids, counts = np.unique(bins, return_counts=True)
        if len(ids) <= 1 or counts.min() >= min_count:
            break
        small = ids[int(np.argmin(counts))]
        neighbour = small - 1 if small > ids.min() else small + 1
        bins[bins == small] = neighbour
        # relabel contiguous
        remap = {old: new for new, old in enumerate(np.unique(bins))}
        bins = np.vectorize(remap.get)(bins)
    # final contiguous relabel
    remap = {old: new for new, old in enumerate(np.unique(bins))}
    return np.vectorize(remap.get)(bins).astype(int)


def mondrian_bins(rho: np.ndarray, min_count: int, max_bins: int = 5) -> np.ndarray:
    """Quantile-bin ``rho`` into at most ``max_bins`` bins, merging adjacent
    bins so every returned bin holds at least ``min_count`` cells. Returns
    contiguous integer bin ids ``0..B-1``.
    """
    rho = np.asarray(rho, dtype=float)
    n = len(rho)
    if n == 0:
        return np.array([], dtype=int)
    B = max(1, min(max_bins, n // max(1, min_count)))
    if B <= 1:
        return np.zeros(n, dtype=int)
    edges = np.unique(np.quantile(rho, np.linspace(0, 1, B + 1)))
    if len(edges) <= 2:
        return np.zeros(n, dtype=int)
    bins = np.clip(np.digitize(rho, edges[1:-1]), 0, len(edges) - 2)
    return _merge_small(bins, min_count)
