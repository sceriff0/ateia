from utils.phenotyping.classify import classify_cell, minimal_violated_constraint

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
