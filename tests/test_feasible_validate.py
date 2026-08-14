import os

from utils.phenotyping.feasible import enumerate_feasible, validate
from utils.phenotyping.panel_schema import parse_panel

TESTDATA_DIR = os.path.join(os.path.dirname(__file__), "testdata")


def _run(panel_dict):
    p = parse_panel(panel_dict)
    return validate(p, enumerate_feasible(p))


def test_valid_panel_has_no_errors():
    errs, _ = _run({
        "markers": {"A": {"role": "lineage", "compartment": "cell"},
                    "B": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"A": "+"}, "Q": {"B": "+"}},
        "constraints": {},
    })
    assert errs == []


def test_unreachable_phenotype_is_error():
    errs, _ = _run({
        "markers": {"A": {"role": "lineage", "compartment": "cell"},
                    "B": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"A": "+", "B": "+"}},
        "constraints": {"exclusive": [{"markers": ["A", "B"], "rate": "never"}]},
    })
    assert any("unreachable" in e.lower() for e in errs)


def test_collision_identical_signatures_is_error():
    errs, _ = _run({
        "markers": {"A": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"A": "+"}, "Q": {"A": "+"}},
        "constraints": {},
    })
    assert any("collision" in e.lower() for e in errs)
    # Finding 2: the collision already explains the tie-break loser; it must
    # not ALSO be reported as unreachable-due-to-a-constraint.
    assert not any("constraint deleted every cell" in e for e in errs)


def test_dead_marker_is_warning():
    _, warns = _run({
        "markers": {"A": {"role": "lineage", "compartment": "cell"},
                    "DEAD": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"A": "+"}},
        "constraints": {},
    })
    assert any("DEAD" in w for w in warns)


def test_hierarchical_panel_has_zero_subsumption_warnings():
    # panel_min.yaml has a genuine parent chain (Immune -> T_cell -> CD8_T/CD4_T)
    # where every child's inherited_signature is necessarily a superset of its
    # parent's. That is correct inheritance, not accidental overlap, and must
    # not trigger a Subsumption warning (Finding 1).
    p = parse_panel(os.path.join(TESTDATA_DIR, "panel_min.yaml"))
    _, warns = validate(p, enumerate_feasible(p))
    assert not any("subsumption" in w.lower() for w in warns)


def test_unrelated_overlap_is_subsumption_warning():
    # P and Q are NOT linked by a parent chain, yet Q's signature is a strict
    # superset of P's with matching values on the shared marker: genuine
    # accidental overlap, which must still warn.
    _, warns = _run({
        "markers": {"A": {"role": "lineage", "compartment": "cell"},
                    "B": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"A": "+"}, "Q": {"A": "+", "B": "+"}},
        "constraints": {},
    })
    assert any("subsumption" in w.lower() for w in warns)


# ── common_ancestor ───────────────────────────────────────────────────────────

PARENTS = {
    "Immune": None, "T_cell": "Immune", "T_helper": "T_cell", "CD4_Treg": "T_cell",
    "Tumour": None,
}


def test_common_ancestor_of_siblings():
    from utils.phenotyping.feasible import common_ancestor

    assert common_ancestor(PARENTS, ["T_helper", "CD4_Treg"]) == ("T_cell", 1)


def test_common_ancestor_when_a_candidate_is_itself_the_ancestor():
    from utils.phenotyping.feasible import common_ancestor

    # depth 0 = the ancestor IS one of the candidates. It must not be -1, which is
    # the reserved "not collapsed" sentinel.
    assert common_ancestor(PARENTS, ["T_cell", "T_helper"]) == ("T_cell", 0)


def test_common_ancestor_depth_counts_to_the_nearest_candidate():
    from utils.phenotyping.feasible import common_ancestor

    # Immune is two links above T_helper but zero above itself -> nearest wins.
    assert common_ancestor(PARENTS, ["T_helper", "Immune"]) == ("Immune", 0)
    # T_cell is one link above both siblings.
    assert common_ancestor(PARENTS, ["T_helper", "CD4_Treg"]) == ("T_cell", 1)


def test_common_ancestor_none_across_independent_roots():
    from utils.phenotyping.feasible import common_ancestor

    assert common_ancestor(PARENTS, ["T_helper", "Tumour"]) == (None, -1)


def test_common_ancestor_needs_two_candidates():
    from utils.phenotyping.feasible import common_ancestor

    assert common_ancestor(PARENTS, ["T_helper"]) == (None, -1)
    assert common_ancestor(PARENTS, []) == (None, -1)


def test_common_ancestor_ignores_unknown_names():
    from utils.phenotyping.feasible import common_ancestor

    # "Unclassified" is a feasible-set name with no node in the phenotype tree.
    assert common_ancestor(PARENTS, ["T_helper", "Unclassified"]) == (None, -1)
