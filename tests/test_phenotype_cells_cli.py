import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "bin" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cp = _load("compile_panel")
pc = _load("phenotype_cells")


def _make_inputs(tmp_path):
    panel = tmp_path / "panel.yaml"
    panel.write_text(
        "markers:\n"
        "  PanCK: {role: lineage, compartment: cell, statistic: Mean, negative_reference: auto}\n"
        "  CD45:  {role: lineage, compartment: cell, statistic: Mean, negative_reference: auto}\n"
        "  Ki67:  {role: state,   compartment: cell, statistic: Mean}\n"
        "phenotypes:\n"
        "  Tumour: {PanCK: '+', CD45: '-'}\n"
        "  Immune: {CD45: '+', PanCK: '-'}\n"
        "constraints:\n"
        "  exclusive:\n"
        "    - {markers: [PanCK, CD45], rate: never}\n"
    )
    cfg_path = tmp_path / "model_config.json"
    cp.main(["--panel", str(panel), "--out", str(cfg_path),
             "--report", str(tmp_path / "r.html"), "--validate-only", "--accept-all"])

    rng = np.random.default_rng(0)
    rows = []
    lab = 1

    def add(n, pk, cd, ki, sd=0.2):
        nonlocal lab
        for _ in range(n):
            rows.append({"label": lab, "PanCK: Cell: Mean": pk + rng.normal(0, sd),
                         "CD45: Cell: Mean": cd + rng.normal(0, sd),
                         "Ki67: Cell: Mean": ki + rng.normal(0, 0.2)})
            lab += 1
    add(60, 10.0, 0.0, 10.0)         # Tumour, Ki67+
    add(60, 0.0, 10.0, 0.0)          # Immune, Ki67-
    add(20, 5.0, 5.0, 5.0, sd=5.0)   # mid, wide spread -> straddles both calibration
                                     # clusters; per cell signs land free/contra/pos/neg
                                     # so some resolve to a non-committed outcome
                                     # (Artefact via contra under weighted calibration,
                                     # or Ambiguous via free)
    add(2, 10.0, 10.0, 0.0)          # both-high (within the confident-positive cluster
                                     # for each marker, not a wild statistical outlier
                                     # that would hijack the 2-component GMM fit) -> Conflict
    quant = pd.DataFrame(rows)
    quant.to_csv(tmp_path / "merged_quant.csv", index=False)

    morph = pd.DataFrame({"label": quant["label"],
                          "x": rng.uniform(0, 500, len(quant)),
                          "y": rng.uniform(0, 500, len(quant)),
                          "area": 100.0})
    morph.to_csv(tmp_path / "morphology.csv", index=False)
    return cfg_path, tmp_path / "merged_quant.csv", tmp_path / "morphology.csv"


