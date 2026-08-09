import json
import os

import numpy as np
from utils.phenotyping.classify import (
    CellOutcome,
    classify_cell,
    classify_cells_vectorized,
    minimal_violated_constraint,
)

LINEAGE = ["CD3", "CD4", "CD45", "CD8", "PanCK"]
# Minimal feasible set fragment (patterns already screened for never/requires).
FEASIBLE = [
    {"pattern": {"PanCK": 1, "CD45": 0, "CD3": 0, "CD8": 0, "CD4": 0}, "phenotype": "Tumour"},
    {"pattern": {"PanCK": 1, "CD45": 0, "CD3": 1, "CD8": 0, "CD4": 0}, "phenotype": "Tumour"},
    {"pattern": {"PanCK": 0, "CD45": 1, "CD3": 1, "CD8": 1, "CD4": 0}, "phenotype": "CD8_T"},
    {"pattern": {"PanCK": 0, "CD45": 1, "CD3": 0, "CD8": 0, "CD4": 0}, "phenotype": "Immune"},
    {"pattern": {"PanCK": 0, "CD45": 0, "CD3": 0, "CD8": 0, "CD4": 0}, "phenotype": "Unclassified"},
]
NEVER = [{"markers": ["PanCK", "CD45"], "rate": "never", "id": 0}]
REQUIRES = [{"if": "CD8", "then": "CD3", "id": 5}]

def _signs(**kw):
    s = {m: "free" for m in LINEAGE}
    s.update(kw)
    return s

def test_committed_when_free_marker_absorbed_by_F():
    # PanCK+ CD45- with CD3 free: both matching patterns map to Tumour -> committed (§5.8).
    out = classify_cell(_signs(PanCK="pos", CD45="neg"), FEASIBLE, NEVER, REQUIRES, LINEAGE)
    assert out.outcome == "Tumour"
    assert out.empty_type == 0 and out.violated_constraint_id == -1

def test_ambiguous_when_multiple_names():
    out = classify_cell(_signs(CD45="pos", PanCK="neg"), FEASIBLE, NEVER, REQUIRES, LINEAGE)
    assert out.outcome == "Ambiguous"           # Immune and CD8_T both feasible
    assert len(out.candidate_names) > 1

def test_unclassified_when_single_unnamed():
    out = classify_cell(_signs(PanCK="neg", CD45="neg", CD3="neg", CD8="neg", CD4="neg"),
                        FEASIBLE, NEVER, REQUIRES, LINEAGE)
    assert out.outcome == "Unclassified"

def test_conflict_on_confident_never_violation():
    out = classify_cell(_signs(PanCK="pos", CD45="pos"), FEASIBLE, NEVER, REQUIRES, LINEAGE)
    assert out.outcome == "Conflict"
    assert out.empty_type == 1 and out.violated_constraint_id == 0

def test_artefact_on_contradiction():
    out = classify_cell(_signs(PanCK="contra"), FEASIBLE, NEVER, REQUIRES, LINEAGE)
    assert out.outcome == "Artefact"
    assert out.empty_type == 2

def test_minimal_violated_requires():
    vid = minimal_violated_constraint(_signs(CD8="pos", CD3="neg"), NEVER, REQUIRES)
    assert vid == 5


_MODEL_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".superpowers", "sdd", "2026-08-09-phenotype-perf",
    "model_config.json",
)


def _naive_classify_cell_oracle(signs, feasible, never, requires, lineage):
    """TEST ORACLE — NOT production code.

    The obvious per-cell/per-pattern/per-marker scan that `classify_cells_vectorized`
    replaces (its uint64-bitmask match is the sole production implementation now;
    see classify.py). Kept here, independent of production, purely so the
    equivalence test below has something to compare against that couldn't share a
    bug with the code under test.
    """
    if any(signs.get(m) == "contra" for m in lineage):
        return CellOutcome("Artefact", 2, -1, [], [])
    matches = []
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
            matches.append(entry)
    names = sorted({e["phenotype"] for e in matches})
    if len(names) == 1:
        return CellOutcome(names[0], 0, -1, names, matches)
    if len(names) > 1:
        return CellOutcome("Ambiguous", 0, -1, names, matches)
    vid = minimal_violated_constraint(signs, never, requires)
    if vid is not None:
        return CellOutcome("Conflict", 1, vid, [], [])
    return CellOutcome("Artefact", 2, -1, [], [])


