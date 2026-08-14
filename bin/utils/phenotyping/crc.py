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
- `crc_select_alpha`: the selection rule — scans the full ascending grid and
  keeps the largest alpha whose UCB risk clears `alpha_target`. The UCB is
  NOT monotone in alpha (see the function docstring for why), so this scan
  is exhaustive by necessity, not a shortcut. If no alpha in the grid
  qualifies, fall back to the smallest (most conservative) alpha.
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

    This MUST be an exhaustive linear scan, not a binary search. The UCB is

        UCB(alpha) = R(alpha) + sqrt(ln(1/delta) / (2 * n(alpha)))

    where `n(alpha)` is the count of committed cells, which grows with
    alpha (looser sign thresholds commit more cells). The width term
    therefore *shrinks* as alpha grows, while the risk term `R(alpha)`
    tends to grow — the two effects compete, so `UCB(alpha)` is not
    monotone in alpha. Empirically it is U-shaped: small alpha commits few
    cells, the width term dominates and the bound fails; mid-range alpha
    commits enough cells to shrink the width term while risk is still low;
    large alpha's risk term eventually dominates again. The qualifying set
    (UCB <= alpha_target) is therefore an INTERIOR INTERVAL of the sorted
    grid, not a prefix, so a binary search over the grid can miss a
    qualifying alpha entirely (a probe that lands past the interval reads
    as "everything below fails" when it does not). Scanning in ascending
    order and keeping the last (largest) qualifying alpha is the only
    correct way to find the least-abstention alpha that honors the risk
    guarantee. If none qualifies, fall back to the smallest (most
    conservative) alpha.
    """
    grid = sorted(alpha_grid)
    chosen = None
    for alpha in grid:
        if risk_ucb_fn(alpha) <= alpha_target:
            chosen = alpha
    return chosen if chosen is not None else grid[0]
