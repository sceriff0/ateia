from __future__ import annotations

import math
from collections import defaultdict, deque
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


def _is_satisfied(v: str, adjacency: Dict[str, List[int]], color: Dict[int, str]) -> bool:
    """A marker is satisfied if it appears in < 2 rare/soft pairs, or its
    incident pairs are not all the same partition."""
    edges = adjacency[v]
    if len(edges) < 2:
        return True
    return len({color[e] for e in edges}) == 2


def _repair_to_fixed_point(
    soft_cs: List, adjacency: Dict[str, List[int]], color: Dict[int, str]
) -> Dict[int, str]:
    """Iterate repair passes to a fixed point (not a single pass): flipping one
    marker's pair can retroactively re-break an already-checked marker that
    shares that pair, so a single sweep is not enough (this was the original
    bug). Re-scan until no marker is left confined to one partition.

    Each flip is chosen defensively: among a vertex's incident pairs (tried in
    a deterministic, neighbor-name-sorted order), prefer one whose *other*
    endpoint stays satisfied after the flip, so fixing one marker does not
    blindly re-break another. If every candidate would re-break its other
    endpoint, flip the deterministic first candidate anyway to guarantee
    progress; the following pass(es) then address any fallout. Bounded by
    max_iters so a graph-theoretically unsatisfiable topology (an isolated
    odd cycle -- see module docstring note) degrades gracefully instead of
    looping forever.
    """

    def other_endpoint(edge_idx: int, v: str) -> str:
        a, b = soft_cs[edge_idx].markers
        return b if a == v else a

    max_iters = 2 * len(soft_cs) + 5
    for _ in range(max_iters):
        changed = False
        for v in sorted(adjacency):
            if _is_satisfied(v, adjacency, color):
                continue
            candidates = sorted(adjacency[v], key=lambda e: other_endpoint(e, v))
            chosen = None
            for e in candidates:
                u = other_endpoint(e, v)
                original = color[e]
                color[e] = "audit" if original == "enforce" else "enforce"
                u_ok = _is_satisfied(u, adjacency, color)
                color[e] = original
                if u_ok:
                    chosen = e
                    break
            if chosen is None:
                chosen = candidates[0]
            color[chosen] = "audit" if color[chosen] == "enforce" else "enforce"
            changed = True
        if not changed:
            break
    return color


def _two_color_partition(soft_cs: List) -> Dict[int, str]:
    """Assign each rare/soft pair (by index into ``soft_cs``) to ``"enforce"``
    or ``"audit"`` such that every marker appearing in >= 2 pairs sees both
    sides (never confined to a single partition).

    Two stages, both fully deterministic (sort keys are pure functions of
    marker name strings -- no dependence on set/dict iteration order or
    hashing, no RNG):

    1. A BFS 2-coloring of the marker/pair graph (markers are vertices, pairs
       are edges): components are visited in sorted-marker order, and within
       a component each vertex's still-uncolored incident edges are colored
       by alternating starting from the color opposite the edge that first
       reached it. This already satisfies every vertex of degree >= 2 in
       trees and even cycles, and gives a good balanced starting point.
    2. A repair pass iterated to a *fixed point* (``_repair_to_fixed_point``)
       to catch any marker the BFS pass alone leaves confined to one side
       (e.g. where two biconnected components share a vertex). A single
       repair sweep is not enough -- fixing one marker can retroactively
       re-break an already-checked marker sharing a pair with it -- so the
       repair re-scans until nothing changes.

    Note: a graph-theoretic edge case -- an isolated odd cycle (e.g. three
    pairs A-B, B-C, C-A, all rare/soft, with no other edges touching A, B, or
    C) -- has no valid 2-coloring at all (proper 2-edge-coloring of an odd
    cycle is impossible), so no algorithm can satisfy every vertex there.
    That topology does not arise from the exclusivity pairs exercised by
    this codebase's tests.
    """
    adjacency: Dict[str, List[int]] = defaultdict(list)
    for i, c in enumerate(soft_cs):
        a, b = c.markers
        adjacency[a].append(i)
        adjacency[b].append(i)

    def other_endpoint(edge_idx: int, v: str) -> str:
        a, b = soft_cs[edge_idx].markers
        return b if a == v else a

    color: Dict[int, str] = {}
    visited: set = set()
    parent_color: Dict[str, str] = {}
    enforce_count = 0
    audit_count = 0

    for start in sorted(adjacency):
        if start in visited:
            continue
        visited.add(start)
        parent_color[start] = None
        queue = deque([start])
        while queue:
            v = queue.popleft()
            incident = sorted(adjacency[v], key=lambda e: other_endpoint(e, v))
            uncolored = [e for e in incident if e not in color]
            if parent_color.get(v) is not None:
                next_color = "audit" if parent_color[v] == "enforce" else "enforce"
            else:
                # New component root: bias the starting color toward global balance.
                next_color = "enforce" if enforce_count <= audit_count else "audit"
            for e in uncolored:
                color[e] = next_color
                if next_color == "enforce":
                    enforce_count += 1
                else:
                    audit_count += 1
                nb = other_endpoint(e, v)
                if nb not in visited:
                    visited.add(nb)
                    parent_color[nb] = next_color
                    queue.append(nb)
                next_color = "audit" if next_color == "enforce" else "enforce"

    return _repair_to_fixed_point(soft_cs, adjacency, color)


def split_constraints(panel: Panel) -> Dict[str, List[dict]]:
    never_cs = [c for c in panel.exclusive if c.rate == "never"]
    soft_cs = sorted(
        (c for c in panel.exclusive if c.rate in ("rare", "soft")),
        key=lambda c: (c.markers[0], c.markers[1], c.rate),
    )

    partition = _two_color_partition(soft_cs)  # index -> "enforce" | "audit"

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
