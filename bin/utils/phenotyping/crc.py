"""Conformal Risk Control alpha-tuning (phenotyping section 5.6).

Tunes the single global alpha so that the audit-constraint excess
co-positivity risk stays at or below `alpha_target` (design decisions
D8/D14). Three primitives:

- `hoeffding_ucb`: a Hoeffding upper confidence bound on a bounded [0, 1]
  mean, used to turn a point-estimate risk into a conservative bound.
- `risk_excess_copositivity`: the empirical risk for one candidate alpha —
  how much the observed co-positivity rate of audited marker pairs exceeds
  each pair's nominal (expected) rate, averaged over pairs and floored at
  zero (a pair being *less* co-positive than nominal is not risk).
- `crc_select_alpha`: the selection rule. Risk is monotone increasing in
  alpha (looser thresholds commit more cells, so co-positivity can only
  grow), so the largest alpha whose UCB risk clears `alpha_target` is the
  least-abstention choice that still honors the guarantee. If no alpha in
  the grid qualifies, fall back to the smallest (most conservative) alpha.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

import numpy as np


def hoeffding_ucb(mean: float, n: int, delta: float = 0.1) -> float:
    """Hoeffding upper confidence bound for a [0, 1]-bounded mean.

    Returns `1.0` (maximally conservative) when `n <= 0`, since no sample
    supports any tighter bound.
    """
    if n <= 0:
        return 1.0
    return float(mean + np.sqrt(np.log(1.0 / delta) / (2.0 * n)))


def risk_excess_copositivity(
    committed_signs: Dict[str, np.ndarray],
    audit_pairs: List[dict],
    nominal: Dict[int, float],
) -> Tuple[float, int]:
    """Empirical excess co-positivity risk over audited marker pairs.

    `committed_signs[m]` is a 0/1 array over committed cells for marker
    `m`. `audit_pairs[i] = {"markers": [a, b], "id": int}`. `nominal[id]`
    is that pair's expected (nominal) co-positivity rate.

    For each pair, observed co-positivity is the fraction of committed
    cells where both markers read 1; the pair's excess is
    `max(0, observed - nominal)`. `R` is the mean excess over pairs; `n`
    is the number of committed cells (from the first usable pair, or 0 if
    none). Pairs referencing a marker missing from `committed_signs` or an
    id missing from `nominal` are skipped rather than raising.
    """
    n = 0
    for arr in committed_signs.values():
        n = int(np.asarray(arr).size)
        break

    excesses = []
    for pair in audit_pairs:
        a, b = pair["markers"]
        pair_id = pair["id"]
        if a not in committed_signs or b not in committed_signs:
            continue
        if pair_id not in nominal:
            continue
        va = np.asarray(committed_signs[a], dtype=int)
        vb = np.asarray(committed_signs[b], dtype=int)
        size = min(va.size, vb.size)
        if size == 0:
            continue
        n = size
        obs = float(np.mean((va[:size] == 1) & (vb[:size] == 1)))
        excesses.append(max(0.0, obs - float(nominal[pair_id])))

    R = float(np.mean(excesses)) if excesses else 0.0
    return R, n


def crc_select_alpha(
    alpha_grid: List[float],
    risk_ucb_fn: Callable[[float], float],
    alpha_target: float,
) -> float:
    """Select alpha: largest grid value with UCB risk <= alpha_target.

    Risk is monotone increasing in alpha, so the qualifying alphas
    (UCB risk <= alpha_target) form a prefix of the ascending grid, and a
    binary search for the rightmost element of that prefix yields the same
    least-abstention alpha that an ascending linear scan keeping the last
    qualifying value would — in O(log n) calls to `risk_ucb_fn` instead of
    O(n). If none qualifies, fall back to the smallest (most conservative)
    alpha.
    """
    grid = sorted(alpha_grid)
    lo, hi = 0, len(grid) - 1
    best_idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if risk_ucb_fn(grid[mid]) <= alpha_target:
            best_idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return grid[best_idx] if best_idx != -1 else grid[0]
