"""Pure-numpy two-component 1-D Gaussian mixture (EM) fit.

Used as the GMM fallback for marker calibration (deriving positive/negative
nulls) when constraint-graph derivation is unavailable. No scipy/sklearn
dependency -- numpy only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class GMM1D:
    weights: np.ndarray
    means: np.ndarray
    variances: np.ndarray
    degenerate: bool

    @property
    def low_mean(self) -> float:
        return float(self.means[0])

    @property
    def high_mean(self) -> float:
        return float(self.means[1])

    def threshold(self) -> float:
        """Midpoint between the two component means."""
        return 0.5 * (float(self.means[0]) + float(self.means[1]))

    def responsibility_high(self, x) -> np.ndarray:
        """Posterior P(high component | x) for each value in ``x``.

        This is the soft-membership weight used by responsibility-weighted
        conformal calibration (replacing hard threshold masks). A degenerate
        fit (no separable components) returns an uninformative ``0.5``
        everywhere, so a marker with no usable bimodality contributes uniform
        weight rather than a spurious hard split. In the extreme tails where
        both component densities underflow to 0, membership falls back to the
        side of the midpoint threshold (deterministic, no RNG).
        """
        x = np.asarray(x, dtype=float)
        if self.degenerate:
            return np.full(x.shape, 0.5)
        lo = self.weights[0] * _norm_pdf(x, self.means[0], self.variances[0])
        hi = self.weights[1] * _norm_pdf(x, self.means[1], self.variances[1])
        denom = lo + hi
        resp = np.divide(
            hi, denom, out=np.full(x.shape, 0.5, dtype=float), where=denom > 0
        )
        tail = denom <= 0
        if np.any(tail):
            resp = np.where(tail, (x >= self.threshold()).astype(float), resp)
        return resp


def _norm_pdf(x: np.ndarray, m: float, v: float) -> np.ndarray:
    return np.exp(-0.5 * (x - m) ** 2 / v) / np.sqrt(2.0 * np.pi * v)


def fit_two_component_gmm(
    x,
    n_iter: int = 200,
    tol: float = 1e-6,
    min_var: float = 1e-9,
) -> GMM1D:
    """Fit a 2-component 1-D Gaussian mixture to `x` via EM.

    Deterministic: initialization comes from data quantiles, not RNG.
    `degenerate=True` when the two means coincide, a component collapses
    (near-zero weight), or the input is too small / (near-)constant.
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size

    if n < 4 or np.allclose(x, x[0] if n else 0.0):
        m = float(np.mean(x)) if n else 0.0
        return GMM1D(
            np.array([0.5, 0.5]),
            np.array([m, m]),
            np.array([min_var, min_var]),
            True,
        )

    lo, hi = np.percentile(x, 25), np.percentile(x, 75)
    means = np.array([lo, hi], dtype=float)
    if means[0] == means[1]:
        means = np.array([x.min(), x.max()], dtype=float)
    var = np.array([np.var(x) + min_var] * 2, dtype=float)
    w = np.array([0.5, 0.5], dtype=float)

    prev_ll = -np.inf
    for _ in range(n_iter):
        g = np.stack([w[k] * _norm_pdf(x, means[k], var[k]) for k in range(2)])  # (2, n)
        denom = g.sum(0) + 1e-300
        r = g / denom
        nk = r.sum(1) + 1e-12
        w = nk / n
        means = (r * x).sum(1) / nk
        var = np.maximum((r * (x - means[:, None]) ** 2).sum(1) / nk, min_var)
        ll = float(np.log(denom).sum())
        if abs(ll - prev_ll) < tol:
            break
        prev_ll = ll

    order = np.argsort(means)
    means, var, w = means[order], var[order], w[order]
    degenerate = bool(abs(means[1] - means[0]) < 1e-6 or w.min() < 1e-3)
    return GMM1D(w, means, var, degenerate)
