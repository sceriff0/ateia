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


def test_pheno_extra_measurements_numeric_and_keys():
    prow = {
        "pheno_score:Tumour": 0.9, "pheno_score:Immune": 0.1,
        "p_neg:CD3": 0.2, "p_pos:CD3": 0.8, "p_neg:Ki67": 0.5, "p_pos:Ki67": 0.5,
        "state:Ki67": 1, "n_candidates": 1, "empty_type": 0,
        "violated_constraint_id": -1, "provenance": 0, "density_bin": 2,
    }
    ms = eg.pheno_extra_measurements(prow, ["Tumour", "Immune"], ["CD3"], ["Ki67"])
    names = {m["name"] for m in ms}
    assert "pheno_score: Tumour" in names
    assert "CD3: p_neg" in names and "CD3: p_pos" in names
    assert "state: Ki67" in names
    assert {"n_candidates", "empty_type", "violated_constraint_id", "provenance", "density_bin"} <= names
    for m in ms:
        assert isinstance(m["value"], (int, float))


def test_resolve_classification_uses_palette():
    palette = {"Tumour": [10, 20, 30], "Ambiguous": [150, 150, 150]}
    name, color = eg.resolve_classification({"outcome": "Tumour"}, palette)
    assert name == "Tumour"
    assert color == eg.rgb_to_qupath_color(10, 20, 30)


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
    quant_p = tmp_path / "merged.csv"
    quant.to_csv(quant_p, index=False)
    pheno = pd.DataFrame({"label": [1, 2], "outcome": ["Tumour", "Ambiguous"],
                          "n_candidates": [1, 2], "empty_type": [0, 0],
                          "violated_constraint_id": [-1, -1], "provenance": [0, 0],
                          "density_bin": [0, 0], "pheno_score:Tumour": [0.9, 0.4],
                          "pheno_score:Immune": [0.0, 0.4], "p_neg:PanCK": [0.02, 0.5],
                          "p_pos:PanCK": [0.9, 0.5]})
    lookup = {int(r["label"]): dict(r) for _, r in pheno.iterrows()}
    palette = {"Tumour": [200, 0, 0], "Ambiguous": [150, 150, 150]}
    out = tmp_path / "cells.geojson"
    eg.export_geojson(quant, str(out), pixel_size=0.325,
                      marker_cols=["PanCK: Cytoplasm: Mean"], contours=None,
                      pheno_lookup=lookup, palette=palette, patient_id="P001",
                      feasible_names=["Tumour", "Immune"], lineage=["PanCK"], states=[])
    gj = json.loads(out.read_text())
    f0 = gj["features"][0]
    assert f0["id"] == "P001:1"
    assert f0["properties"]["classification"]["name"] == "Tumour"
    measurements = f0["properties"]["measurements"]
    names = {m["name"] for m in measurements}
    assert "PanCK: Cytoplasm: Mean" in names        # legacy key unchanged
    assert "pheno_score: Tumour" in names           # new numeric evidence
    assert "PanCK: p_neg" in names
    # label measurement: FlowPath reconstructs the stable id "<patient_id>:<label>"
    # from this numeric measurement (QuPath drops non-UUID feature ids on import).
    # A dropped emit must fail here, not just silently miss the key.
    assert "label" in names
    label_measurements = [m for m in measurements if m["name"] == "label"]
    assert len(label_measurements) == 1
    assert label_measurements[0]["value"] == 1


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
