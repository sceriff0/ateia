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
