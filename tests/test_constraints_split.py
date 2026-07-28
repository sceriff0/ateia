import math

from utils.phenotyping.constraints import lambda_for, split_constraints
from utils.phenotyping.panel_schema import parse_panel


def test_lambda_rare_is_log_odds():
    assert math.isclose(lambda_for("rare", 0.01), math.log(0.99 / 0.01), rel_tol=1e-9)


def test_all_never_pairs_are_hard_and_never_in_audit():
    p = parse_panel({
        "markers": {m: {"role": "lineage", "compartment": "cell"} for m in ["A", "B", "C", "D"]},
        "phenotypes": {"P": {"A": "+"}},
        "constraints": {"exclusive": [
            {"markers": ["A", "B"], "rate": "never"},
            {"markers": ["C", "D"], "rate": "never"},
        ]},
    })
    s = split_constraints(p)
    assert len(s["never"]) == 2
    assert s["enforce"] == [] and s["audit"] == []


def test_rare_soft_split_is_deterministic_and_ids_unique():
    p = parse_panel({
        "markers": {m: {"role": "lineage", "compartment": "cell"} for m in ["A", "B", "C", "D", "E", "F"]},
        "phenotypes": {"P": {"A": "+"}},
        "constraints": {"exclusive": [
            {"markers": ["A", "B"], "rate": "rare"},
            {"markers": ["C", "D"], "rate": "rare"},
            {"markers": ["A", "C"], "rate": "soft"},
            {"markers": ["E", "F"], "rate": "rare"},
        ]},
    })
    s1 = split_constraints(p)
    s2 = split_constraints(p)
    assert s1 == s2                                   # deterministic
    ids = [c["id"] for c in s1["never"] + s1["enforce"] + s1["audit"]]
    assert len(ids) == len(set(ids))                  # unique
    assert len(s1["enforce"]) + len(s1["audit"]) == 4 # all rare/soft placed


def test_no_marker_confined_to_one_partition_when_it_has_multiple_pairs():
    p = parse_panel({
        "markers": {m: {"role": "lineage", "compartment": "cell"} for m in ["A", "B", "C", "D", "E"]},
        "phenotypes": {"P": {"A": "+"}},
        "constraints": {"exclusive": [
            {"markers": ["A", "B"], "rate": "rare"},
            {"markers": ["A", "C"], "rate": "rare"},
            {"markers": ["A", "D"], "rate": "rare"},
            {"markers": ["A", "E"], "rate": "rare"},
        ]},
    })
    s = split_constraints(p)
    enforce_markers = {m for c in s["enforce"] for m in c["markers"]}
    audit_markers = {m for c in s["audit"] for m in c["markers"]}
    # 'A' appears in 4 pairs — must not be confined to one partition.
    assert "A" in enforce_markers and "A" in audit_markers
