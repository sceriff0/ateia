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


def _panel_from_pairs(pairs, rate="rare"):
    markers = sorted({m for pair in pairs for m in pair})
    return parse_panel({
        "markers": {m: {"role": "lineage", "compartment": "cell"} for m in markers},
        "phenotypes": {"P": {markers[0]: "+"}},
        "constraints": {"exclusive": [{"markers": list(pair), "rate": rate} for pair in pairs]},
    })


def _degree_ge_2_markers(pairs):
    from collections import Counter
    deg = Counter()
    for a, b in pairs:
        deg[a] += 1
        deg[b] += 1
    return {m for m, d in deg.items() if d >= 2}


def test_stratification_counterexample_no_marker_confined():
    # Regression: a single repair pass over markers can fix one marker while
    # retroactively re-breaking an already-checked one that shares a pair
    # with it. Here 'H' appears in exactly two pairs -- ('D','H') and
    # ('C','H') -- and under the old single-pass repair both landed in
    # 'audit', so 'H' never appeared in 'enforce'. 'C' (degree 3) must also
    # not be confined.
    pairs = [
        ("A", "C"), ("D", "H"), ("D", "E"), ("E", "F"),
        ("C", "H"), ("C", "G"), ("B", "E"),
    ]
    p = _panel_from_pairs(pairs)
    s = split_constraints(p)
    enforce_markers = {m for c in s["enforce"] for m in c["markers"]}
    audit_markers = {m for c in s["audit"] for m in c["markers"]}
    for m in _degree_ge_2_markers(pairs):
        assert m in enforce_markers, f"{m!r} confined out of enforce"
        assert m in audit_markers, f"{m!r} confined out of audit"


def test_stratification_property_over_fixed_topologies_and_deterministic():
    topologies = [
        # path: internal nodes have degree 2
        [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")],
        # star: center has degree 4
        [("Z", "A"), ("Z", "B"), ("Z", "C"), ("Z", "D")],
        # even cycle (4-cycle): every node degree 2
        [("A", "B"), ("B", "C"), ("C", "D"), ("D", "A")],
        # diamond with an inner triangle: B and C degree 3, A and D degree 2
        [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"), ("B", "C")],
        # the reported counterexample tree
        [("A", "C"), ("D", "H"), ("D", "E"), ("E", "F"), ("C", "H"), ("C", "G"), ("B", "E")],
    ]
    for pairs in topologies:
        p = _panel_from_pairs(pairs)
        s1 = split_constraints(p)
        s2 = split_constraints(p)
        assert s1 == s2, f"topology {pairs}: not deterministic"
        enforce_markers = {m for c in s1["enforce"] for m in c["markers"]}
        audit_markers = {m for c in s1["audit"] for m in c["markers"]}
        for m in _degree_ge_2_markers(pairs):
            assert m in enforce_markers and m in audit_markers, (
                f"topology {pairs}: marker {m!r} confined to one partition"
            )
