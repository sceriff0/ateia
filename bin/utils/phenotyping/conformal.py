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
    for b in np.unique(cal.bins):
        b = int(b)
        mask = cal.bins == b
        neg_cal = cal.neg_by_bin.get(b, np.array([]))
        pos_cal = cal.pos_by_bin.get(b, np.array([]))
        for i in np.where(mask)[0]:
            p_neg[i] = p_neg_scalar(sk[i], neg_cal)
            p_pos[i] = p_pos_scalar(sk[i], pos_cal)
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
