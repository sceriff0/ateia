"""Fix 1: widen structural anchor resolution (references.py §4.5).

A marker that is the ANTECEDENT of a `requires` (M -> Y) gains a negative
anchor by contrapositive: Y-negative cells must be M-negative, so they are a
structural negative reference for M that does NOT threshold M on itself. This
reduces reliance on the self-referential GMM fallback (the exchangeability
concern), complementing the responsibility-weighting fix.
"""

from utils.phenotyping.constraints import split_constraints
from utils.phenotyping.panel_schema import parse_panel
from utils.phenotyping.references import resolve_references

_PANEL = """
markers:
  CD8:  {role: lineage, compartment: cell, statistic: Mean, negative_reference: auto}
  CD3:  {role: lineage, compartment: cell, statistic: Mean, negative_reference: auto}
phenotypes:
  CD8_T: {CD3: '+', CD8: '+'}
  Other: {CD3: '-', CD8: '-'}
constraints:
  requires:
    - {if: CD8, then: CD3}
"""


def test_antecedent_gets_requires_negative_anchor(tmp_path):
    p = tmp_path / "panel.yaml"
    p.write_text(_PANEL)
    panel = parse_panel(str(p))
    split = split_constraints(panel)
    refs, warns = resolve_references(panel, split)

    # CD8 -> CD3: CD3-negative cells are a structural negative anchor for CD8.
    assert refs["CD8"]["neg_source"] == {"type": "requires_neg", "marker": "CD3"}
    # and CD8 no longer emits an unresolved-negative GMM-fallback warning
    assert not any("CD8" in w and "negative_reference" in w for w in warns)

    # CD3 is only a consequent, not an antecedent -> still GMM fallback for neg.
    assert refs["CD3"]["neg_source"] == {"type": "gmm"}