def test_phenotypes_csv_has_frozen_columns_and_taxonomy(tmp_path):
    cfg_path, quant, morph = _make_inputs(tmp_path)
    rc = pc.main(["--merged_quant", str(quant), "--morphology", str(morph),
                  "--model_config", str(cfg_path), "--out", str(tmp_path / "phenotypes.csv"),
                  "--audit", str(tmp_path / "audit.csv"), "--qc", str(tmp_path / "qc.json"),
                  "--alpha-target", "0.1", "--min-calibration", "20"])
    assert rc == 0
    df = pd.read_csv(tmp_path / "phenotypes.csv")

    cfg = json.loads(cfg_path.read_text())
    lineage = cfg["lineage_markers"]
    states = cfg["state_markers"]
    names = sorted({e["phenotype"] for e in cfg["feasible_set"]})
    expected = (["label", "phenotype", "candidates", "n_candidates", "tree_path",
                 "density_bin", "outcome", "empty_type", "violated_constraint_id", "provenance"]
                + [f"pheno_score:{n}" for n in names]
                + [f"p_neg:{m}" for m in lineage + states]
                + [f"p_pos:{m}" for m in lineage + states]
                + [f"sign:{m}" for m in lineage]
                + [f"state:{m}" for m in states])
    assert list(df.columns) == expected

    assert (df["provenance"] == 0).all()
    assert set(df["outcome"]).issubset(set(names) | {"Ambiguous", "Conflict", "Artefact", "Unclassified"})
    # robust outcomes present
    assert "Conflict" in set(df["outcome"])          # the 2 extreme both-high cells
    # The wide-spread mid cells produce a non-committed / rejected outcome. Under
    # responsibility-weighted calibration a cell in the valley between the two
    # modes is atypical of BOTH reference distributions, so two-sided conformal
    # rejects both hypotheses -> "contra" -> Artefact (a better call than the
    # old truncated calibration's "free" -> Ambiguous). Either flavor satisfies
    # "the straddlers are not confidently committed"; the Ambiguous *logic*
    # itself is unit-tested calibration-independently in test_classify.py.
    assert {"Ambiguous", "Artefact"} & set(df["outcome"])
    assert {"Tumour", "Immune"} & set(df["outcome"]) # at least one lineage committed
    # numeric evidence
    for m in lineage:
        assert pd.api.types.is_numeric_dtype(df[f"p_neg:{m}"])

    # Fix 4 gate: the label-free calibration diagnostic is emitted and the
    # weighted scheme is near-Uniform(0,1) on confident reference cells (the old
    # in-sample truncation spiked p-values near 0/1, giving KS -> ~0.5). We
    # deliberately re-baseline on THIS improving rather than on a frozen CSV.
    qc = json.loads((tmp_path / "qc.json").read_text())
    assert qc["calibration_ks"], "calibration_ks diagnostic must be populated"
    worst_ks = max(v for marker in qc["calibration_ks"].values() for v in marker.values())
    assert worst_ks < 0.15, f"calibration KS too far from uniform: {worst_ks}"


def test_qc_and_audit_written(tmp_path):
    cfg_path, quant, morph = _make_inputs(tmp_path)
    pc.main(["--merged_quant", str(quant), "--morphology", str(morph),
             "--model_config", str(cfg_path), "--out", str(tmp_path / "phenotypes.csv"),
             "--audit", str(tmp_path / "audit.csv"), "--qc", str(tmp_path / "qc.json"),
             "--alpha-target", "0.1", "--min-calibration", "20"])
    qc = json.loads((tmp_path / "qc.json").read_text())
    assert "chosen_alpha" in qc and "degraded_markers" in qc and "reporting_mode" in qc
    assert (tmp_path / "audit.csv").exists()   # no audit pairs here -> header-only file is fine


def test_phenotypes_csv_byte_identical_to_frozen_fixture(tmp_path):
    # Row assembly in run_phenotyping builds phenotypes.csv from preallocated
    # per-column arrays rather than a list of per-cell dicts (perf change, see
    # .superpowers/sdd/2026-08-09-phenotype-perf/task-3-brief.md). This test
    # pins the emitted CSV bytes against a fixture generated from the
    # pre-change code on this same deterministic input, so any future drift
    # in column order, dtype, or rounding is caught. Whole-file text
    # comparison (not DataFrame equality) is deliberate: DataFrame equality
    # can be blind to formatting differences (e.g. an int column silently
    # becoming float and writing "1.0" instead of "1") that a downstream CSV
    # consumer would still see.
    cfg_path, quant, morph = _make_inputs(tmp_path)
    out_csv = tmp_path / "phenotypes.csv"
    rc = pc.main(["--merged_quant", str(quant), "--morphology", str(morph),
                  "--model_config", str(cfg_path), "--out", str(out_csv),
                  "--audit", str(tmp_path / "audit.csv"), "--qc", str(tmp_path / "qc.json"),
                  "--alpha-target", "0.1", "--min-calibration", "20"])
    assert rc == 0
    expected = (_ROOT / "tests" / "testdata" / "phenotype_cells_regression_expected.csv").read_text()
    assert out_csv.read_text() == expected


