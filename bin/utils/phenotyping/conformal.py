"""Two-sided rank-based conformal scores (phenotyping section 5.3).

Given a cell's marker score and a per-density-bin calibration set, computes
two one-sided conformal p-values: `p_neg` (small => reject the "negative"
hypothesis => cell reads positive) and `p_pos` (small => reject the
"positive" hypothesis => cell reads negative). Each cell is ranked only
against its own bin's calibration set (`MarkerCalibration.bins[i]`). If the
marker's calibration is `degraded` (see calibration.py, D5), both p-values
are forced to 1.0 for every cell, which keeps the marker undetermined
("free") at any alpha downstream.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from .calibration import MarkerCalibration


def p_neg_scalar(s_i: float, neg_cal: np.ndarray) -> float:
    """One-sided conformal p-value against the negative calibration set.

    Small p_neg => s_i is unusually high relative to the negative
    distribution => reject "negative" => cell reads positive.
    """
    neg_cal = np.asarray(neg_cal, dtype=float)
    return (1 + int(np.sum(neg_cal >= s_i))) / (neg_cal.size + 1)


def p_pos_scalar(s_i: float, pos_cal: np.ndarray) -> float:
    """One-sided conformal p-value against the positive calibration set.

    Small p_pos => s_i is unusually low relative to the positive
    distribution => reject "positive" => cell reads negative.
    """
    pos_cal = np.asarray(pos_cal, dtype=float)
    return (1 + int(np.sum(pos_cal <= s_i))) / (pos_cal.size + 1)


def weighted_tail_pvalues(
    query: np.ndarray, ref_scores: np.ndarray, ref_weights: np.ndarray, side: str
) -> np.ndarray:
    """Responsibility-weighted one-sided tail probability, vectorized.

    Replaces the hard-membership rank p-value (`p_neg_scalar`/`p_pos_scalar`
    against a truncated calibration subset) with a weighted empirical tail over
    ALL reference cells:

        side="neg":  p_neg(q) = (Σ_j w_j · 1[s_j >= q]) / (Σ_j w_j)
        side="pos":  p_pos(q) = (Σ_j w_j · 1[s_j <= q]) / (Σ_j w_j)

    Every cell contributes, weighted by how negative (resp. positive) it is, so
    a confidently-positive cell (w_neg≈0) neither pollutes nor truncates the
    negative reference. Exchangeability is restored in expectation: the
    reference is no longer the marker distribution hard-cut at the GMM
    crossover. Returns all-1.0 if the total weight is ~0 (no usable reference
    -> marker stays undetermined, matching the degrade convention).
    """
    query = np.asarray(query, dtype=float)
    ref_scores = np.asarray(ref_scores, dtype=float)
    ref_weights = np.asarray(ref_weights, dtype=float)
    total = float(ref_weights.sum())
    if total <= 1e-12 or ref_scores.size == 0:
        return np.ones(query.shape)

    order = np.argsort(ref_scores, kind="mergesort")   # deterministic ties
    s_sorted = ref_scores[order]
    w_sorted = ref_weights[order]
    if side == "neg":
        # mass at-or-above q = total - mass strictly below q
        suffix = np.concatenate([np.cumsum(w_sorted[::-1])[::-1], [0.0]])
        idx = np.searchsorted(s_sorted, query, side="left")
        mass = suffix[idx]
    elif side == "pos":
        prefix = np.concatenate([[0.0], np.cumsum(w_sorted)])
        idx = np.searchsorted(s_sorted, query, side="right")
        mass = prefix[idx]
    else:
        raise ValueError(f"side must be 'neg' or 'pos', got {side!r}")
    return mass / total


def tail_threshold(
    ref_scores: np.ndarray, ref_weights: np.ndarray, side: str, alpha: float
):
    """Invert `weighted_tail_pvalues`: the score at which the tail crosses `alpha`.

    THE single owner of the forward rule's inverse. `phenotype_cells.py` calls this on
    the same `MarkerCalibration` object, at the same `chosen_alpha`, that produced the
    classification -- re-deriving a threshold from a GMM would be a second implementation
    of the decision rule and would diverge, which is exactly the failure `panel_schema.py`
    documents for VALID_STATS.

        side="neg" -> t_pos, and a cell reads pos iff  value >  t_pos
        side="pos" -> t_neg, and a cell reads neg iff  value <  t_neg

    THE COMPARISONS ARE STRICT, and the returned value is the LAST reference score that
    FAILS the bound -- not the first that satisfies it. Both facts follow from where the
    step functions jump:

        p_neg(q) = mass{s >= q}  is non-increasing and LEFT-continuous: at q = s_k the
        mass still includes s_k, and only drops for q just above it. So
        {q : p_neg(q) <= alpha} is the OPEN interval (t_pos, inf), whose infimum is the
        largest reference score still failing.

    Taking the first satisfying score instead is an off-by-one that mis-signs every cell
    lying strictly between two reference scores, which is most of them. `p_pos` is the
    mirror image (non-decreasing, right-continuous), giving the open interval
    (-inf, t_neg).

    Note the crossing: `t_pos` inverts the NEGATIVE reference and `t_neg` the POSITIVE
    one, matching `resolve_sign`'s two one-sided tests. Swapping them still round-trips
    within each side, so it is not a mistake the round-trip property catches on its own.

    Returns `None` when no reference score fails the bound -- including the zero-weight
    and empty-reference cases, where `weighted_tail_pvalues` degrades to all-1.0. `None`
    means THAT SIDE NEVER FIRES, so a marker with both sides `None` is undetermined
    everywhere, matching the degrade convention.
    """
    if side not in ("neg", "pos"):
        raise ValueError(f"side must be 'neg' or 'pos', got {side!r}")

    ref_scores = np.asarray(ref_scores, dtype=float)
    ref_weights = np.asarray(ref_weights, dtype=float)
    if ref_scores.size == 0 or float(ref_weights.sum()) <= 1e-12:
        return None

    candidates = np.unique(ref_scores)
    pvals = weighted_tail_pvalues(candidates, ref_scores, ref_weights, side)
    failing = np.nonzero(pvals > alpha)[0]
    if failing.size == 0:
        return None
    # p_neg fails on a PREFIX of the sorted scores -> the boundary is the last failure.
    # p_pos fails on a SUFFIX -> the boundary is the first failure.
    return float(candidates[failing[-1] if side == "neg" else failing[0]])


def ks_uniform(pvals: np.ndarray) -> float:
    """One-sample Kolmogorov–Smirnov distance of ``pvals`` to Uniform(0,1).

    Calibration diagnostic (§ Fix 4): under correct calibration the p-values of
    a genuinely-reference population are ~Uniform(0,1). A large value flags the
    old truncation pathology (p-values spiked near 0/1). Pure-numpy, no scipy
    dependency, so it runs anywhere the phenotyper runs.
    """
    p = np.sort(np.asarray(pvals, dtype=float))
    n = p.size
    if n == 0:
        return float("nan")
    ecdf_hi = np.arange(1, n + 1) / n
    ecdf_lo = np.arange(0, n) / n
    return float(np.maximum(np.abs(ecdf_hi - p), np.abs(p - ecdf_lo)).max())


def conformal_scores(sk: np.ndarray, cal: MarkerCalibration) -> Tuple[np.ndarray, np.ndarray]:
    """Per-cell two-sided conformal p-values, ranked within each cell's own bin.

    Returns `(p_neg, p_pos)` arrays shaped like `sk`. If `cal.degraded`,
    returns all-1.0 arrays for both (D5 degrade propagation).
    """
    sk = np.asarray(sk, dtype=float)
    n = sk.size
    if cal.degraded:
        return np.ones(n), np.ones(n)

    p_neg = np.ones(n)
    p_pos = np.ones(n)
    empty = (np.array([]), np.array([]))
    for b in np.unique(cal.bins):
        b = int(b)
        idx = np.where(cal.bins == b)[0]
        neg_s, neg_w = cal.neg_by_bin.get(b, empty)
        pos_s, pos_w = cal.pos_by_bin.get(b, empty)
        p_neg[idx] = weighted_tail_pvalues(sk[idx], neg_s, neg_w, "neg")
        p_pos[idx] = weighted_tail_pvalues(sk[idx], pos_s, pos_w, "pos")
    return p_neg, p_pos


def resolve_sign(p_neg: float, p_pos: float, alpha: float) -> str:
    """Two-sided sign resolution at level alpha (phenotyping section 5.4).

    `neg` stays in the prediction set iff `p_neg > alpha`; `pos` stays in
    the set iff `p_pos > alpha`. `{pos}` => "pos" (confidently positive,
    sign 1), `{neg}` => "neg" (confidently negative, sign 0), `{pos,neg}`
    => "free" (undetermined), `{}` => "contra" (contradiction, surfaced as
    Artefact evidence downstream).
    """
    neg_in = p_neg > alpha
    pos_in = p_pos > alpha
    if pos_in and not neg_in:
        return "pos"
    if neg_in and not pos_in:
        return "neg"
    if neg_in and pos_in:
        return "free"
    return "contra"


def resolve_signs(p_neg: np.ndarray, p_pos: np.ndarray, alpha: float) -> np.ndarray:
    """Vectorized `resolve_sign`, returning a dtype=object array of the same strings."""
    p_neg = np.asarray(p_neg, dtype=float)
    p_pos = np.asarray(p_pos, dtype=float)
    out = np.empty(p_neg.shape, dtype=object)
    for i in range(p_neg.size):
        out[i] = resolve_sign(float(p_neg[i]), float(p_pos[i]), alpha)
    return out
