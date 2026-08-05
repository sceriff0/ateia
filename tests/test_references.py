from utils.phenotyping.constraints import split_constraints
from utils.phenotyping.panel_schema import parse_panel
from utils.phenotyping.references import resolve_references

PANEL = {
    "markers": {
        "PanCK": {"role": "lineage", "compartment": "cell", "negative_reference": "auto"},
        "CD45":  {"role": "lineage", "compartment": "cell", "negative_reference": "auto"},
        "CD3":   {"role": "lineage", "compartment": "cell"},
        "CD8":   {"role": "lineage", "compartment": "cell"},
        "PD1":   {"role": "state",   "compartment": "cell", "negative_reference": "isotype_ch7"},
    },
    "phenotypes": {"Tumour": {"PanCK": "+", "CD45": "-"}, "Immune": {"CD45": "+", "PanCK": "-"}},
    "constraints": {
        "exclusive": [{"markers": ["PanCK", "CD45"], "rate": "never"}],
        "requires":  [{"if": "CD8", "then": "CD3"}],
    },
}

def test_auto_negative_uses_never_partner():
    p = parse_panel(PANEL)
    refs, _ = resolve_references(p, split_constraints(p))
    assert refs["PanCK"]["neg_source"] == {"type": "partner", "marker": "CD45"}
    assert refs["CD45"]["neg_source"] == {"type": "partner", "marker": "PanCK"}

def test_auto_positive_uses_requires_antecedent():
    p = parse_panel(PANEL)
    refs, _ = resolve_references(p, split_constraints(p))
    assert refs["CD3"]["pos_source"] == {"type": "requires", "marker": "CD8"}

def test_explicit_control_channel_and_gmm_fallback_warns():
    p = parse_panel(PANEL)
    refs, warns = resolve_references(p, split_constraints(p))
    assert refs["PD1"]["neg_source"] == {"type": "control", "ref": "isotype_ch7"}
    # CD8 is the antecedent of `CD8 -> CD3`, so by contrapositive (Fix 1) its
    # negative anchor is CD3-negative cells; only its POSITIVE side (CD8 is no
    # requires-consequent, no explicit ref) falls to gmm, with a warning.
    assert refs["CD8"]["neg_source"] == {"type": "requires_neg", "marker": "CD3"}
    assert refs["CD8"]["pos_source"] == {"type": "gmm"}
    assert any("CD8" in w and "positive_reference" in w for w in warns)
