import pytest
from utils.phenotyping.panel_schema import PanelError, parse_panel, typecheck

PANEL = {
    "markers": {
        "PanCK": {
            "role": "lineage",
            "compartment": "cytoplasm",
            "statistic": "Mean",
            "negative_reference": "auto",
        },
        "CD45": {
            "role": "lineage",
            "compartment": "cytoplasm",
            "negative_reference": "auto",
        },
        "FoxP3": {"role": "lineage", "compartment": "nuclear"},
        "Ki67": {"role": "state", "compartment": "nuclear"},
    },
    "phenotypes": {
        "Tumour": {"PanCK": "+", "CD45": "-"},
        "Immune": {"CD45": "+", "PanCK": "-"},
        "Treg": {"parent": "Immune", "FoxP3": "+"},
    },
    "constraints": {
        "exclusive": [{"markers": ["PanCK", "CD45"], "rate": "never"}],
        "requires": [{"if": "FoxP3", "then": "CD45"}],
    },
}


def test_parse_normalizes_compartment_and_defaults_statistic():
    p = parse_panel(PANEL)
    assert p.markers["PanCK"].compartment == "Cytoplasm"
    assert p.markers["PanCK"].statistic == "Mean"  # explicit
    assert p.markers["FoxP3"].statistic == "Median"  # nuclear default
    assert p.markers["CD45"].statistic == "Mean"  # cytoplasm default
    assert p.markers["FoxP3"].compartment == "Nucleus"


def test_parse_phenotype_signature_and_parent():
    p = parse_panel(PANEL)
    assert p.phenotypes["Tumour"].markers == {"PanCK": 1, "CD45": 0}
    assert p.phenotypes["Treg"].parent == "Immune"
    assert p.exclusive[0].rate == "never"
    assert p.requires == [("FoxP3", "CD45")]


def test_typecheck_passes_on_valid_panel():
    typecheck(parse_panel(PANEL))  # must not raise


def test_missing_compartment_is_error():
    bad = {
        "markers": {"X": {"role": "lineage"}},
        "phenotypes": {"P": {"X": "+"}},
        "constraints": {},
    }
    with pytest.raises(PanelError, match="compartment"):
        typecheck(parse_panel(bad))


def test_state_marker_in_exclusive_is_error():
    bad = {
        "markers": {
            "A": {"role": "lineage", "compartment": "cell"},
            "K": {"role": "state", "compartment": "nuclear"},
        },
        "phenotypes": {"P": {"A": "+"}},
        "constraints": {"exclusive": [{"markers": ["A", "K"], "rate": "never"}]},
    }
    with pytest.raises(PanelError, match="state marker"):
        typecheck(parse_panel(bad))


def test_unknown_phenotype_marker_is_error():
    bad = {
        "markers": {"A": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"NOPE": "+"}},
        "constraints": {},
    }
    with pytest.raises(PanelError, match="unknown marker"):
        typecheck(parse_panel(bad))


def test_unresolved_parent_is_error():
    bad = {
        "markers": {"A": {"role": "lineage", "compartment": "cell"}},
        "phenotypes": {"P": {"parent": "GHOST", "A": "+"}},
        "constraints": {},
    }
    with pytest.raises(PanelError, match="parent"):
        typecheck(parse_panel(bad))


def test_bad_statistic_errors():
    bad = {
        "markers": {
            "A": {"role": "lineage", "compartment": "cell", "statistic": "Variance"}
        },
        "phenotypes": {"P": {"A": "+"}},
        "constraints": {},
    }
    with pytest.raises(PanelError, match="statistic"):
        typecheck(parse_panel(bad))


# ── the statistic vocabulary has ONE owner ────────────────────────────────────
def test_valid_stats_is_derived_from_measurements_not_re_declared():
    """VALID_STATS must BE the measurement vocabulary, not a copy of it.

    It used to be a literal ``{"Mean", "Median", "Sum"}``. That is a second,
    independent declaration of something ``bin/utils/measurements.py`` already
    owns, and the two diverged the instant REDSEA added its compensated
    statistics — a panel naming a column ``quantify.py`` really emits was
    rejected as invalid.
    """
    from utils.measurements import STATISTICS
    from utils.phenotyping.panel_schema import VALID_STATS

    assert VALID_STATS == set(STATISTICS)


def test_panel_may_gate_on_a_redsea_compensated_statistic():
    """The point of pairing REDSEA with phenotyping: gate on compensated signal.

    REDSEA removes lateral spillover from touching cells, which is exactly the
    artefact that makes a neighbour of a CD3+ cell look CD3+ — i.e. precisely the
    failure a lineage gate suffers from. A panel must be able to ask for it.
    """
    from utils.measurements import REDSEA_STATISTICS

    panel = dict(PANEL)
    panel["markers"] = dict(panel["markers"])
    panel["markers"]["PanCK"] = {
        "role": "lineage",
        "compartment": "cell",
        "statistic": REDSEA_STATISTICS[0],
        "negative_reference": "auto",
    }
    typecheck(parse_panel(panel))


def test_a_bogus_statistic_is_still_rejected():
    """Widening the vocabulary must not turn the check into a no-op."""
    panel = dict(PANEL)
    panel["markers"] = dict(panel["markers"])
    panel["markers"]["PanCK"] = {
        "role": "lineage",
        "compartment": "cell",
        "statistic": "Avg",
        "negative_reference": "auto",
    }
    with pytest.raises(PanelError, match="statistic must be one of"):
        typecheck(parse_panel(panel))
