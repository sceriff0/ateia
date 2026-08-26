"""How good is the per-tile accept/reject decision?

NO COMPETITOR REPORTS THIS, and synthetic data is what makes it computable:
the generator knows which tiles were truly unregistrable, so the gate can be
scored as a classifier rather than assumed correct.

It measures precisely the weakness this repo already documented. The dense
blanking sweep in tests/test_tile_residual_confidence.py found a 40-90%-blanked
band where tiles are ACCEPTED with a WRONG displacement, and about a third of
that exposure survives any per-tile threshold.
"""

from __future__ import annotations

import numpy as np

__all__ = ["gate_roc", "gate_auc"]


def _key(rec):
    return (int(rec["ix"]), int(rec["iy"]))


def gate_roc(labels, accepted):
    """Confusion matrix of the accept decision against truth.

    A tile absent from ``accepted`` counts as REJECTED: a method that never
    emitted a control point abstained, it did not accept.
    """
    tp = fp = tn = fn = 0
    for rec in labels:
        truth = bool(rec["registrable"])
        got = bool(accepted.get(_key(rec), False))
        if truth and got:
            tp += 1
        elif truth and not got:
            fn += 1
        elif not truth and got:
            fp += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    if precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = float("nan")
    return {"precision": precision, "recall": recall, "f1": f1,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn, "n": len(labels)}


def gate_auc(labels, scores):
    """AUC over a continuous confidence score, LOWER = more confident.

    That polarity matches scikit-image's phase-correlation ``error`` (0 is a
    perfect match, ~1 is no shared structure), which is the score STARE gates
    on. NaN -- what skimage returns for an empty crop -- is ranked as the least
    confident value possible, never dropped.
    """
    y, s = [], []
    for rec in labels:
        raw = scores.get(_key(rec), float("nan"))
        val = float(raw)
        y.append(1 if rec["registrable"] else 0)
        s.append(np.inf if np.isnan(val) else val)

    y = np.asarray(y)
    s = np.asarray(s, dtype=float)
    n_pos, n_neg = int(y.sum()), int((1 - y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")

    # Rank ascending because lower score = more confident = more likely positive.
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(s.size, dtype=float)
    ranks[order] = np.arange(1, s.size + 1, dtype=float)
    # Average ranks within ties so an uninformative constant score gives 0.5.
    for val in np.unique(s):
        tie = s == val
        if tie.sum() > 1:
            ranks[tie] = ranks[tie].mean()

    rank_sum = ranks[y == 1].sum()
    # Mann-Whitney with the polarity ALREADY flipped: because low score means
    # positive, this expression is 1 - auc_high_score_positive. Do not subtract
    # it from 1 again -- a perfectly separating score must give 1.0, and the
    # double flip would give 0.0.
    auc = (n_pos * (n_pos + 1) / 2 + n_pos * n_neg - rank_sum) / (n_pos * n_neg)
    return float(auc)
