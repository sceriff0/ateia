"""Candidate scoring + softmax pheno_score (phenotyping design §5.5).

Given a phenotype pattern (a `{marker: 0|1}` assignment) and per-cell calibrated
`p_neg`/`p_pos` marker probabilities, `score_pattern` computes a log-likelihood-style
score for that pattern, penalized by any violated `enforce` (rare double-positive)
constraints:

    score = Σ_k ℓ_k − Σ_enforce λ·1[a=1 ∧ b=1]

where `ℓ_k = 0` if the marker's resolved sign is `"free"` (it does not constrain the
phenotype), else `log(1 − p_neg_k)` if the pattern calls for marker `k` positive, else
`log(1 − p_pos_k)`.

`softmax_pheno_scores` turns a cell's list of candidate patterns (as produced by
`classify.classify_cells_vectorized`, each `{"pattern": {...}, "phenotype": name}`)
into a
probability distribution over phenotype names via a (shift-stabilized) softmax over
`score_pattern`, summing mass across candidates that share a phenotype name. Every
name in `all_pheno_names` is present in the output, `0.0` for names with no candidate
pattern for this cell. These `pheno_score:<Name>` values are exported to GeoJSON and
drive FlowPath's candidate set.
"""

from __future__ import annotations

import math
from typing import Dict, List

# Floor for (1 - p) before taking log, so a calibrated probability of exactly 1.0
# yields a large finite penalty instead of -inf.
_EPS = 1e-12


def _log1m(p: float) -> float:
    """log(1 - p), with (1 - p) floored at _EPS to guard p == 1.0."""
    return math.log(max(_EPS, 1.0 - p))


def score_pattern(
    pattern: Dict[str, int],
    p_neg: Dict[str, float],
    p_pos: Dict[str, float],
    signs: Dict[str, str],
    enforce: List[dict],
    lineage: List[str],
) -> float:
    """Score one candidate pattern for a cell: Σ_k ℓ_k − Σ_enforce λ·1[a=1∧b=1].

    `ℓ_k` is 0 for `free` markers (they don't constrain the phenotype); otherwise
    it is `log(1 − p_neg_k)` when the pattern is positive for marker `k`, or
    `log(1 − p_pos_k)` when the pattern is negative for marker `k`. Each `enforce`
    constraint (`{"markers": [a, b], "lambda": λ, ...}`) subtracts `λ` when the
    pattern is positive for both `a` and `b`.
    """
    score = 0.0
    for k in lineage:
        if signs.get(k) == "free":
            continue
        if pattern[k] == 1:
            score += _log1m(p_neg[k])
        else:
            score += _log1m(p_pos[k])
    for c in enforce:
        a, b = c["markers"]
        if pattern.get(a) == 1 and pattern.get(b) == 1:
            score -= float(c["lambda"])
    return score


def softmax_pheno_scores(
    candidate_patterns: List[Dict],
    all_pheno_names: List[str],
    p_neg: Dict[str, float],
    p_pos: Dict[str, float],
    signs: Dict[str, str],
    enforce: List[dict],
    lineage: List[str],
    temperature: float = 1.0,
) -> Dict[str, float]:
    """Softmax over `candidate_patterns`' scores, aggregated (summed) by phenotype name.

    Returns a dict with every name in `all_pheno_names` present: `0.0` for names with
    no candidate pattern for this cell, and softmax mass otherwise. The masses over
    candidate names sum to 1.0 (within floating tolerance) whenever there is at least
    one candidate. The softmax is shift-stabilized (subtract the max score before
    exponentiating) to avoid overflow.
    """
    out = {name: 0.0 for name in all_pheno_names}
    if not candidate_patterns:
        return out

    raw_scores = [
        score_pattern(entry["pattern"], p_neg, p_pos, signs, enforce, lineage)
        for entry in candidate_patterns
    ]
    m = max(raw_scores)
    exp_scores = [math.exp((r - m) / temperature) for r in raw_scores]
    z = sum(exp_scores)

    for entry, ex in zip(candidate_patterns, exp_scores):
        name = entry["phenotype"]
        out[name] = out.get(name, 0.0) + ex / z
    return out
