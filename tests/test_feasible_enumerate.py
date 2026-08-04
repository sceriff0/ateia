from utils.phenotyping.feasible import (
    enumerate_feasible,
    inherited_signature,
    lineage_markers,
)
from utils.phenotyping.panel_schema import parse_panel

PANEL = {
    "markers": {
        "PanCK": {"role": "lineage", "compartment": "cell"},
        "CD45": {"role": "lineage", "compartment": "cell"},
        "CD3": {"role": "lineage", "compartment": "cell"},
        "CD8": {"role": "lineage", "compartment": "cell"},
        "CD4": {"role": "lineage", "compartment": "cell"},
        "Ki67": {"role": "state", "compartment": "nuclear"},
    },
    "phenotypes": {
        "Tumour": {"PanCK": "+", "CD45": "-"},
        "Immune": {"CD45": "+", "PanCK": "-"},
        "T_cell": {"parent": "Immune", "CD3": "+"},
        "CD8_T": {"parent": "T_cell", "CD8": "+", "CD4": "-"},
    },
    "constraints": {
        "exclusive": [{"markers": ["PanCK", "CD45"], "rate": "never"}],
        "requires": [{"if": "CD8", "then": "CD3"}],
    },
}


def test_lineage_markers_excludes_state_and_is_sorted():
    assert lineage_markers(parse_panel(PANEL)) == ["CD3", "CD4", "CD45", "CD8", "PanCK"]


def test_inherited_signature_merges_parent_chain():
    sig = inherited_signature(parse_panel(PANEL), "CD8_T")
    assert sig == {"CD45": 1, "PanCK": 0, "CD3": 1, "CD8": 1, "CD4": 0}


def test_never_pair_removed_from_F():
    for e in enumerate_feasible(parse_panel(PANEL)):
        assert not (e["pattern"]["PanCK"] == 1 and e["pattern"]["CD45"] == 1)


def test_requires_edge_removed_from_F():
    for e in enumerate_feasible(parse_panel(PANEL)):
        if e["pattern"]["CD8"] == 1:
            assert e["pattern"]["CD3"] == 1


def test_most_specific_naming_and_unclassified_present():
    F = enumerate_feasible(parse_panel(PANEL))
    cd8 = [
        e
        for e in F
        if e["pattern"] == {"CD45": 1, "PanCK": 0, "CD3": 1, "CD8": 1, "CD4": 0}
    ]
    assert cd8 and cd8[0]["phenotype"] == "CD8_T"
    assert "Unclassified" in {e["phenotype"] for e in F}