def test_phenotypes_csv_byte_identical_full_panel_fixture():
    # The deterministic fixture above only exercises 2 lineage + 1 state
    # marker / 3 phenotypes / 13 columns. The perf change (and the bench in
    # the task brief) targets the real 12-lineage/7-state/104-pattern panel,
    # which produces the full 81-column shape. This test pins that shape and
    # its rounding against a committed fixture generated from the pre-change
    # code (git rev c7d4ddb) on the same tracked inputs, confirmed
    # byte-identical to the post-change code before being committed.
    cfg_path = _ROOT / "tests" / "testdata" / "model_config_full_panel.json"
    quant = _ROOT / "tests" / "testdata" / "phenotype_cells_full_panel_merged_quant.csv"
    morph = _ROOT / "tests" / "testdata" / "phenotype_cells_full_panel_morphology.csv"
    ph, _audit, _qc = pc.run_phenotyping(
        str(quant), str(morph), str(cfg_path), alpha_target=0.1, min_calibration=10
    )
    assert ph.shape[1] == 81
    expected_path = _ROOT / "tests" / "testdata" / "phenotype_cells_full_panel_expected.csv"
    assert ph.to_csv(index=False) == expected_path.read_text()


def test_round6_matches_python_round_on_ratio_lattice_including_640_multiples():
    # Regression guard for a real bug: an earlier version of run_phenotyping's
    # column assembly used np.round(arr, 6) for p_neg:*/p_pos:* instead of
    # Python's round(). np.round(x, 6) computes rint(x * 1e6) / 1e6 — the
    # multiply is inexact, so when x*1e6 lands within an ULP of a .5 boundary,
    # np.round can disagree with the correctly-rounded round(x, 6). This is
    # reachable in production: conformal p-values here are weighted ratios
    # (sum of weights)/(sum of weights), and when GMM responsibilities
    # saturate to 0.0/1.0 the ratio becomes an exact integer fraction m/T.
    # Sweeping every m/T for T up to 1280 (which includes 640 and 1280, both
    # observed to produce disagreements) and asserting pc._round6 matches
    # Python's round() would have caught a regression to np.round: at T=640,
    # np.round(1/640, 6) == 0.001562 while round(1/640, 6) == 0.001563.
    mismatches_python_vs_numpy = 0
    for t in (1, 7, 29, 43, 100, 640, 1280):
        m = np.arange(0, t + 1, dtype=np.float64)
        ratios = m / t
        expected = np.array([round(float(x), 6) for x in ratios])
        actual = pc._round6(ratios)
        np.testing.assert_array_equal(actual, expected)
        # Sanity: confirm np.round would in fact have disagreed somewhere in
        # this sweep, so this test is not vacuously passing.
        via_numpy = np.round(ratios, 6)
        mismatches_python_vs_numpy += int(np.sum(via_numpy != expected))
    assert mismatches_python_vs_numpy > 0, (
        "expected at least one np.round/round(x, 6) disagreement in this sweep; "
        "the guard would be vacuous otherwise"
    )


def test_round6_exact_counterexample_via_weighted_tail_pvalues():
    # The specific counterexample that surfaced the bug: 640 equally-weighted
    # reference scores 0..639, queried at 638.5 -> p_neg = 1/640 exactly.
    from phenotyping.conformal import weighted_tail_pvalues

    ref_scores = np.arange(640.0)
    ref_weights = np.ones(640)
    p_neg = weighted_tail_pvalues(np.array([638.5]), ref_scores, ref_weights, side="neg")
    assert p_neg[0] == 1.0 / 640

    correct = round(float(p_neg[0]), 6)
    wrong_via_numpy = float(np.round(p_neg, 6)[0])
    assert correct == 0.001563
    assert wrong_via_numpy == 0.001562
    assert correct != wrong_via_numpy

    assert pc._round6(p_neg)[0] == correct
