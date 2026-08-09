import json
import os

import numpy as np
import pytest
from utils.phenotyping.classify import (
    _CHUNK_CELLS,
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


# Tracked fixture (a real compiled panel: 12 lineage markers, 104 feasible
# patterns — see `git ls-files tests/testdata/model_config_full_panel.json`),
# committed alongside the small `model_config_min.json` other tests depend on.
# Path is relative to this test file, not the SDD scratch directory (which is
# untracked and deleted when the plan finishes — a prior version of these tests
# pointed at `.superpowers/sdd/...` and would FileNotFoundError in CI).
_MODEL_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "testdata", "model_config_full_panel.json",
)

# Module-level fixture: parsed once, reused by every test below instead of each
# test re-opening/re-parsing the file.
with open(_MODEL_CONFIG_PATH) as _fh:
    _FULL_CFG = json.load(_fh)
FULL_LINEAGE = _FULL_CFG["lineage_markers"]
FULL_FEASIBLE = _FULL_CFG["feasible_set"]
FULL_NEVER = _FULL_CFG["constraints"]["never"]
FULL_REQUIRES = _FULL_CFG["constraints"]["requires"]


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

    n=5000 exceeds `_CHUNK_CELLS` (4096), so this also crosses the chunk-block
    seam in classify_cells_vectorized at least once (see
    test_classify_cells_vectorized_crosses_chunk_boundary for a seam-focused
    check with an exact, deliberately-chosen cell count).
    """
    n = 5000
    assert n > _CHUNK_CELLS, "test intent: exercise more than one chunk block"
    rng = np.random.default_rng(1234)
    sign_values = np.array(["pos", "neg", "free", "contra"], dtype=object)
    # Biased toward free/neg: a 12-marker/104-pattern panel with the markers
    # closer to uniform pos/neg/free (as an earlier version of this test used)
    # sends ~94% of cells to Artefact/Conflict, leaving only ~4.7% (committed +
    # Ambiguous) to actually reach the match-grouping code this test guards.
    # These probabilities were tuned (see task-1-report.md) to land ~300
    # committed and ~940 Ambiguous cells out of 5000, so the minimums asserted
    # below have real headroom instead of being satisfied by a single lucky cell.
    probs = [0.15, 0.35, 0.45, 0.05]  # pos, neg, free, contra
    sm = {
        m: rng.choice(sign_values, size=n, p=probs)
        for m in FULL_LINEAGE
    }

    got = classify_cells_vectorized(sm, FULL_FEASIBLE, FULL_NEVER, FULL_REQUIRES, FULL_LINEAGE)
    assert len(got) == n

    committed_count = 0
    ambiguous_count = 0
    for i in range(n):
        signs_i = {m: sm[m][i] for m in FULL_LINEAGE}
        want = _naive_classify_cell_oracle(signs_i, FULL_FEASIBLE, FULL_NEVER, FULL_REQUIRES, FULL_LINEAGE)
        g = got[i]
        if g.empty_type == 0 and g.outcome != "Ambiguous":
            committed_count += 1
        if g.outcome == "Ambiguous":
            ambiguous_count += 1
        assert g.outcome == want.outcome, f"cell {i}: outcome"
        assert g.empty_type == want.empty_type, f"cell {i}: empty_type"
        assert g.violated_constraint_id == want.violated_constraint_id, (
            f"cell {i}: violated_constraint_id"
        )
        assert g.candidate_names == want.candidate_names, f"cell {i}: candidate_names"
        assert g.candidate_patterns == want.candidate_patterns, f"cell {i}: candidate_patterns"

    # The match-grouping code (the code this test actually guards) is only
    # reached by committed/Ambiguous cells. Require real coverage of both, not
    # just "more than one outcome bucket" (which {Artefact, Conflict} alone
    # would satisfy without ever touching match-grouping).
    assert committed_count >= 100, (
        f"only {committed_count} committed cells — not enough coverage of the "
        "match-grouping path"
    )
    assert ambiguous_count >= 100, (
        f"only {ambiguous_count} Ambiguous cells — not enough coverage of the "
        "multi-name match-grouping path"
    )


def test_classify_cells_vectorized_crosses_chunk_boundary():
    """classify_cells_vectorized processes cells in blocks of `_CHUNK_CELLS` to
    bound match-matrix memory (see classify.py). Deliberately pick a cell count
    just past one chunk boundary (`_CHUNK_CELLS + 123`, three blocks for a
    smaller chunk-independent sanity check would also cross, but +123 keeps this
    fast) and confirm every cell — including ones on both sides of the seam —
    still agrees with the naive oracle. Uses the tiny hand-built FEASIBLE set
    (not the full panel) so this stays cheap despite the larger n.
    """
    n = _CHUNK_CELLS + 123
    rng = np.random.default_rng(7)
    sign_values = np.array(["pos", "neg", "free", "contra"], dtype=object)
    probs = [0.2, 0.3, 0.45, 0.05]
    sm = {m: rng.choice(sign_values, size=n, p=probs) for m in LINEAGE}

    got = classify_cells_vectorized(sm, FEASIBLE, NEVER, REQUIRES, LINEAGE)
    assert len(got) == n

    # Focus the comparison on cells straddling the chunk seam plus a scattered
    # sample of the rest, rather than all n (this fixture is tiny/fast, so a
    # full sweep is cheap too, but the seam is the specific thing under test).
    check_indices = list(range(_CHUNK_CELLS - 5, _CHUNK_CELLS + 5)) + list(range(0, n, 37))
    for i in sorted(set(check_indices)):
        signs_i = {m: sm[m][i] for m in LINEAGE}
        want = _naive_classify_cell_oracle(signs_i, FEASIBLE, NEVER, REQUIRES, LINEAGE)
        g = got[i]
        assert g == want, f"cell {i} (chunk boundary at {_CHUNK_CELLS})"


def test_classify_cell_adapter_matches_naive_oracle():
    """classify_cell is now a thin single-row adapter over classify_cells_vectorized
    (see classify.py). Confirm the adapter itself — not just the batch path —
    agrees with the independent naive oracle, on 200 randomized cells (same sign
    distribution as the batch equivalence test) using the real compiled panel
    fixture.
    """
    rng = np.random.default_rng(99)
    sign_values = ["pos", "neg", "free", "contra"]
    for _ in range(200):
        signs = {m: rng.choice(sign_values, p=[0.15, 0.35, 0.45, 0.05]) for m in FULL_LINEAGE}
        want = _naive_classify_cell_oracle(signs, FULL_FEASIBLE, FULL_NEVER, FULL_REQUIRES, FULL_LINEAGE)
        got = classify_cell(signs, FULL_FEASIBLE, FULL_NEVER, FULL_REQUIRES, FULL_LINEAGE)
        assert got == want


def test_classify_cells_vectorized_rejects_empty_lineage():
    """An empty `lineage` list must fail loudly, not fabricate n=0 and silently
    return `[]` — the batch path used to derive `n = len(sm[lineage[0]])` (an
    IndexError on empty `lineage`, previously uncaught), and `classify_all` would
    then hand `run_phenotyping` an empty outcome list, producing a silently
    short/empty `phenotypes.csv` instead of failing the run.
    """
    with pytest.raises(ValueError, match="at least one lineage marker"):
        classify_cells_vectorized({}, FEASIBLE, NEVER, REQUIRES, [])


def test_classify_cell_adapter_rejects_empty_lineage():
    """The classify_cell adapter must surface the same clear error as the batch
    path for an empty lineage list — not the raw IndexError this raised before
    the fix (`classify_cell({}, feasible, [], [], [])` is the reviewer's exact
    repro).
    """
    with pytest.raises(ValueError, match="at least one lineage marker"):
        classify_cell({}, FEASIBLE, [], [], [])
