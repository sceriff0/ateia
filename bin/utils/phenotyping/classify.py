"""Subcube ∩ F → five outcomes + reason (phenotyping design §5.5).

Given per-marker signs resolved by `conformal.resolve_sign` (`"pos"|"neg"|"free"|"contra"`),
the feasible set `F` (from `feasible.py`), and the `never`/`requires` constraints (from
`constraints.split_constraints`), classify a cell into one of five outcomes:

- ``<Phenotype>`` (committed): exactly one distinct candidate phenotype NAME (not
  ``"Unclassified"``) remains after intersecting the sign subcube with `F`.
- ``"Unclassified"``: the single remaining candidate name is ``"Unclassified"``.
- ``"Ambiguous"``: more than one distinct candidate name remains.
- ``"Conflict"``: no feasible pattern matches (empty ∩ F) AND a confident constraint
  violation explains why (a `never` pair both `pos`, or a `requires` `if` `pos` with
  `then` `neg`).
- ``"Artefact"``: no feasible pattern matches and no confident constraint violation
  explains it (e.g. any marker sign is `contra`, or the subcube is simply outside `F`
  for reasons not captured by a single confident constraint).

Per §5.8, candidates are counted by DISTINCT phenotype NAME: several feasible patterns
that map to the same name count as one committed candidate. This is what lets a `free`
marker be absorbed by `F` into a confident singleton (see test
`test_committed_when_free_marker_absorbed_by_F`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

# uint64 bitmask packing (see classify_cells_vectorized) can address at most 64
# lineage markers. enumerate_feasible already refuses to enumerate more than 16
# (2 ** len(lin) > max_enumerate), so this is a generous, defensive ceiling.
_MAX_VECTORIZED_LINEAGE = 64


@dataclass
class CellOutcome:
    outcome: str
    empty_type: int
    violated_constraint_id: int
    candidate_names: List[str]
    candidate_patterns: List[Dict]


def matching_patterns(signs: Dict[str, str], feasible: List[Dict], lineage: List[str]) -> List[Dict]:
    """Return the feasible entries consistent with `signs`.

    `pos` requires the pattern value to be 1, `neg` requires 0, `free` matches any
    value. If any lineage marker's sign is `contra`, the subcube is empty by
    construction and `[]` is returned regardless of `feasible`.
    """
    if any(signs.get(m) == "contra" for m in lineage):
        return []
    out: List[Dict] = []
    for entry in feasible:
        pat = entry["pattern"]
        ok = True
        for m in lineage:
            s = signs.get(m, "free")
            if s == "pos" and pat[m] != 1:
                ok = False
                break
            if s == "neg" and pat[m] != 0:
                ok = False
                break
        if ok:
            out.append(entry)
    return out


def minimal_violated_constraint(signs: Dict[str, str], never: List[dict], requires: List[dict]) -> Optional[int]:
    """Return the id of the first confident constraint violation, else None.

    A `never` constraint (mutually-exclusive marker pair) is confidently violated
    when both its markers are `pos`. A `requires` constraint (`if` marker implies
    `then` marker) is confidently violated when `if` is `pos` and `then` is `neg`.
    `never` constraints are checked first, in list order, followed by `requires`.
    """
    for c in never:
        a, b = c["markers"]
        if signs.get(a) == "pos" and signs.get(b) == "pos":
            return int(c["id"])
    for c in requires:
        if signs.get(c["if"]) == "pos" and signs.get(c["then"]) == "neg":
            return int(c["id"])
    return None


def classify_cell(
    signs: Dict[str, str],
    feasible: List[Dict],
    never: List[dict],
    requires: List[dict],
    lineage: List[str],
) -> CellOutcome:
    """Classify a cell into one of five outcomes: committed phenotype, Unclassified,
    Ambiguous, Conflict, or Artefact. See module docstring for the full taxonomy.
    """
    if any(signs.get(m) == "contra" for m in lineage):
        return CellOutcome("Artefact", 2, -1, [], [])

    entries = matching_patterns(signs, feasible, lineage)
    names = sorted({e["phenotype"] for e in entries})

    if len(names) == 1:
        return CellOutcome(names[0], 0, -1, names, entries)
    if len(names) > 1:
        return CellOutcome("Ambiguous", 0, -1, names, entries)

    # empty ∩ F: distinguish an explained Conflict from an unexplained Artefact.
    vid = minimal_violated_constraint(signs, never, requires)
    if vid is not None:
        return CellOutcome("Conflict", 1, vid, [], [])
    return CellOutcome("Artefact", 2, -1, [], [])


def classify_cells_vectorized(
    sm: Dict[str, np.ndarray],
    feasible: List[Dict],
    never: List[dict],
    requires: List[dict],
    lineage: List[str],
) -> List[CellOutcome]:
    """Batch equivalent of calling `classify_cell` once per cell, in order.

    `sm` maps each lineage marker to a length-n array of resolved signs
    (`"pos"|"neg"|"free"|"contra"`), e.g. `{m: resolve_signs(...) for m in lineage}`.
    Returns the same list of `CellOutcome` that `[classify_cell({m: sm[m][i] for m in
    lineage}, feasible, never, requires, lineage) for i in range(n)]` would produce —
    `classify_cell` and `matching_patterns` remain the reference implementation and
    are unchanged; this is purely a faster way to compute the same result over many
    cells at once.

    The feasible-pattern match (`matching_patterns`'s per-cell/per-pattern/per-marker
    triple loop) is replaced by encoding each pattern's positive markers as a
    `uint64` bitmask and each cell's `pos`/`neg` sign requirements as two more
    `uint64` values: a pattern matches a cell iff
    `(pos_req & ~pattern_mask) == 0 and (neg_req & pattern_mask) == 0`. `free`
    markers need no handling — they are absent from both masks. This bounds lineage
    to `_MAX_VECTORIZED_LINEAGE` (64) markers; above that, uint64 packing would
    silently truncate high-order markers, so this falls back to the per-cell
    reference path instead.
    """
    n = len(sm[lineage[0]]) if lineage else 0
    lin_count = len(lineage)

    if lin_count > _MAX_VECTORIZED_LINEAGE:
        return [
            classify_cell({m: sm[m][i] for m in lineage}, feasible, never, requires, lineage)
            for i in range(n)
        ]
    assert lin_count <= _MAX_VECTORIZED_LINEAGE

    bit = {m: np.uint64(1) << np.uint64(i) for i, m in enumerate(lineage)}
    pat_mask = np.array(
        [sum(int(bit[m]) for m in lineage if entry["pattern"][m] == 1) for entry in feasible],
        dtype=np.uint64,
    )
    pattern_names = np.array([entry["phenotype"] for entry in feasible])

    pos_req = np.zeros(n, dtype=np.uint64)
    neg_req = np.zeros(n, dtype=np.uint64)
    contra = np.zeros(n, dtype=bool)
    for m in lineage:
        col = sm[m]
        pos_req |= np.where(col == "pos", bit[m], np.uint64(0)).astype(np.uint64)
        neg_req |= np.where(col == "neg", bit[m], np.uint64(0)).astype(np.uint64)
        contra |= col == "contra"

    f_count = len(feasible)
    match = np.empty((n, f_count), dtype=bool)
    for j in range(f_count):
        p = pat_mask[j]
        match[:, j] = ((pos_req & ~p) == 0) & ((neg_req & p) == 0)
    match[contra] = False

    # Group matching feasible-entry indices by cell. np.nonzero on a C-order 2D
    # array yields (row, col) pairs sorted by row then col, so `cols` already
    # lists each cell's matches in the same order matching_patterns would.
    rows, cols = np.nonzero(match)
    counts = np.bincount(rows, minlength=n)
    offsets = np.concatenate(([0], np.cumsum(counts)))

    outs: List[CellOutcome] = []
    for i in range(n):
        if contra[i]:
            outs.append(CellOutcome("Artefact", 2, -1, [], []))
            continue

        idx = cols[offsets[i]:offsets[i + 1]]
        if idx.size == 0:
            signs_i = {m: sm[m][i] for m in lineage}
            vid = minimal_violated_constraint(signs_i, never, requires)
            if vid is not None:
                outs.append(CellOutcome("Conflict", 1, vid, [], []))
            else:
                outs.append(CellOutcome("Artefact", 2, -1, [], []))
            continue

        patterns_i = [feasible[j] for j in idx]
        names_i = sorted(set(pattern_names[idx].tolist()))
        if len(names_i) == 1:
            outs.append(CellOutcome(names_i[0], 0, -1, names_i, patterns_i))
        else:
            outs.append(CellOutcome("Ambiguous", 0, -1, names_i, patterns_i))
    return outs
