from utils.phenotyping.feasible import enumerate_feasible, validate
from utils.phenotyping.panel_schema import parse_panel


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


def test_dead_marker_is_warning():
    _, warns = _run({
        "markers": {"A": {"role": "lineage", "compartment": "cell"},
                    "DEAD": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"A": "+"}},
        "constraints": {},
    })
    assert any("DEAD" in w for w in warns)
