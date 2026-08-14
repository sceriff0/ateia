"""Tests for bin/export_geojson.py phenotype support — stamped id, classification,
pheno measurements, and the panel_model.json sidecar (phenotyping section 6.1).

Backward compatibility is paramount here: this module is the contract seam to the
sibling FlowPath QuPath extension. All new params default to None; existing
callers/tests must see byte-identical behaviour.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

_SPEC = importlib.util.spec_from_file_location(
    "export_geojson", Path(__file__).resolve().parent.parent / "bin" / "export_geojson.py"
)
eg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eg)


def test_stamped_id():
    assert eg.stamped_id("P001", 7) == "P001:7"
    assert eg.stamped_id("P001", 7.0) == "P001:7"


DENSE_PHENO_KEYS = {
    "_pheno.free_mask", "_pheno.ambiguous", "_pheno.n_candidates", "_pheno.depth",
    "_pheno.empty_type", "_pheno.violated_constraint_id", "_pheno.provenance",
    "_pheno.density_bin",
}


def _prow(**over):
    row = {
        "phenotype": "Tumour", "outcome": "Tumour",
        "pheno_score:Tumour": 0.9, "pheno_score:Immune": 0.0,
        "p_neg:CD3": 0.2, "p_pos:CD3": 0.8, "state:Ki67": 1,
        "sign:CD3": "1", "n_candidates": 1, "ancestor_depth": -1, "empty_type": 0,
        "violated_constraint_id": -1, "provenance": 0, "density_bin": 2,
    }
    row.update(over)
    return row


def test_pheno_measurements_are_the_eight_dense_keys_plus_sparse_scores():
    ms = eg.pheno_extra_measurements(_prow(), ["CD3"])
    names = {m["name"] for m in ms}
    assert DENSE_PHENO_KEYS <= names
    # Immune scores 0 -> absent, not a zero. Absent means 0 by contract.
    assert names - DENSE_PHENO_KEYS == {"_pheno.score.Tumour"}
    for m in ms:
        assert isinstance(m["value"], (int, float))


def test_no_legacy_phenotype_key_survives():
    """The old spellings registered as PHANTOM marker channels in the consumer's
    gating panel, because none of them parses as a compartment key."""
    ms = eg.pheno_extra_measurements(_prow(), ["CD3"])
    for m in ms:
        n = m["name"]
        assert n.startswith("_pheno.")
        assert not n.startswith("pheno_score: ")
        assert not n.startswith("state: ")
        assert not n.endswith(": p_neg") and not n.endswith(": p_pos")
        assert ": " not in n


def test_free_mask_decodes_against_lineage_order():
    row = _prow(**{"sign:CD3": "\u00b7", "sign:CD4": "1", "sign:CD8": "\u00b7"})
    ms = {m["name"]: m["value"] for m in eg.pheno_extra_measurements(row, ["CD3", "CD4", "CD8"])}
    assert ms["_pheno.free_mask"] == 0b101


def test_derivations_agree_with_the_scores():
    """n_candidates and ambiguous are materialized for QuPath's filter UI, which has no
    way to count sparse keys. The scores stay authoritative, so they must agree."""
    row = _prow(**{"n_candidates": 2, "pheno_score:Immune": 0.4, "pheno_score:Tumour": 0.6})
    ms = {m["name"]: m["value"] for m in eg.pheno_extra_measurements(row, ["CD3"])}
    n_scores = sum(1 for k in ms if k.startswith("_pheno.score."))
    assert ms["_pheno.n_candidates"] == n_scores == 2
    assert ms["_pheno.ambiguous"] == 1


def test_depth_is_dense_and_minus_one_when_not_collapsed():
    ms = {m["name"]: m["value"] for m in eg.pheno_extra_measurements(_prow(), ["CD3"])}
    assert ms["_pheno.depth"] == -1
    collapsed = {m["name"]: m["value"]
                 for m in eg.pheno_extra_measurements(_prow(ancestor_depth=0), ["CD3"])}
    assert collapsed["_pheno.depth"] == 0


def test_resolve_classification_reads_phenotype_not_outcome():
    """Forced: reading `outcome` leaves a collapsed cell as "Ambiguous" and makes the
    entire ancestor feature invisible in QuPath."""
    palette = {"Tumour": [10, 20, 30], "Immune": [1, 2, 3], "Ambiguous": [150, 150, 150]}
    name, color = eg.resolve_classification({"phenotype": "Tumour", "outcome": "Tumour"}, palette)
    assert name == "Tumour"
    assert color == eg.rgb_to_qupath_color(10, 20, 30)
    # collapsed: phenotype is the ancestor while outcome stays the verdict
    name, color = eg.resolve_classification({"phenotype": "Immune", "outcome": "Ambiguous"}, palette)
    assert name == "Immune"
    assert color == eg.rgb_to_qupath_color(1, 2, 3)


def test_sidecar_has_palette_and_constraint_table(tmp_path):
    cfg = json.loads((Path("tests/testdata/model_config_min.json")).read_text())
    out = tmp_path / "panel_model.json"
    eg.write_panel_model_sidecar(cfg, str(out))
    side = json.loads(out.read_text())
    assert "palette" in side and "constraint_table" in side and "phenotypes" in side
    # never pair present in the flattened constraint table
    kinds = {c["kind"] for c in side["constraint_table"]}
    assert "never" in kinds


def test_end_to_end_export_stamps_and_classifies(tmp_path):
    quant = pd.DataFrame({"label": [1, 2], "x": [5, 6], "y": [7, 8],
                          "PanCK: Cytoplasm: Mean": [10.0, 0.0], "area": [100, 100]})
    pheno = pd.DataFrame({"label": [1, 2],
                          "phenotype": ["Tumour", "Immune"],   # cell 2 collapsed
                          "outcome": ["Tumour", "Ambiguous"],
                          "n_candidates": [1, 2], "ancestor_depth": [-1, 0],
                          "empty_type": [0, 0], "violated_constraint_id": [-1, -1],
                          "provenance": [0, 0], "density_bin": [0, 0],
                          "sign:PanCK": ["1", "\u00b7"],
                          "pheno_score:Tumour": [0.9, 0.4],
                          "pheno_score:Immune": [0.0, 0.6],
                          "p_neg:PanCK": [0.02, 0.5], "p_pos:PanCK": [0.9, 0.5]})
    lookup = {int(r["label"]): dict(r) for _, r in pheno.iterrows()}
    palette = {"Tumour": [200, 0, 0], "Immune": [0, 0, 200], "Ambiguous": [150, 150, 150]}
    out = tmp_path / "cells.geojson"
    eg.export_geojson(quant, str(out), pixel_size=0.325,
                      marker_cols=["PanCK: Cytoplasm: Mean"], contours=None,
                      pheno_lookup=lookup, palette=palette, patient_id="P001",
                      lineage=["PanCK"])
    gj = json.loads(out.read_text())
    f0, f1 = gj["features"]
    assert f0["id"] == "P001:1"
    assert f0["properties"]["classification"]["name"] == "Tumour"
    # The collapsed cell draws as its ancestor, not grey "Ambiguous".
    assert f1["properties"]["classification"]["name"] == "Immune"

    measurements = f0["properties"]["measurements"]
    names = {m["name"] for m in measurements}
    assert "PanCK: Cytoplasm: Mean" in names        # legacy marker key unchanged
    assert DENSE_PHENO_KEYS <= names
    assert "_pheno.score.Tumour" in names
    assert "pheno_score: Tumour" not in names       # old spelling gone
    assert "PanCK: p_neg" not in names              # evidence stays in the CSV

    # Every feature carries the same eight dense keys, whatever its outcome.
    for f in gj["features"]:
        assert DENSE_PHENO_KEYS <= {m["name"] for m in f["properties"]["measurements"]}

    # The free marker on the collapsed cell shows up in its mask.
    m1 = {m["name"]: m["value"] for m in f1["properties"]["measurements"]}
    assert m1["_pheno.free_mask"] == 0b1

    # label measurement: the consumer reconstructs the stable id "<patient_id>:<label>"
    # from this numeric measurement (QuPath drops non-UUID feature ids on import).
    label_measurements = [m for m in measurements if m["name"] == "label"]
    assert len(label_measurements) == 1
    assert label_measurements[0]["value"] == 1


def test_panel_model_sidecar_is_self_describing(tmp_path):
    cfg = json.loads((Path("tests/testdata/model_config_min.json")).read_text())
    cfg.update({"phenotype_order": ["Tumour", "Unclassified"], "lineage_order": ["PanCK"],
                "outcome_names": ["Ambiguous", "Conflict", "Artefact", "Unclassified"]})
    out = tmp_path / "panel_model.json"
    eg.write_panel_model_sidecar(cfg, str(out))
    side = json.loads(out.read_text())
    assert side["contract"] == {
        "version": 1, "measurement_prefix": "_pheno.", "sparse_zero": True,
        "calibration_sidecar": "calibration_model.json",
    }
    assert side["phenotype_order"] == ["Tumour", "Unclassified"]
    assert side["lineage_order"] == ["PanCK"]
    assert side["outcome_names"][0] == "Ambiguous"


def test_calibration_model_sidecar_projects_the_qc(tmp_path):
    qc = {"chosen_alpha": 0.05, "alpha_target": 0.05, "crc_ran": True,
          "degraded_markers": ["CD31"], "n_bins": 3, "density_radius": 41.7,
          "calibration_ks": {}, "n_cells": 999,
          "thresholds": {"CD4": {"measurement": "CD4: Cell: Median", "collapsed": False,
                                 "degraded": False,
                                 "bins": [{"bin": 0, "n_cells": 10, "t_neg": 1.0, "t_pos": 2.0}]}}}
    out = tmp_path / "calibration_model.json"
    eg.write_calibration_model_sidecar(qc, "P001", str(out))
    side = json.loads(out.read_text())
    assert side["contract"] == {"version": 1, "kind": "calibration"}
    assert side["patient_id"] == "P001"
    for key in ("chosen_alpha", "degraded_markers", "n_bins", "thresholds"):
        assert key in side
    assert "n_cells" not in side           # QC-only field, not part of the contract
    entry = side["thresholds"]["CD4"]
    assert entry["measurement"] == "CD4: Cell: Median" and entry["collapsed"] is False


def test_backward_compat_no_panel_supplied_is_unchanged(tmp_path):
    """No pheno_lookup/palette/patient_id supplied -> constant "Cell", no id, no extras."""
    quant = pd.DataFrame({"label": [1, 2], "x": [5, 6], "y": [7, 8],
                          "PanCK: Cytoplasm: Mean": [10.0, 0.0], "area": [100, 100]})
    out = tmp_path / "cells.geojson"
    eg.export_geojson(quant, str(out), pixel_size=0.325,
                      marker_cols=["PanCK: Cytoplasm: Mean"], contours=None)
    gj = json.loads(out.read_text())
    f0 = gj["features"][0]
    assert "id" not in f0
    assert f0["properties"]["classification"] == {
        "name": "Cell", "colorRGB": eg.rgb_to_qupath_color(*eg.CELL_COLOR_RGB)
    }
    names = {m["name"] for m in f0["properties"]["measurements"]}
    assert "pheno_score: Tumour" not in names
    assert not any(n.startswith("pheno_score:") for n in names)
    # `label` is deliberately NOT in this negative assertion. It used to be, back
    # when the phenotype path was the only thing that emitted it. c8c8b21 made
    # `label` an unconditional measurement for every cell (FlowPath needs the
    # stable-id anchor whether or not a panel ran), and
    # tests/test_export_geojson.py::test_label_is_emitted_as_a_measurement now
    # asserts exactly that for this same no-panel case.
    assert "label" in names
