from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List

from .panel_schema import Panel

DEFAULT_R = {"rare": 0.01, "soft": 0.05}
SOFT_LAMBDA = 1.0


def lambda_for(rate: str, r: float) -> float:
    if rate == "soft":
        return SOFT_LAMBDA
    r = min(max(r, 1e-6), 1 - 1e-6)
    return math.log((1.0 - r) / r)


def _r_of(c) -> float:
    return c.r if c.r is not None else DEFAULT_R.get(c.rate, 0.01)


def split_constraints(panel: Panel) -> Dict[str, List[dict]]:
    never_cs = [c for c in panel.exclusive if c.rate == "never"]
    soft_cs = sorted(
        (c for c in panel.exclusive if c.rate in ("rare", "soft")),
        key=lambda c: (c.markers[0], c.markers[1], c.rate),
    )

    # Deterministic base split: alternate enforce/audit over the sorted pairs.
    partition = {}  # index -> "enforce" | "audit"
    for i, _ in enumerate(soft_cs):
        partition[i] = "enforce" if i % 2 == 0 else "audit"

    # Stratification repair: any marker in >=2 pairs must not be confined to one side.
    marker_pairs = defaultdict(list)
    for i, c in enumerate(soft_cs):
        for m in c.markers:
            marker_pairs[m].append(i)
    for m, idxs in marker_pairs.items():
        if len(idxs) < 2:
            continue
        sides = {partition[i] for i in idxs}
        if len(sides) == 1:
            # Flip the last pair of this marker to the other side (deterministic: highest index).
            flip = max(idxs)
            partition[flip] = "audit" if partition[flip] == "enforce" else "enforce"

    out: Dict[str, List[dict]] = {"never": [], "enforce": [], "audit": [], "requires": []}
    next_id = 0
    for c in never_cs:
        out["never"].append({"markers": list(c.markers), "rate": "never", "r": 0.0, "id": next_id})
        next_id += 1
    for i, c in enumerate(soft_cs):
        if partition[i] == "enforce":
            r = _r_of(c)
            out["enforce"].append({
                "markers": list(c.markers), "rate": c.rate, "r": r,
                "lambda": lambda_for(c.rate, r), "id": next_id,
            })
            next_id += 1
    for i, c in enumerate(soft_cs):
        if partition[i] == "audit":
            out["audit"].append({
                "markers": list(c.markers), "rate": c.rate, "r": _r_of(c), "id": next_id,
            })
            next_id += 1
    for a, b in panel.requires:
        out["requires"].append({"if": a, "then": b, "id": next_id})
        next_id += 1
    return out
