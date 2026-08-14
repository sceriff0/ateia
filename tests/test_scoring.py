import math

from utils.phenotyping.scoring import score_pattern, softmax_pheno_scores

LINEAGE = ["A", "B"]
ENFORCE = [{"markers": ["A", "B"], "rate": "rare", "r": 0.01, "lambda": 4.6, "id": 0}]


def test_free_marker_contributes_zero():
    s = score_pattern({"A": 1, "B": 1}, {"A": 0.5, "B": 0.5}, {"A": 0.5, "B": 0.5},
                      {"A": "free", "B": "free"}, [], LINEAGE)
    assert s == 0.0


def test_enforce_penalty_applied_on_double_positive():
    base = score_pattern({"A": 1, "B": 1}, {"A": 0.01, "B": 0.01}, {"A": 0.99, "B": 0.99},
                         {"A": "pos", "B": "pos"}, [], LINEAGE)
    pen = score_pattern({"A": 1, "B": 1}, {"A": 0.01, "B": 0.01}, {"A": 0.99, "B": 0.99},
                        {"A": "pos", "B": "pos"}, ENFORCE, LINEAGE)
    assert math.isclose(base - pen, 4.6, rel_tol=1e-9)


def test_softmax_sums_to_one_over_candidates_and_zero_elsewhere():
    cands = [
        {"pattern": {"A": 1, "B": 0}, "phenotype": "P"},
        {"pattern": {"A": 0, "B": 1}, "phenotype": "Q"},
    ]
    scores = softmax_pheno_scores(cands, ["P", "Q", "R"],
                                  {"A": 0.02, "B": 0.5}, {"A": 0.5, "B": 0.02},
                                  {"A": "pos", "B": "pos"}, [], LINEAGE)
    assert scores["R"] == 0.0
    assert math.isclose(scores["P"] + scores["Q"], 1.0, rel_tol=1e-9)
    assert scores["P"] > scores["Q"]   # A is the more confident positive
