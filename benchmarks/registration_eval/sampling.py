"""Bootstrap confidence intervals and paired tiled-vs-untiled comparison."""
from __future__ import annotations

import numpy as np


def bootstrap_ci(values, n_boot: int = 1000, ci: float = 95, seed: int = 0, stat=np.median):
    """Return (ci_low, point_estimate, ci_high) for `stat` over bootstrap resamples."""
    v = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    boot = np.array([stat(rng.choice(v, size=v.size, replace=True)) for _ in range(n_boot)])
    half = (100 - ci) / 2
    return (float(np.percentile(boot, half)), float(stat(v)), float(np.percentile(boot, 100 - half)))


def paired_diff_test(a, b, n_boot: int = 1000, ci: float = 95, seed: int = 0) -> dict:
    """Bootstrap the median paired difference (a - b). CI excluding 0 => significant.

    `frac_positive` is the fraction of bootstrap medians > 0 (a one-sided mass).
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != b.shape:
        raise ValueError(f"paired arrays must match: {a.shape} vs {b.shape}")
    diff = a - b
    rng = np.random.default_rng(seed)
    boot = np.array([np.median(rng.choice(diff, size=diff.size, replace=True)) for _ in range(n_boot)])
    half = (100 - ci) / 2
    return {
        "median_diff": float(np.median(diff)),
        "ci_low": float(np.percentile(boot, half)),
        "ci_high": float(np.percentile(boot, 100 - half)),
        "frac_positive": float(np.mean(boot > 0)),
        "n": int(diff.size),
    }
