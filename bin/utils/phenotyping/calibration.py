"""Per-marker responsibility-WEIGHTED conformal calibration + degrade guard
(phenotyping section 5.1/5.2).

Assigns every cell a soft negative/positive membership weight for a marker
(from a two-component GMM posterior on whichever channel the reference source
names -- see `references.py`), then splits the (score, weight) pairs by density
bin, collapsing to a single global bin and degrading when a bin (or the whole
marker) lacks enough EFFECTIVE calibration mass (D5).

This replaces the earlier hard-threshold index sets (`neg_idx = {sk < thr}`),
which truncated the reference distribution at the GMM crossover and so violated
conformal exchangeability -- a cell's calibration set was selected using, and
contained, the cell's own score. Weighting keeps every cell but down-weights it
by how negative (resp. positive) it is, restoring exchangeability in
expectation. See the calibration-exchangeability design note.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np

from .gmm import fit_two_component_gmm


@dataclass
class MarkerCalibration:
    marker: str
    degraded: bool
    # bin id -> (scores, weights) over ALL cells in that bin (weights in [0,1])
    neg_by_bin: Dict[int, Tuple[np.ndarray, np.ndarray]]
    pos_by_bin: Dict[int, Tuple[np.ndarray, np.ndarray]]
    bins: np.ndarray


def _effective_count(weights: np.ndarray) -> float:
    """Effective number of calibration cells = Σw.

    Weights are GMM posteriors bounded in [0, 1], so their sum is a genuine
    effective count: it degrades both when total membership mass is negligible
    (all-tiny weights) and when only a few cells carry real weight (their sum is
    small). No Kish (Σw)²/Σw² correction is needed precisely because the [0,1]
    bound already prevents a single cell from contributing more than one unit.
    """
    return float(np.sum(np.asarray(weights, dtype=float)))


def marker_calibration_weights(
    k: str, values: Dict[str, np.ndarray], references: Dict[str, dict]
) -> Tuple[np.ndarray, np.ndarray]:
    """Soft negative/positive membership weights for marker `k`, per cell.

    The weight is a two-component-GMM posterior on the channel the reference
    source names:

    - neg ``partner`` source: ``w_neg = P(partner-high | s_partner)`` -- a cell
      confidently positive for the exclusivity partner is a confident negative
      for `k`.
    - neg ``requires_neg`` source (Fix 1, contrapositive of ``k -> consequent``):
      ``w_neg = P(consequent-low | s_consequent) = 1 - P(consequent-high)`` --
      a cell confidently negative for the consequent must be negative for `k`.
    - pos ``requires`` source: ``w_pos = P(antecedent-high | s_antecedent)``.
    - otherwise (``gmm`` fallback, and unresolved ``phenotype``/``control``):
      the GMM on `k`'s own values, ``w_neg = P(low | s_k)``,
      ``w_pos = P(high | s_k)``.

    Every branch returns dense weight arrays over all cells (no cell dropped).
    """
    ref = references[k]
    sk = np.asarray(values[k], dtype=float)
    neg, pos = ref["neg_source"], ref["pos_source"]
    own_high = fit_two_component_gmm(sk).responsibility_high(sk)

    if neg.get("type") == "partner" and neg.get("marker") in values:
        sj = np.asarray(values[neg["marker"]], dtype=float)
        w_neg = fit_two_component_gmm(sj).responsibility_high(sj)
    elif neg.get("type") == "requires_neg" and neg.get("marker") in values:
        sc = np.asarray(values[neg["marker"]], dtype=float)
        w_neg = 1.0 - fit_two_component_gmm(sc).responsibility_high(sc)
    else:
        w_neg = 1.0 - own_high

    if pos.get("type") == "requires" and pos.get("marker") in values:
        sx = np.asarray(values[pos["marker"]], dtype=float)
        w_pos = fit_two_component_gmm(sx).responsibility_high(sx)
    else:
        w_pos = own_high

    return w_neg, w_pos


def _split_weighted_by_bin(
    sk: np.ndarray, w: np.ndarray, bins: np.ndarray
) -> Dict[int, Tuple[np.ndarray, np.ndarray]]:
    out: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    for b in np.unique(bins):
        sel = bins == b
        out[int(b)] = (sk[sel], w[sel])
    return out


def finalize_calibration(
    k: str,
    sk: np.ndarray,
    w_neg: np.ndarray,
    w_pos: np.ndarray,
    bins: np.ndarray,
    min_cal: int,
) -> MarkerCalibration:
    """Split (score, weight) pairs by bin; collapse and degrade per D5.

    Per-bin split first. Adequacy is measured by effective sample size Σw (not
    a raw count), so a handful of confident cells or a smear of near-zero
    weights both read as "too little calibration." If any bin's effective
    negative or positive mass is below `min_cal`, collapse to a single global
    bin; if the collapsed marker is still short, set `degraded=True` (both
    p-values forced to 1.0 downstream).
    """
    sk = np.asarray(sk, dtype=float)
    w_neg = np.asarray(w_neg, dtype=float)
    w_pos = np.asarray(w_pos, dtype=float)

    def _adequate(b):
        nb = _split_weighted_by_bin(sk, w_neg, b)
        pb = _split_weighted_by_bin(sk, w_pos, b)
        ok = all(_effective_count(nb[x][1]) >= min_cal for x in nb) and all(
            _effective_count(pb[x][1]) >= min_cal for x in pb
        )
        return ok, nb, pb

    ok, nb, pb = _adequate(bins)
    if ok:
        return MarkerCalibration(k, False, nb, pb, bins.astype(int))

    collapsed = np.zeros_like(bins)
    ok, nb, pb = _adequate(collapsed)
    if ok:
        return MarkerCalibration(k, False, nb, pb, collapsed.astype(int))

    return MarkerCalibration(k, True, nb, pb, collapsed.astype(int))
