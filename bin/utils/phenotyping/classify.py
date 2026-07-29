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
