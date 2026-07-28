"""Per-marker conformal-calibration sets + degrade guard (phenotyping section 5.1/5.2).

Builds the negative/positive calibration index sets for a marker (using the
symbolic reference sources resolved by `references.py`, falling back to the
GMM when a source has no structural anchor), then splits the calibration
scores by density bin -- collapsing to a single global bin, and finally
degrading, when a bin (or the whole marker) doesn't have enough calibration
cells (D5).
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
    neg_by_bin: Dict[int, np.ndarray]
    pos_by_bin: Dict[int, np.ndarray]
    bins: np.ndarray


def marker_calibration_indices(
    k: str, values: Dict[str, np.ndarray], references: Dict[str, dict]
) -> Tuple[np.ndarray, np.ndarray]:
    """Select negative/positive calibration cell indices for marker `k`.

    `partner`/`requires` sources threshold the partner/antecedent channel at
    its own GMM crossover; every other source type (`gmm`, `phenotype`,
    `control`, ...) falls back to the GMM low/high mode on marker `k`'s own
    values.
    """
    ref = references[k]
    sk = np.asarray(values[k], dtype=float)
    neg, pos = ref["neg_source"], ref["pos_source"]

    if neg.get("type") == "partner" and neg["marker"] in values:
        sj = np.asarray(values[neg["marker"]], dtype=float)
        thr = fit_two_component_gmm(sj).threshold()
        neg_idx = np.where(sj >= thr)[0]
    else:
        thr = fit_two_component_gmm(sk).threshold()
        neg_idx = np.where(sk < thr)[0]

    if pos.get("type") == "requires" and pos["marker"] in values:
        sx = np.asarray(values[pos["marker"]], dtype=float)
        thr = fit_two_component_gmm(sx).threshold()
        pos_idx = np.where(sx >= thr)[0]
    else:
        thr = fit_two_component_gmm(sk).threshold()
        pos_idx = np.where(sk >= thr)[0]

    return neg_idx, pos_idx


def _split_by_bin(sk: np.ndarray, idx: np.ndarray, bins: np.ndarray) -> Dict[int, np.ndarray]:
    out: Dict[int, np.ndarray] = {}
    for b in np.unique(bins):
        sel = idx[bins[idx] == b]
        out[int(b)] = sk[sel]
    return out


def finalize_calibration(
    k: str,
    sk: np.ndarray,
    neg_idx: np.ndarray,
    pos_idx: np.ndarray,
    bins: np.ndarray,
    min_cal: int,
) -> MarkerCalibration:
    """Split calibration scores by bin; collapse and degrade per D5.

    Per-bin split first. If any bin holds fewer than `min_cal` negatives or
    positives, collapse to a single global bin (all-zeros). If the
    collapsed set is still short, set `degraded=True`.
    """
    sk = np.asarray(sk, dtype=float)
    neg_idx = np.asarray(neg_idx, dtype=int)
    pos_idx = np.asarray(pos_idx, dtype=int)

    def _adequate(b):
        nb = _split_by_bin(sk, neg_idx, b)
        pb = _split_by_bin(sk, pos_idx, b)
        ok = all(len(nb[x]) >= min_cal for x in nb) and all(len(pb[x]) >= min_cal for x in pb)
        return ok, nb, pb

    ok, nb, pb = _adequate(bins)
    if ok:
        return MarkerCalibration(k, False, nb, pb, bins.astype(int))

    collapsed = np.zeros_like(bins)
    ok, nb, pb = _adequate(collapsed)
    if ok:
        return MarkerCalibration(k, False, nb, pb, collapsed.astype(int))

    return MarkerCalibration(k, True, nb, pb, collapsed.astype(int))
