"""Tests for bin/export_geojson.py's exported GeoJSON contract.

This module is the contract seam to the sibling FlowPath QuPath extension, so what
`export_geojson()` writes into each Feature is pinned here rather than left to the
`real`-tagged nf-tests (which CI's `--tag stub` gate never runs).

Recovered from tests/test_export_geojson_phenotype.py, which was deleted with the
panel-phenotyping extraction (see CHANGELOG's [Unreleased] Removed entry). Only the
"phenotyping is off" case survived that removal, and it is the case that now describes
the module's whole behaviour: constant "Cell" classification, no stamped feature `id`,
no phenotype measurements.
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


def test_no_panel_supplied_is_constant_cell_classification(tmp_path):
    """Constant "Cell", no id, no phenotype extras.

    The absent `id` is load-bearing: export_geojson() passes object_id=None so that
    features are NOT all stamped with the constant "PathDetectionObject", which QuPath
    treats as a per-object UUID — a shared id across every detection breaks re-import
    for the whole cohort.
    """
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
    assert "PanCK: Cytoplasm: Mean" in names        # the marker key is still emitted
    assert "pheno_score: Tumour" not in names
    assert not any(n.startswith("pheno_score:") or n == "label" for n in names)
