from __future__ import annotations

import itertools
from typing import Dict, List, Tuple

from .panel_schema import Panel, PanelError


def lineage_markers(panel: Panel) -> List[str]:
    return sorted(m for m, mk in panel.markers.items() if mk.role == "lineage")


def inherited_signature(panel: Panel, name: str) -> Dict[str, int]:
    chain: List[str] = []
    cur = name
    seen = set()
    while cur is not None:
        if cur in seen:
            raise PanelError(f"parent cycle at phenotype {cur!r}")
        seen.add(cur)
        chain.append(cur)
        cur = panel.phenotypes[cur].parent
    lin = set(lineage_markers(panel))
    sig: Dict[str, int] = {}
    for node in reversed(chain):  # ancestors first, child overrides
        for m, v in panel.phenotypes[node].markers.items():
            if m in lin:
                sig[m] = v
    return sig


def _violates_never(pattern: Dict[str, int], panel: Panel) -> bool:
    for c in panel.exclusive:
        if c.rate == "never":
            a, b = c.markers
            if pattern.get(a) == 1 and pattern.get(b) == 1:
                return True
    return False


def _violates_requires(pattern: Dict[str, int], panel: Panel) -> bool:
    for a, b in panel.requires:
        if pattern.get(a) == 1 and pattern.get(b) == 0:
            return True
    return False


def _match_specificity(pattern: Dict[str, int], sig: Dict[str, int]):
    for m, v in sig.items():
        if pattern[m] != v:
            return None
    return len(sig)


def name_pattern(panel: Panel, pattern: Dict[str, int]) -> str:
    best_name, best_spec = "Unclassified", -1
    for name in panel.phenotypes:
        spec = _match_specificity(pattern, inherited_signature(panel, name))
        if spec is not None and spec > best_spec:
            best_spec, best_name = spec, name
    return best_name


def enumerate_feasible(panel: Panel, max_enumerate: int = 100000) -> List[Dict]:
    lin = lineage_markers(panel)
    if 2 ** len(lin) > max_enumerate:
        raise PanelError(
            f"|lineage cube|=2**{len(lin)} exceeds --max-enumerate={max_enumerate}; "
            "raise the knob or reduce lineage markers (ILP fallback not implemented)"
        )
    out: List[Dict] = []
    for bits in itertools.product((0, 1), repeat=len(lin)):
        pattern = dict(zip(lin, bits))
        if _violates_never(pattern, panel) or _violates_requires(pattern, panel):
            continue
        out.append({"pattern": pattern, "phenotype": name_pattern(panel, pattern)})
    return out


def _ancestors(panel: Panel, name: str) -> set:
    """Names reachable by walking `name`'s `parent` chain (excludes `name` itself)."""
    anc: set = set()
    cur = panel.phenotypes[name].parent
    while cur is not None and cur not in anc:
        anc.add(cur)
        cur = panel.phenotypes[cur].parent
    return anc


def validate(panel: Panel, feasible: List[Dict]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    if not feasible:
        errors.append("Unsatisfiable feasible set (F = empty); check for a requires cycle.")

    named = {e["phenotype"] for e in feasible}

    sigs = {name: tuple(sorted(inherited_signature(panel, name).items())) for name in panel.phenotypes}
    seen: Dict[tuple, str] = {}
    collision_losers: set = set()
    for name, sig in sigs.items():
        if sig in seen:
            errors.append(f"Collision: phenotypes {seen[sig]!r} and {name!r} share an identical signature.")
            collision_losers.add(name)
        else:
            seen[sig] = name

    for name in panel.phenotypes:
        if name not in named and name not in collision_losers:
            # A Collision error already accounts for the tie-break loser above;
            # only report "unreachable" here when a constraint (not a naming
            # tie-break) is what actually deleted this phenotype's cells.
            errors.append(f"Unreachable phenotype {name!r}: a constraint deleted every cell of this type.")

    raw = {name: inherited_signature(panel, name) for name in panel.phenotypes}
    for a in raw:
        for b in raw:
            if a == b:
                continue
            if b in _ancestors(panel, a) or a in _ancestors(panel, b):
                # Ancestor/descendant pairs necessarily have subset signatures
                # (a child's inherited_signature is a superset of its parent's
                # by construction) -- that's correct inheritance, not
                # accidental overlap, so it must not warn.
                continue
            ka, kb = set(raw[a]), set(raw[b])
            if ka < kb and all(raw[a][m] == raw[b][m] for m in ka):
                warnings.append(f"Subsumption: {a!r} signature is a subset of {b!r} (resolved by specificity).")

    used = set()
    for ph in panel.phenotypes.values():
        used |= set(ph.markers)
    for c in panel.exclusive:
        used |= set(c.markers)
    for a, b in panel.requires:
        used |= {a, b}
    for m, mk in panel.markers.items():
        if mk.role == "lineage" and m not in used:
            warnings.append(f"Marker {m!r} declared but used by nothing (dead channel).")

    return errors, warnings


def common_ancestor(parents: Dict[str, str], names: List[str]):
    """The nearest phenotype that is an ancestor of every candidate.

    Returns ``(name, depth)`` -- or ``(None, -1)`` when there are fewer than two
    distinct candidates, when any candidate has no node in the tree, or when they
    share no common ancestor. A candidate that is itself an ancestor of the others
    IS the answer; specificity naming makes that reachable.

    ``depth`` = parent-links from the ancestor down to the NEAREST candidate, so 0
    means the ancestor is itself one of the candidates and 1 means it is their direct
    parent. It is a resume-cost hint: how far above the leaves the suspended call sits.
    ``-1`` is reserved for "not collapsed" and is never a real depth -- an earlier
    ``links - 1`` definition made a candidate-is-the-ancestor collapse indistinguishable
    from no collapse at all.

    THE one owner of this rule. FlowPath's ReconciliationQueue.propagateUp implemented
    the same semantics independently; that copy is deleted and the consumer reads the
    label this function produces.
    """
    uniq = sorted(set(names))
    if len(uniq) < 2 or any(n not in parents for n in uniq):
        return None, -1

    def chain(name):
        out, cur, seen = [], name, set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = parents.get(cur)
        return out

    chains = {n: chain(n) for n in uniq}
    for cand in chains[uniq[0]]:      # self first, then ancestors -> deepest common wins
        if all(cand in chains[n] for n in uniq):
            return cand, min(chains[n].index(cand) for n in uniq)
    return None, -1