def test_classify_cells_vectorized_matches_naive_oracle():
    """Guard for the whole vectorization: classify_cells_vectorized (production)
    must agree with the naive per-cell/per-pattern/per-marker oracle above,
    field-for-field, on every cell — over randomized sign matrices (including
    contra and free signs) against the real compiled panel fixture.

    Deliberately does NOT compare against `classify_cell`: since production now
    holds only the vectorized path, `classify_cell` is a thin adapter over
    `classify_cells_vectorized` and comparing the two would be tautological — a
    bug in the batch path would show up identically on both sides. The oracle
    above is written independently, so it can't share a bug with the code under
    test. (It is exercised too, transitively, via the equality assertions below.)
    """
    with open(_MODEL_CONFIG_PATH) as fh:
        cfg = json.load(fh)
    lineage = cfg["lineage_markers"]
    feasible = cfg["feasible_set"]
    never = cfg["constraints"]["never"]
    requires = cfg["constraints"]["requires"]

    n = 5000
    rng = np.random.default_rng(1234)
    sign_values = np.array(["pos", "neg", "free", "contra"], dtype=object)
    # Skew away from contra/free so plenty of cells land in every outcome bucket
    # (committed, Ambiguous, Unclassified, Conflict, Artefact).
    probs = [0.35, 0.45, 0.15, 0.05]
    sm = {
        m: rng.choice(sign_values, size=n, p=probs)
        for m in lineage
    }

    got = classify_cells_vectorized(sm, feasible, never, requires, lineage)
    assert len(got) == n

    seen_outcomes = set()
    for i in range(n):
        signs_i = {m: sm[m][i] for m in lineage}
        want = _naive_classify_cell_oracle(signs_i, feasible, never, requires, lineage)
        g = got[i]
        seen_outcomes.add(g.outcome)
        assert g.outcome == want.outcome, f"cell {i}: outcome"
        assert g.empty_type == want.empty_type, f"cell {i}: empty_type"
        assert g.violated_constraint_id == want.violated_constraint_id, (
            f"cell {i}: violated_constraint_id"
        )
        assert g.candidate_names == want.candidate_names, f"cell {i}: candidate_names"
        assert g.candidate_patterns == want.candidate_patterns, f"cell {i}: candidate_patterns"

    # Sanity: the randomized sweep actually exercised more than one outcome bucket,
    # otherwise this test would be a tautology over a degenerate case.
    assert len(seen_outcomes) > 1


def test_classify_cell_adapter_matches_naive_oracle():
    """classify_cell is now a thin single-row adapter over classify_cells_vectorized
    (see classify.py). Confirm the adapter itself — not just the batch path —
    agrees with the independent naive oracle, on a handful of hand-picked cells
    covering each outcome bucket using the real compiled panel fixture.
    """
    with open(_MODEL_CONFIG_PATH) as fh:
        cfg = json.load(fh)
    lineage = cfg["lineage_markers"]
    feasible = cfg["feasible_set"]
    never = cfg["constraints"]["never"]
    requires = cfg["constraints"]["requires"]

    rng = np.random.default_rng(99)
    sign_values = ["pos", "neg", "free", "contra"]
    for _ in range(200):
        signs = {m: rng.choice(sign_values, p=[0.35, 0.45, 0.15, 0.05]) for m in lineage}
        want = _naive_classify_cell_oracle(signs, feasible, never, requires, lineage)
        got = classify_cell(signs, feasible, never, requires, lineage)
        assert got == want
